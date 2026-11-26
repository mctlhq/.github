import json
import unittest

from common import A, PR, World, pr_event, secret
from model.errors import Crash

G = "mcp-events-relay"


def setup(w, name="a", fill=1, **kw):
    url, s = f"https://{name}.example/h", secret(fill=fill)
    r = w.recipient(url, s)
    return w.subscribe(url, s, **kw), r, url


class CrashTests(unittest.TestCase):
    def test_t12_crash_before_commit_rematerializes(self):
        w = World()
        (s1, r1, _), (s2, r2, _) = setup(w, "a", 1), setup(w, "b", 2)
        w.publish(pr_event(1))
        w.server.store.crash_points.add("before_commit")
        with self.assertRaises(Crash):
            w.server.fanout.run_once()
        self.assertEqual(1, w.server.stream.pending_count(G))
        self.assertEqual([], w.rows(s1["id"], "event"))
        w.pump()  # XAUTOCLAIM re-materializes
        self.assertEqual(0, w.server.stream.pending_count(G))
        for s in (s1, s2):
            self.assertEqual(1, len(w.rows(s["id"], "event")))
        self.assertEqual((1, 1), (len(r1.accepted), len(r2.accepted)))

    def test_t13_crash_after_commit_before_xack(self):
        w = World()
        s1, r1, _ = setup(w)
        w.publish(pr_event(1))
        w.server.store.crash_points.add("after_commit")
        with self.assertRaises(Crash):
            w.server.fanout.run_once()
        self.assertEqual(1, w.server.stream.pending_count(G))
        self.assertEqual(1, len(w.rows(s1["id"], "event")))
        w.pump()
        self.assertEqual(1, len(w.rows(s1["id"], "event")))  # nothing new inserted
        self.assertEqual(0, w.server.stream.pending_count(G))
        self.assertEqual(1, len(r1.accepted))

    def test_t14_crash_after_post_resends_same_webhook_id(self):
        w = World()
        w.server.worker.hooks["after_post"] = lambda **kw: (_ for _ in ()).throw(Crash("after_post"))
        s1, r1, _ = setup(w)
        w.publish(pr_event(1))
        w.server.fanout.run_once()
        with self.assertRaises(Crash):
            w.server.worker.tick()
        self.assertEqual(1, len(r1.accepted))  # the POST landed; the row was not marked
        self.assertEqual("inflight", w.rows(s1["id"], "event")[0]["state"])
        w.server.worker.hooks.clear()
        w.clock.advance(10)
        w.server.worker.run_until_idle()
        reqs = [q for q in w.transport.requests if json.loads(q["body"]).get("eventId") == "github:evt-1"]
        self.assertEqual(2, len(reqs))
        self.assertEqual(1, len({q["headers"]["webhook-id"] for q in reqs}))
        self.assertEqual(1, len(r1.accepted))  # recipient deduped
        self.assertEqual("delivered", w.rows(s1["id"], "event")[0]["state"])


class IndependenceTests(unittest.TestCase):
    def test_t15_failing_subscriber_does_not_block_another(self):
        w = World(max_attempts=3, base_backoff_s=1, cap_backoff_s=4)
        s1, r1, _ = setup(w, "bad", 1)
        s2, r2, _ = setup(w, "good", 2)
        r1.force_status = 500
        for i in range(5):
            w.publish(pr_event(i))
        w.pump()
        self.assertEqual([f"github:evt-{i}" for i in range(5)], [m["eventId"] for m in r2.accepted])
        self.assertEqual(0, w.server.stream.pending_count(G))
        states = {d["state"] for d in w.rows(s1["id"], "event")}
        self.assertEqual({"dead_letter"}, states)
        self.assertEqual(5, sum(1 for a in w.server.store.audit
                                if a["stage"] == "dead_letter" and a["subscription_id"] == s1["id"]))

    def test_t15_410_deactivates_and_413_is_final(self):
        w = World()
        s1, r1, _ = setup(w, "gone", 1)
        s2, r2, _ = setup(w, "big", 2)
        r1.force_status, r2.force_status = 410, 413
        w.publish(pr_event(1))
        w.pump()
        self.assertEqual("revoked", w.server.store.subscriptions[s1["id"]]["state"])
        self.assertEqual("failed_final", w.rows(s1["id"], "event")[0]["state"])
        self.assertEqual("failed_final", w.rows(s2["id"], "event")[0]["state"])
        self.assertEqual(1, sum(1 for q in w.transport.requests if q["url"].startswith("https://big")
                                and json.loads(q["body"]).get("eventId")))  # 413 not retried

    def test_t15_revoked_mid_run_other_continues(self):
        w = World()
        s1, r1, u1 = setup(w, "a", 1)
        s2, r2, _ = setup(w, "b", 2)
        w.publish(pr_event(1))
        w.server.fanout.run_once()
        w.rpc("events/unsubscribe", {"name": PR, "arguments": {"repository": "mctlhq/.github"},
                                     "delivery": {"url": u1}})
        w.server.worker.run_until_idle()
        self.assertEqual([], r1.accepted)
        self.assertEqual(1, len(r2.accepted))

    def test_t15_quota_pauses_one_subscription_then_gap(self):
        w = World(per_subscription_quota=2, max_attempts=50)
        s1, r1, _ = setup(w, "slow", 1)
        s2, r2, _ = setup(w, "fast", 2)
        r1.force_status = 500
        for i in range(6):
            w.publish(pr_event(i))
            w.server.fanout.run_once()
            w.server.worker.tick()
        self.assertEqual(6, len(r2.accepted))  # others unaffected
        self.assertEqual(2, len(w.rows(s1["id"], "event")))  # no more than quota materialized
        self.assertIsNotNone(w.server.store.subscriptions[s1["id"]]["gap_from"])
        r1.force_status = None
        w.clock.advance(100)
        w.server.worker.run_until_idle()
        w.publish(pr_event(100))
        w.pump()
        self.assertEqual(1, len(w.rows(s1["id"], "gap")))
        self.assertEqual(["gap"], [m["type"] for m in r1.control if m["type"] == "gap"])
        self.assertEqual(3, len(r1.accepted))  # 2 retried + the new one; evt-2..5 are the gap


if __name__ == "__main__":
    unittest.main()
