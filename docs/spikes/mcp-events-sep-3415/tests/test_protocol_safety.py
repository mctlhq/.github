import json
import subprocess
import sys
import threading
import unittest

from common import A, B, PR, REPO, World, pr_event, secret
from model.callback_guard import CallbackGuard, GuardError
from model.client import ExtensionNotAdvertised, ReferenceClient
from model.delivery import Response
from model.errors import CALLBACK_ENDPOINT_ERROR, METHOD_NOT_FOUND, NOT_FOUND, RpcError
from model.recipient import MockRecipient

ARGS = {"repository": "mctlhq/.github"}


def setup(w, name="a", fill=1, **kw):
    url, s = f"https://{name}.example/h", secret(fill=fill)
    r = w.recipient(url, s)
    return w.subscribe(url, s, **kw), r, url


class CapabilityTests(unittest.TestCase):
    def test_t16_no_extension_no_events_calls(self):
        w = World()
        w.server.advertise = False
        self.assertEqual({}, w.server.capabilities()["extensions"])
        for m in ("events/list", "events/poll", "events/subscribe", "events/unsubscribe", "events/stream"):
            with self.assertRaises(RpcError) as cm:
                w.rpc(m, {})
            self.assertEqual(METHOD_NOT_FOUND, cm.exception.code)
        c = ReferenceClient(w.server, A)
        with self.assertRaises(ExtensionNotAdvertised):
            c.call("events/poll", {"name": PR})
        self.assertEqual([], c.sent)
        plan = c.plan()
        self.assertEqual("fallback", plan["mode"])
        self.assertEqual({"claude-channel", "host-scheduled-reads"}, {f["kind"] for f in plan["fallbacks"]})
        self.assertFalse(any(f["shares_events_cursor"] for f in plan["fallbacks"]))

    def test_t16_with_extension_client_uses_events(self):
        w = World()
        c = ReferenceClient(w.server, A)
        self.assertEqual("events", c.plan()["mode"])
        self.assertEqual(PR, [e["name"] for e in c.call("events/list")["events"] if e["name"] == PR][0])


class RevocationTests(unittest.TestCase):
    def test_t17_claim_before_revoke_completes_before_unsubscribe_returns(self):
        w = World(http_timeout_s=10)
        sub, r, url = setup(w)
        w.publish(pr_event(1))
        w.server.fanout.run_once()
        claimed, release = threading.Event(), threading.Event()

        def after_claim(**kw):
            claimed.set()
            release.wait(10)

        w.server.worker.hooks["after_claim"] = after_claim
        t = threading.Thread(target=w.server.worker.tick)
        t.start()
        self.assertTrue(claimed.wait(5))
        done = threading.Event()
        u = threading.Thread(target=lambda: (w.rpc("events/unsubscribe", {
            "name": PR, "arguments": ARGS, "delivery": {"url": url}}), done.set()))
        u.start()
        self.assertFalse(done.wait(0.3))  # blocked on the in-flight attempt
        self.assertEqual("revoked", w.server.store.subscriptions[sub["id"]]["state"])  # already committed
        release.set()
        t.join(5)
        self.assertTrue(done.wait(5))
        u.join(5)
        self.assertEqual(1, len(r.accepted))  # the pre-revoke claim was allowed to complete
        self.assertEqual(0, w.server.worker.inflight[sub["id"]])  # nothing in flight afterwards

    def test_t17_claim_after_revoke_sends_nothing(self):
        w = World()
        sub, r, url = setup(w)
        w.publish(pr_event(1))
        w.server.fanout.run_once()
        w.server.worker.hooks["before_claim"] = lambda **kw: w.server.subs.revoke(sub["id"], "test")
        sent_before = len(w.transport.requests)
        w.server.worker.tick()
        self.assertEqual(sent_before, len(w.transport.requests))
        self.assertEqual([], r.accepted)
        self.assertEqual(0, w.server.worker.inflight.get(sub["id"], 0))

    def test_t18_ack_facts_and_expiry(self):
        w = World()
        sub, r, url = setup(w, ttlMs=60_000)
        self.assertTrue(sub["refreshBefore"])
        w.publish(pr_event(1))
        w.pump()
        row = w.rows(sub["id"], "event")[0]
        self.assertEqual(("delivered", False), (row["state"], row["processed"]))  # 2xx is not processed
        with self.assertRaises(RpcError) as cm:
            w.rpc("mctl/ack_event", {"subscriptionId": sub["id"], "eventId": "github:evt-1"}, auth=B)
        self.assertEqual(NOT_FOUND, cm.exception.code)
        w.rpc("mctl/ack_event", {"subscriptionId": sub["id"], "eventId": "github:evt-1"})
        self.assertTrue(w.rows(sub["id"], "event")[0]["processed"])
        w.clock.advance(61)  # not refreshed before refreshBefore
        w.publish(pr_event(2))
        w.pump()
        self.assertEqual(1, len(r.accepted))
        self.assertEqual(["terminated"], [m["type"] for m in r.control if m["type"] == "terminated"])
        self.assertEqual("revoked", w.server.store.subscriptions[sub["id"]]["state"])

    def test_t18_refresh_extends_expiry(self):
        w = World()
        url, s = "https://a.example/h", secret()
        r = w.recipient(url, s)
        w.subscribe(url, s, ttlMs=60_000)
        w.clock.advance(50)
        res = w.subscribe(url, s, ttlMs=60_000)
        w.clock.advance(50)
        w.publish(pr_event(1))
        w.pump()
        self.assertEqual("active", w.server.store.subscriptions[res["id"]]["state"])


