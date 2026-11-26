"""Mutation tests for verdict_consistency (C1-C3). Run: python -I -m unittest discover -s checks"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import verdict_consistency as vc  # noqa: E402
import yaml  # noqa: E402

TEXT = vc.REPORT.read_text()
MAN = yaml.safe_load(vc.MANIFEST.read_text())


class VerdictConsistency(unittest.TestCase):
    def test_current_files_pass(self):
        self.assertEqual(vc.check(TEXT, MAN), [])

    def test_c1_dropped_mapping_fails(self):
        mutated = "\n".join(l for l in TEXT.splitlines() if not l.startswith("| `DEFER`"))
        self.assertTrue(any("C1" in e for e in vc.check(mutated, MAN)))

    def test_c2_target_not_in_manifest_fails(self):
        mutated = TEXT.replace("| `BOUNDED_PILOT` | `GO_ADAPTER_IN_MCTL_API`", "| `BOUNDED_PILOT` | `BOUNDED_PILOT`")
        self.assertTrue(any("C2" in e for e in vc.check(mutated, MAN)))

    def test_c3_decision_line_without_manifest_decision_fails(self):
        self.assertTrue(any("C3" in e for e in vc.check(TEXT + "\nDecision: GO\n", MAN)))
        decided = {"spec": {**MAN["spec"], "decision": {"verdict": "NO_GO"}}}
        self.assertEqual(vc.check(TEXT + "\nDecision: GO\n", decided), [])


if __name__ == "__main__":
    unittest.main()
