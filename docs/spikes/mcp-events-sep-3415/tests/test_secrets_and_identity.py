import base64
import json
import unittest

from common import A, PR, World, pr_event, secret
from model import webhooks
from model.errors import INVALID_PARAMS, RpcError


class SecretTests(unittest.TestCase):
    def test_t3_invalid_secrets_are_invalid_params(self):
        w = World()
        url = "https://hook.example/a"
        w.recipient(url, secret())
        bad = [None, "", "whsec_" + base64.b64encode(b"x" * 23).decode(), "plain",
               "whsec_" + base64.b64encode(b"x" * 65).decode(), "whsec_!!notbase64"]
        for value in bad:
            with self.subTest(value=str(value)[:12]):
                with self.assertRaises(RpcError) as cm:
                    w.subscribe(url, value)
                self.assertEqual(INVALID_PARAMS, cm.exception.code)
                self.assertNotIn(str(value), json.dumps(cm.exception.payload())) if value else None
        self.assertEqual({}, w.server.store.subscriptions)
        for n in (24, 64):
            webhooks.validate_secret(secret(n))
        self.assertFalse(hasattr(webhooks, "generate_secret"))  # no server-generated secret

    def test_t4_refresh_dual_signs_then_only_new(self):
        w = World(secret_grace_s=300)
        url, old, new = "https://hook.example/a", secret(fill=1), secret(fill=2)
        r = w.recipient(url, old)
        sub = w.subscribe(url, old)
        w.publish(pr_event(1))
        w.pump()
        w.subscribe(url, new)  # client-initiated refresh with a new secret
        r.trust(new)
        w.publish(pr_event(2))
        w.server.fanout.run_once()
        w.server.worker.tick()
        last = w.transport.requests[-1]
        self.assertEqual(2, len(last["headers"]["webhook-signature"].split(" ")))
        self.assertTrue(webhooks.verify(old, last["headers"], last["body"]))
        self.assertTrue(webhooks.verify(new, last["headers"], last["body"]))
        w.clock.advance(301)  # grace over
        w.publish(pr_event(3))
        w.server.fanout.run_once()
        w.server.worker.tick()
        last = w.transport.requests[-1]
        self.assertEqual(1, len(last["headers"]["webhook-signature"].split(" ")))
        self.assertTrue(webhooks.verify(new, last["headers"], last["body"]))
        self.assertFalse(webhooks.verify(old, last["headers"], last["body"]))
        self.assertEqual(3, len(r.accepted))
        self.assertEqual(sub["id"], r.accepted[0]["subscription_id"])

    def test_t5_no_secret_in_any_record(self):
        w = World()
        url, s = "https://hook.example/a", secret(fill=9)
        w.recipient(url, s)
        w.subscribe(url, s)
        with self.assertRaises(RpcError):
            w.subscribe(url, "whsec_short")
        w.publish(pr_event(1))
        w.pump()
        body = s[len("whsec_"):]
        blob = json.dumps([w.server.store.audit, w.server.log, w.server.store.deliveries,
                           [{"h": r["headers"], "b": r["body"].decode()} for r in w.transport.requests]])
        self.assertNotIn(s, blob)
        self.assertNotIn(body, blob)
        self.assertIn(webhooks.fingerprint(s), blob)  # only the fingerprint is recorded


class WebhookIdentityTests(unittest.TestCase):
    def test_t6_two_subscriptions_one_event(self):
        w = World()
        s1, s2 = secret(fill=1), secret(fill=2)
        u1, u2 = "https://one.example/h", "https://two.example/h"
        r1, r2 = w.recipient(u1, s1), w.recipient(u2, s2)
        a, b = w.subscribe(u1, s1), w.subscribe(u2, s2)
        w.publish(pr_event(1))
        w.pump()
        self.assertEqual(1, len(r1.accepted))
        self.assertEqual(1, len(r2.accepted))
        self.assertEqual(r1.accepted[0]["eventId"], r2.accepted[0]["eventId"])
        self.assertNotEqual(r1.accepted[0]["webhook_id"], r2.accepted[0]["webhook_id"])
        self.assertEqual([], r1.rejected + r2.rejected)

    def test_t7_retry_identity_and_headers(self):
        w = World(max_attempts=5)
        url, s = "https://hook.example/a", secret()
        r = w.recipient(url, s)
        sub = w.subscribe(url, s)
        w.publish(pr_event(1))
        w.server.fanout.run_once()
        r.force_status = 500
        w.server.worker.tick()
        w.clock.advance(5)
        w.server.worker.tick()
        r.force_status = None
        w.clock.advance(30)
        w.server.worker.tick()
        reqs = [q for q in w.transport.requests if json.loads(q["body"]).get("eventId") == "github:evt-1"]
        self.assertEqual(3, len(reqs))
        self.assertEqual(1, len({q["headers"]["webhook-id"] for q in reqs}))
        self.assertEqual(3, len({q["headers"]["webhook-timestamp"] for q in reqs}))
        self.assertEqual(3, len({q["headers"]["webhook-signature"] for q in reqs}))
        for q in w.transport.requests:
            self.assertNotEqual("github:evt-1", q["headers"]["webhook-id"])
            self.assertEqual(sub["id"], q["headers"]["X-MCP-Subscription-Id"])
        ctrl = [q for q in w.transport.requests if json.loads(q["body"]).get("type") == "verification"]
        self.assertEqual(1, len(ctrl))
        self.assertNotIn(ctrl[0]["headers"]["webhook-id"], {q["headers"]["webhook-id"] for q in reqs})

    def test_t8_replay_and_spoofing(self):
        w = World()
        url, s = "https://hook.example/a", secret()
        r = w.recipient(url, s)
        w.subscribe(url, s)
        w.publish(pr_event(1))
        w.pump()
        req = w.transport.requests[-1]
        n = len(r.accepted)
        self.assertEqual(200, r.receive(req["headers"], req["body"]).status)  # replay: no second effect
        self.assertEqual(n, len(r.accepted))
        self.assertIn("duplicate_webhook_id", r.rejected)
        fresh = dict(req["headers"], **{"webhook-id": "msg_other"})
        self.assertEqual(401, r.receive(fresh, req["body"]).status)  # signature covers the id
        stale = req["headers"]
        w.clock.advance(301)
        self.assertEqual(400, r.receive(dict(stale, **{"webhook-id": "msg_new"}), req["body"]).status)
        self.assertEqual(400, r.receive({"webhook-id": "x"}, req["body"]).status)
        w.clock.advance(-301)
        bad = dict(req["headers"], **{"webhook-id": "msg_zzz", "webhook-signature": "v1,AAAA"})
        self.assertEqual(401, r.receive(bad, req["body"]).status)

    def test_t8_secret_past_grace_rejected(self):
        w = World(secret_grace_s=10)
        url, old, new = "https://hook.example/a", secret(fill=1), secret(fill=2)
        r = w.recipient(url, new)
        w.recipient(url, old)  # recipient trusts only old at first
        r.trust(new)
        w.transport.routes[url] = r
        r.secrets.clear()
        r.default_secrets = [old]
        w.subscribe(url, old)
        w.subscribe(url, new)
        w.clock.advance(11)
        w.publish(pr_event(1))
        w.server.fanout.run_once()
        w.server.worker.tick()
        self.assertEqual([], r.accepted)  # only the new secret signs now; old-only recipient rejects
        self.assertIn("bad_signature", r.rejected)


if __name__ == "__main__":
    unittest.main()