class VerificationTests(unittest.TestCase):
    def test_t19_wrong_or_delayed_echo_fails(self):
        w = World()
        url, s = "https://a.example/h", secret()

        class Wrong(MockRecipient):
            def receive(self, headers, body):
                return Response(200, body=b'{"challenge":"nope"}')

        class Slow(MockRecipient):
            def receive(self, headers, body):
                resp = super().receive(headers, body)
                resp.elapsed = 60.0
                return resp

        for cls in (Wrong, Slow):
            with self.subTest(cls.__name__):
                r = cls(w.clock)
                r.trust(s)
                w.transport.routes[url] = r
                with self.assertRaises(RpcError) as cm:
                    w.subscribe(url, s)
                self.assertEqual(CALLBACK_ENDPOINT_ERROR, cm.exception.code)
                self.assertEqual({}, w.server.store.subscriptions)

    def test_t19_no_delivery_before_activation_and_match_activates(self):
        w = World()
        sub, r, url = setup(w)
        self.assertEqual("active", w.server.store.subscriptions[sub["id"]]["state"])
        self.assertEqual(1, len(r.control))  # the verification envelope
        w.server.store.subscriptions[sub["id"]]["state"] = "pending"
        w.publish(pr_event(1))
        w.pump()
        self.assertEqual([], r.accepted)


class SsrfTests(unittest.TestCase):
    def test_t20_guard_rejections(self):
        w = World()
        for url in ("http://a.example/h", "https://user:pw@a.example/h", "https://93.184.216.34/h",
                    "https://[::1]/h", "https://127.0.0.1/h", "https://a.example:8443/h"):
            with self.subTest(url=url):
                with self.assertRaises(GuardError):
                    w.guard.check(url)
        for addr in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                     "::1", "fd00::1", "fc00::1", "224.0.0.1", "0.0.0.0", "::ffff:10.0.0.1"):
            with self.subTest(addr=addr):
                g = CallbackGuard(lambda h, a=addr: [a])
                with self.assertRaises(GuardError):
                    g.check("https://a.example/h")
        w.guard.check("https://a.example/h")
        for url in ("https://127.0.0.1/h",):
            with self.assertRaises(RpcError) as cm:
                w.subscribe(url, secret())
            self.assertEqual(CALLBACK_ENDPOINT_ERROR, cm.exception.code)

    def test_t20_rebinding_public_at_registration_private_at_delivery(self):
        w = World()
        sub, r, url = setup(w)
        w.dns["a.example"] = ["10.0.0.5"]
        w.publish(pr_event(1))
        w.server.fanout.run_once()
        n = len(w.transport.requests)
        w.server.worker.tick()
        self.assertEqual(n, len(w.transport.requests))  # nothing sent to the private address
        self.assertEqual("retry", w.rows(sub["id"], "event")[0]["state"])

    def test_t20_pinned_ip_used_and_redirects_not_followed(self):
        w = World()
        sub, r, url = setup(w)
        w.publish(pr_event(1))
        w.server.fanout.run_once()
        r.redirect = True
        n = len(w.transport.requests)
        w.server.worker.tick()
        self.assertEqual(n + 1, len(w.transport.requests))  # exactly one request, no follow-up
        self.assertEqual("93.184.216.34", w.transport.requests[-1]["ip"])
        self.assertEqual("retry", w.rows(sub["id"], "event")[0]["state"])


class AuditAndRepoTests(unittest.TestCase):
    def test_t21_audit_has_ids_and_no_content(self):
        w = World()
        sub, r, url = setup(w)
        w.publish(pr_event(1))
        w.pump()
        w.rpc("mctl/ack_event", {"subscriptionId": sub["id"], "eventId": "github:evt-1"})
        by = {a["stage"]: a for a in w.server.store.audit}
        for stage in ("materialized", "delivered", "processed"):
            a = by[stage]
            self.assertEqual("github:evt-1", a["envelope_id"])
            self.assertEqual("github:evt-1", a["correlation_id"])
            self.assertEqual(sub["id"], a["subscription_id"])
            self.assertTrue(a["webhook_id"].startswith("msg_"))
        blob = json.dumps(w.server.store.audit)
        self.assertNotIn("head_sha", blob)
        self.assertNotIn("0000000000000000000000000000000000000001", blob)

    def test_t22_repository_validators_pass(self):
        for cmd in ([sys.executable, "roadmap/scripts/validate.py", "roadmap/spikes/mcp-events-sep-3415.yaml"],
                    [sys.executable, "-m", "unittest", "events.tests.test_envelope_schema"]):
            p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
            self.assertEqual(0, p.returncode, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
