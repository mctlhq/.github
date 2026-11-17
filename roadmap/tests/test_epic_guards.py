"""Negative semantic mutations for the glovo-c2-friends-pilot write gate.

A guard that can only pass is not a guard, so each test breaks exactly one
property of the REAL manifest and asserts the guard turns red with the named
message, after the unmodified manifest is proven green.
"""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import yaml

ROADMAP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROADMAP / "scripts"))

import epic_guards  # noqa: E402
import validate  # noqa: E402

MANIFEST = ROADMAP / "epics" / "glovo-c2-friends-pilot.yaml"
GUARD = epic_guards.GLOVO_C2

DISABLED = "c2-write-disabled-proof"
APPROVAL = "c2-write-approval-proof"
DECISION = "c2-write-gate-decision"


class GlovoWriteGateGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        with MANIFEST.open(encoding="utf-8") as handle:
            self.base = validate._normalise_dates(yaml.safe_load(handle))

    def mutate(self, change) -> list[str]:
        document = copy.deepcopy(self.base)
        items = {i["id"]: i for i in document["spec"]["workItems"]}
        change(document, items)
        return epic_guards.guard_errors(document, GUARD)

    def assertRed(self, errors: list[str], fragment: str) -> None:
        self.assertTrue(
            any(fragment in error for error in errors),
            f"expected a violation containing {fragment!r}, got {errors}",
        )

    def test_real_manifest_is_green(self) -> None:
        self.assertEqual(epic_guards.guard_errors(self.base, GUARD), [])

    def test_real_manifest_passes_validate_py(self) -> None:
        self.assertEqual(validate.semantic_errors(self.base), [])

    def test_deleting_disabled_proof_is_red(self) -> None:
        def change(document, items):
            document["spec"]["workItems"].remove(items[DISABLED])
            for item in items.values():
                if DISABLED in item.get("dependsOn", []):
                    item["dependsOn"].remove(DISABLED)

        errors = self.mutate(change)
        self.assertRed(errors, f"{DISABLED} is missing")

    def test_demoting_disabled_proof_to_optional_is_red(self) -> None:
        errors = self.mutate(lambda d, i: i[DISABLED].update(required=False))
        self.assertRed(errors, f"item {DISABLED} must be required")

    def test_removing_disabled_proof_dependency_on_decision_is_red(self) -> None:
        errors = self.mutate(lambda d, i: i[DISABLED].pop("dependsOn"))
        self.assertRed(errors, f"{DISABLED} must dependsOn {DECISION}")

    def test_unbinding_disabled_proof_from_its_issue_is_red(self) -> None:
        errors = self.mutate(lambda d, i: i[DISABLED].pop("issue"))
        self.assertRed(errors, f"item {DISABLED} must be bound to")

    def test_pre_go_independent_of_disabled_proof_is_red(self) -> None:
        def change(document, items):
            items["c2-go-owner"]["dependsOn"].remove(DISABLED)

        errors = self.mutate(change)
        self.assertRed(errors, f"c2-go-owner must dependsOn {DISABLED}")
        # The live E2E and every later GO lose the proof as well.
        self.assertRed(errors, f"c2-e2e-resilience must depend on {DISABLED}")
        self.assertRed(errors, f"c2-go-10 must depend on {DISABLED}")

    def test_e2e_detached_from_pre_go_is_red(self) -> None:
        errors = self.mutate(lambda d, i: i["c2-e2e-resilience"].pop("dependsOn"))
        self.assertRed(errors, "c2-e2e-resilience must depend on c2-go-owner")

    def test_deleting_approval_proof_is_red(self) -> None:
        def change(document, items):
            document["spec"]["workItems"].remove(items[APPROVAL])
            for item in items.values():
                if APPROVAL in item.get("dependsOn", []):
                    item["dependsOn"].remove(APPROVAL)

        errors = self.mutate(change)
        self.assertRed(errors, f"{APPROVAL} is missing")

    def test_unbinding_approval_proof_is_red(self) -> None:
        errors = self.mutate(lambda d, i: i[APPROVAL].pop("issue"))
        self.assertRed(errors, f"item {APPROVAL} must be bound to")

    def test_dropping_approval_externals_is_red(self) -> None:
        errors = self.mutate(lambda d, i: i[APPROVAL].pop("externalDependsOn"))
        self.assertRed(errors, "must list external mctlhq/mctl-api#1011")

    def test_write_enablement_without_approval_dependency_is_red(self) -> None:
        errors = self.mutate(
            lambda d, i: i["c2-go-20"]["dependsOn"].remove(APPROVAL)
        )
        self.assertRed(errors, f"c2-go-20 must depend on {APPROVAL}")

    def test_new_write_item_without_approval_dependency_is_red(self) -> None:
        def change(document, items):
            document["spec"]["workItems"].append(
                {
                    "id": "c2-enable-write-tools",
                    "title": "Turn the write tools on",
                    "owner": "mctl-glovo",
                    "phase": "safety",
                    "required": False,
                    "dependsOn": [DECISION],
                }
            )

        errors = self.mutate(change)
        self.assertRed(
            errors, f"write-related item c2-enable-write-tools must depend on {APPROVAL}"
        )

    def test_new_write_item_with_approval_dependency_is_green(self) -> None:
        def change(document, items):
            document["spec"]["workItems"].append(
                {
                    "id": "c2-enable-write-tools",
                    "title": "Turn the write tools on",
                    "owner": "mctl-glovo",
                    "phase": "safety",
                    "required": False,
                    "dependsOn": [APPROVAL],
                }
            )

        self.assertEqual(self.mutate(change), [])

    def test_required_depending_on_optional_is_red(self) -> None:
        errors = self.mutate(
            lambda d, i: i["c2-go-2"]["dependsOn"].append("c2-cohort-feedback")
        )
        self.assertRed(errors, "required item c2-go-2 depends on optional c2-cohort-feedback")

    def test_required_depending_on_approval_proof_is_red(self) -> None:
        errors = self.mutate(
            lambda d, i: i["c2-go-owner"]["dependsOn"].append(APPROVAL)
        )
        self.assertRed(errors, f"required item c2-go-owner must not depend on {APPROVAL}")

    def test_disabled_proof_taking_approval_externals_is_red(self) -> None:
        def change(document, items):
            items[DISABLED]["externalDependsOn"] = [
                {"repository": "mctlhq/mctl-api", "number": 1011}
            ]

        errors = self.mutate(change)
        self.assertRed(errors, "must not depend on approval external mctlhq/mctl-api#1011")

    def test_dependency_cycle_is_red(self) -> None:
        errors = self.mutate(
            lambda d, i: i[DECISION].update(dependsOn=["c2-go-owner"])
        )
        self.assertRed(errors, "graph invalid: dependency graph contains cycle")

    def test_unknown_dependency_id_is_red(self) -> None:
        errors = self.mutate(
            lambda d, i: i["c2-go-owner"]["dependsOn"].append("c2-no-such-item")
        )
        self.assertRed(errors, "unknown dependsOn target 'c2-no-such-item'")


