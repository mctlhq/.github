import unittest

from common import A, PR, World, pr_event

ARGS = {"repository": "mctlhq/.github"}


def poll(w, **kw):
    return w.rpc("events/poll", {"name": PR, "arguments": ARGS, **kw})


class CursorTests(unittest.TestCase):
    def test_t9_null_cursor_starts_now_and_ignores_max_age(self):
        w = World()
        for i in range(3):
            w.publish(pr_event(i))
        for kw in ({}, {"cursor": None}, {"cursor": None, "maxAgeMs": 10**9}):
            with self.subTest(kw=kw):
                r = poll(w, **kw)
                self.assertEqual([], r["events"])
                self.assertFalse(r["truncated"])
                nxt = poll(w, cursor=r["cursor"])
                self.assertEqual([], nxt["events"])
        c = poll(w)["cursor"]
        w.publish(pr_event(10))
        self.assertEqual(["github:evt-10"], [e["eventId"] for e in poll(w, cursor=c)["events"]])

    def test_t10_gap_is_truncated_not_an_error(self):
        w = World()
        w.server.stream.maxlen = 5
        c = poll(w)["cursor"]
        for i in range(12):
            w.publish(pr_event(i))
        r = poll(w, cursor=c, maxEvents=100)  # no JSON-RPC error
        self.assertTrue(r["truncated"])
        self.assertEqual([f"github:evt-{i}" for i in range(7, 12)], [e["eventId"] for e in r["events"]])
        again = poll(w, cursor=r["cursor"])
        self.assertFalse(again["truncated"])
        self.assertIn("below_retention", [a["stage"] for a in w.server.store.audit])  # internal only

    def test_t10_max_age_later_than_cursor_truncates(self):
        w = World()
        c = poll(w)["cursor"]
        w.publish(pr_event(1))
        w.clock.advance(100)
        w.publish(pr_event(2))
        r = poll(w, cursor=c, maxAgeMs=50_000)
        self.assertTrue(r["truncated"])
        self.assertEqual(["github:evt-2"], [e["eventId"] for e in r["events"]])
        r = poll(w, cursor=c, maxAgeMs=500_000)
        self.assertFalse(r["truncated"])
        self.assertEqual(2, len(r["events"]))

    def test_t10_stream_activation_reports_truncated(self):
        w = World()
        w.server.stream.maxlen = 2
        c = poll(w)["cursor"]
        for i in range(5):
            w.publish(pr_event(i))
        act = w.rpc("events/stream", {"name": PR, "arguments": ARGS, "cursor": c})
        self.assertTrue(act["active"]["truncated"])

    def test_t10_webhook_subscribe_with_old_cursor_sends_gap(self):
        from common import secret
        w = World()
        w.server.stream.maxlen = 3
        c = poll(w)["cursor"]
        for i in range(8):
            w.publish(pr_event(i))
        url, s = "https://hook.example/a", secret()
        r = w.recipient(url, s)
        res = w.subscribe(url, s, cursor=c)
        self.assertTrue(res["truncated"])
        w.server.worker.run_until_idle()
        self.assertEqual(["gap"], [m["type"] for m in r.control if m["type"] == "gap"])
        self.assertEqual(3, len(r.accepted))

    def test_t11_pagination_exactly_once_in_order(self):
        w = World()
        c = poll(w)["cursor"]
        for i in range(25):
            w.publish(pr_event(i))
        seen, pages = [], 0
        while True:
            r = poll(w, cursor=c, maxEvents=10)
            seen += [e["eventId"] for e in r["events"]]
            c, pages = r["cursor"], pages + 1
            if not r["hasMore"]:
                break
        self.assertEqual([f"github:evt-{i}" for i in range(25)], seen)
        self.assertEqual(3, pages)


if __name__ == "__main__":
    unittest.main()
