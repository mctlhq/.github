import json
import unittest

from common import A, B, PR, REPO, ROOT, World, fixture, pr_event, secret, tenant_of
from model.projection import InvalidEnvelope, project
from model.server import Policy


class ProjectionTests(unittest.TestCase):
    def test_t1_projection_references_only(self):
        for name in ("synthetic-devloop-proposal-ready.json", "github-pull-request-opened.json"):
            env = fixture(name)
            occ = project(env)
            self.assertEqual(env["id"], occ["eventId"])
            self.assertEqual(env["type"], occ["name"])
            self.assertEqual(env["occurred_at"], occ["timestamp"])
            self.assertEqual({"source", "correlation_id", "subject"}, set(occ["data"]))
            self.assertNotIn("title", json.dumps(occ))
        for p in (REPO / "events" / "examples" / "valid").glob("*.json"):
            project(json.loads(p.read_text()))

    def test_t1_body_field_rejected_audited_acked_never_delivered(self):
        w = World()
        url = "https://hook.example/a"
        s = secret()
        w.recipient(url, s)
        res = w.subscribe(url, s)
        bad = json.loads((REPO / "events" / "examples" / "invalid" / "body-field.json").read_text())
        with self.assertRaises(InvalidEnvelope):
            project(bad)
        w.publish(bad)
        w.pump()
        stages = [a["stage"] for a in w.server.store.audit]
        self.assertIn("rejected_envelope", stages)
        self.assertEqual(0, w.server.stream.pending_count("mcp-events-relay"))
        self.assertEqual([], w.rows(res["id"], "event"))
        self.assertEqual([], w.recipients[url].accepted)

    def test_t2_cross_tenant_never_materializes(self):
        # permissive authorize and empty arguments: only materialization-time tenancy protects
        w = World(policy=Policy(lambda *_: True, tenant_of))
        url, s = "https://hook.example/a", secret()
        w.recipient(url, s)
        res = w.subscribe(url, s, args={}, auth=A)
        w.publish(pr_event(1, repo="acme/widgets"))  # tenant B
        noten = pr_event(2)
        noten["subject"]["repository"] = "unbound/repo"  # no tenant
        w.publish(noten)
        w.publish(pr_event(3))  # tenant A
        w.pump()
        self.assertEqual(["github:evt-3"], [d["event_id"] for d in w.rows(res["id"], "event")])

    def test_t2_poll_is_tenant_scoped(self):
        w = World()
        w.publish(pr_event(1))
        w.publish(pr_event(2, repo="acme/widgets"))
        start = w.rpc("events/poll", {"name": PR, "arguments": {"repository": "mctlhq/.github"}})
        w.publish(pr_event(3))
        w.publish(pr_event(4, repo="acme/widgets"))
        got = w.rpc("events/poll", {"name": PR, "arguments": {"repository": "mctlhq/.github"},
                                    "cursor": start["cursor"]})
        self.assertEqual(["github:evt-3"], [e["eventId"] for e in got["events"]])
        from model.errors import FORBIDDEN, RpcError
        with self.assertRaises(RpcError) as cm:
            w.rpc("events/poll", {"name": PR, "arguments": {"repository": "acme/widgets"}}, auth=A)
        self.assertEqual(FORBIDDEN, cm.exception.code)
        with self.assertRaises(RpcError) as cm:
            w.rpc("events/poll", {"name": PR, "arguments": {"repository": "acme/widgets"}}, auth=None)
        self.assertEqual(FORBIDDEN, cm.exception.code)


if __name__ == "__main__":
    unittest.main()