class GuardCliTest(unittest.TestCase):
    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = epic_guards.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_real_corpus_exits_zero(self) -> None:
        code, out, _ = self._run(str(ROADMAP / "epics"))
        self.assertEqual(code, 0, out)

    def test_missing_guarded_epic_exits_two(self) -> None:
        # Removing the manifest must never read as "clean".
        with tempfile.TemporaryDirectory() as directory:
            other = ROADMAP / "epics" / "human-input.yaml"
            Path(directory, "other.yaml").write_text(
                other.read_text(encoding="utf-8"), encoding="utf-8"
            )
            code, _, err = self._run(directory)
        self.assertEqual(code, 2, err)
        self.assertIn("not found in the corpus", err)

    def test_violation_exits_one(self) -> None:
        document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        for item in document["spec"]["workItems"]:
            if item["id"] == DISABLED:
                item["required"] = False
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "glovo.yaml").write_text(
                yaml.safe_dump(document), encoding="utf-8"
            )
            code, out, _ = self._run(directory)
        self.assertEqual(code, 1, out)
        self.assertIn("must be required", out)

    def test_unparseable_manifest_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "broken.yaml").write_text("a: [unclosed", encoding="utf-8")
            code, _, err = self._run(directory)
        self.assertEqual(code, 2, err)


if __name__ == "__main__":
    unittest.main()
