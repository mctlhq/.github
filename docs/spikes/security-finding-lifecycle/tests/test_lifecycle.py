import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import model  # noqa: E402

FX = os.path.join(os.path.dirname(HERE), "fixtures")
FIX_SHA = "2" * 40
DESC_SHA = "3" * 40


def fx(name, **over):
    with open(os.path.join(FX, name)) as fh:
        d = json.load(fh)
    d.update(over)
    return d


def regression(n):
    return fx("trivy-cve-observation.json", delivery_id="trivy-reg-%d" % n, revision=str(n + 2) * 40)


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.m = model.Model()
        self.f = self.m.ingest(fx("github-code-scanning-created.json"))
        self.k = self.f.key

    def to_fixing(self):
        self.m.triage(self.k, "alice")
        self.m.link_remediation(self.k, "wi_1")

    def to_resolved(self):
        self.to_fixing()
        self.m.record_merge(self.k, FIX_SHA)
        self.m.record_verification(self.k, FIX_SHA, "scanner_rerun_exact_revision", "absent")

    def test_t1_full_path(self):
        self.assertEqual(self.f.state, "open")
        self.assertEqual(self.f.vulnerable_rev, "1" * 40)
        self.m.triage(self.k, "alice")
        self.assertEqual(self.f.state, "triaged")
        self.m.link_remediation(self.k, "wi_1")
        self.assertEqual(self.f.state, "fixing")
        self.m.record_merge(self.k, FIX_SHA)
        self.assertEqual(self.f.state, "awaiting_verification")
        self.m.record_verification(self.k, FIX_SHA, "scanner_rerun_exact_revision", "absent")
        self.assertEqual(self.f.state, "resolved")
        fid = self.f.id
        self.m.ingest(regression(1))
        self.assertEqual(self.f.state, "resolved")
        self.m.ingest(regression(2))
        self.assertEqual(self.f.state, "reopened")
        self.assertEqual(self.f.id, fid)
        self.m.link_remediation(self.k, "wi_1")  # reuses the single WorkItem
        self.assertEqual(self.f.state, "fixing")
        self.m.record_merge(self.k, "5" * 40)
        self.assertEqual(self.f.state, "awaiting_verification")
        self.m.record_verification(self.k, "5" * 40, "scanner_rerun_exact_revision", "absent")
        self.assertEqual(self.f.state, "resolved")

    def test_stale_observations_do_not_reopen(self):
        self.to_resolved()
        for n in (1, 2):
            self.m.ingest(fx("trivy-cve-observation.json", delivery_id="stale-%d" % n, revision="1" * 40))
        self.assertEqual(self.f.state, "resolved")
        for n in (3, 4):  # same descendant revision twice counts once
            self.m.ingest(fx("trivy-cve-observation.json", delivery_id="stale-%d" % n, revision=DESC_SHA))
        self.assertEqual(self.f.state, "resolved")

    def test_t2_duplicate_delivery(self):
        for _ in range(3):
            f = self.m.ingest(fx("github-code-scanning-created.json"))
        self.assertEqual(len(self.m.findings), 1)
        self.assertEqual(len(f.observations), 1)
        self.m.link_remediation(self.k, "wi_1")
        self.assertEqual(f.remediation, "wi_1")

    def test_t3_cross_provider_dedup(self):
        f = self.m.ingest(fx("trivy-cve-observation.json"))
        self.assertIs(f, self.f)
        self.assertEqual(len(self.m.findings), 1)
        self.assertEqual(len(f.observations), 2)
        self.m.link_remediation(self.k, "wi_1")
        with self.assertRaises(model.LifecycleError):
            self.m.link_remediation(self.k, "wi_2")

    def test_t4_merged_is_not_resolved(self):
        self.to_fixing()
        self.m.record_merge(self.k, FIX_SHA)
        self.assertEqual(self.f.state, "awaiting_verification")

    def test_t5_unknown_is_not_absent(self):
        self.to_fixing()
        self.m.record_merge(self.k, FIX_SHA)
        for result in ("error", "unavailable"):
            self.m.record_verification(self.k, FIX_SHA, "scanner_rerun_exact_revision", result)
            self.assertEqual(self.f.state, "unverifiable")
        self.m.record_verification(self.k, FIX_SHA, "scanner_rerun_exact_revision", "absent")
        self.assertEqual(self.f.state, "resolved")

    def test_t5b_verification_bound_to_landed_revision(self):
        self.to_fixing()
        self.m.record_merge(self.k, FIX_SHA)
        with self.assertRaises(model.LifecycleError):
            self.m.record_verification(self.k, "4" * 40, "scanner_rerun_exact_revision", "absent")
        self.assertEqual(self.f.state, "awaiting_verification")

    def test_t6_provider_claim_does_not_resolve(self):
        self.to_fixing()
        self.m.record_merge(self.k, FIX_SHA)
        self.m.ingest(fx("github-code-scanning-fixed.json"))
        self.assertEqual(self.f.state, "awaiting_verification")
        self.assertEqual(self.f.evidence[-1]["strength"], "provider_claim")
        self.m.record_verification(self.k, FIX_SHA, "provider_claim", "absent")
        self.assertEqual(self.f.state, "awaiting_verification")

    def test_t7_dismissal_and_flapping(self):
        with self.assertRaises(model.LifecycleError):
            self.m.dismiss(self.k, "", "auth-1", "accepted_risk")
        with self.assertRaises(model.LifecycleError):
            self.m.dismiss(self.k, "bob", "auth-1", "because")
        self.m.dismiss(self.k, "bob", "auth-1", "accepted_risk")
        self.assertEqual(self.f.state, "dismissed")
        self.m.ingest(regression(1))
        self.m.ingest(regression(2))
        self.assertEqual(self.f.state, "dismissed")
        self.assertEqual(len(self.f.successors), 2)

    def test_t7b_deterministic_test_reopens_at_once(self):
        self.to_resolved()
        self.m.ingest(fx("assurance-confirmed-replay.json"))  # different key, no effect
        self.assertEqual(self.f.state, "resolved")
        self.m.ingest(fx("assurance-confirmed-replay.json", vuln_class_id="CVE-2025-54236",
                         component="github.com/example/pgx", delivery_id="afd-x", revision=DESC_SHA))
        self.assertEqual(self.f.state, "reopened")

    def test_t8_untrusted_text(self):
        f = self.m.ingest(fx("claude-security-hypothetical.json"))
        self.assertIs(f, self.f)
        self.assertEqual(f.state, "open")
        self.assertIsNone(f.dismissal)
        self.assertEqual(f.untrusted[-1]["severity"], "critical")


if __name__ == "__main__":
    unittest.main()
