from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROADMAP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROADMAP / "scripts"))
sys.path.insert(0, str(ROADMAP / "tests"))

import completion  # noqa: E402
import github_graph  # noqa: E402
import health  # noqa: E402
import mutations  # noqa: E402
import reconcile  # noqa: E402

EPIC_66 = ROADMAP / "epics" / "roadmap-control-plane.yaml"
CAPTURE_66 = ROADMAP / "fixtures" / "roadmap-control-plane" / "live-capture.json"

RECONCILER = "mctlhq/.github#67"
HEALTH = "mctlhq/.github#83"
APPLY = "mctlhq/.github#68"
TEMPORAL = "mctlhq/.github#85"


def _set_state(snapshot: dict, issue: str, state: str, reason: str | None) -> dict:
    result = copy.deepcopy(snapshot)
    for observation in result["issues"]:
        if observation["requested"] == mutations.ref(issue):
            observation["state"] = state
            observation.pop("stateReason", None)
            if reason is not None:
                observation["stateReason"] = reason
    return result


class CompletionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.safe_load(EPIC_66.read_text(encoding="utf-8"))
        cls.capture = json.loads(CAPTURE_66.read_text(encoding="utf-8"))
        cls.loaded = reconcile.LoadedManifest(
            document=cls.document,
            sha256=__import__("hashlib").sha256(EPIC_66.read_bytes()).hexdigest(),
        )
        cls.validator = Draft202012Validator(
            json.loads((ROADMAP / "schemas" / "roadmap-health.schema.json").read_text())
        )

    def _compute(self, snapshot: dict, document: dict | None = None, unobserved=frozenset()):
        return completion.compute(
            document or self.document, github_graph.observed_graph(snapshot), unobserved
        )

    def _assess(self, snapshot: dict) -> dict:
        return health.assess(EPIC_66, self.loaded, github_graph.FixtureGraphSource(snapshot))

    def _item(self, block: dict, item_id: str) -> dict:
        return next(item for item in block["items"] if item["id"] == item_id)

    # -- dogfood: epic #66 as captured from live GitHub ----------------------

    def test_capture_is_immutable_live_evidence(self) -> None:
        self.assertEqual("live-capture", self.capture["source"]["mode"])
        self.assertEqual([], github_graph.snapshot_errors(self.capture))

    def test_state_reason_is_optional_on_found_and_forbidden_on_not_found(self) -> None:
        """Open issues have no state_reason; a not-found observation may carry none."""

        open_without_reason = [
            item for item in self.capture["issues"]
            if item.get("state") == "open" and "stateReason" not in item
        ]
        self.assertTrue(open_without_reason)
        self.assertEqual([], github_graph.snapshot_errors(self.capture))

        forged = mutations.mark_missing(self.capture, APPLY)
        for observation in forged["issues"]:
            if observation["requested"] == mutations.ref(APPLY):
                observation["stateReason"] = "completed"
        self.assertNotEqual([], github_graph.snapshot_errors(forged))

    def test_epic_66_is_healthy_and_blocked_only_by_governed_apply(self) -> None:
        result = self._assess(self.capture)
        self.assertEqual("healthy", result["state"])
        self.assertEqual([], result["diagnostics"])

        block = result["completion"]
        self.assertEqual("incomplete", block["status"])
        self.assertEqual(["governed-apply"], block["blocking"])
        self.assertEqual({"total": 3, "complete": 2, "incomplete": 1, "unknown": 0}, block["required"])
        self.assertEqual([], [e.message for e in self.validator.iter_errors(result)])

    def test_open_optional_item_does_not_hold_completion_back(self) -> None:
        """#85 is open and required: false. Close only the required blocker."""

        snapshot = _set_state(self.capture, APPLY, "closed", "completed")
        block = self._compute(snapshot)
        temporal = self._item(block, "temporal-health")
        self.assertFalse(temporal["required"])
        self.assertEqual("incomplete", temporal["status"])
        self.assertEqual("complete", block["status"])
        self.assertEqual([], block["blocking"])

    def test_the_required_flag_is_what_decides(self) -> None:
        """Same graph, #85 made required: now it blocks."""

        document = copy.deepcopy(self.document)
        for item in document["spec"]["workItems"]:
            if item["id"] == "temporal-health":
                item["required"] = True
        snapshot = _set_state(self.capture, APPLY, "closed", "completed")
        block = self._compute(snapshot, document)
        self.assertEqual("incomplete", block["status"])
        self.assertEqual(["temporal-health"], block["blocking"])

    # -- item terminal conditions -------------------------------------------

    def test_closed_as_not_planned_or_duplicate_is_not_delivered(self) -> None:
        for reason in ("not_planned", "duplicate"):
            with self.subTest(reason=reason):
                block = self._compute(_set_state(self.capture, HEALTH, "closed", reason))
                item = self._item(block, "roadmap-health")
                self.assertEqual(("incomplete", f"closed_{reason}"), (item["status"], item["reason"]))

    def test_closed_as_completed_or_without_a_reason_is_complete(self) -> None:
        for reason in ("completed", None):
            with self.subTest(reason=reason):
                block = self._compute(_set_state(self.capture, APPLY, "closed", reason))
                self.assertEqual("complete", self._item(block, "governed-apply")["status"])

    def test_closed_for_an_unrecognized_reason_is_unknown_not_complete(self) -> None:
        """Fail closed: a reason that is not evidence of delivery never counts as done."""

        for reason in ("reopened", "some_future_reason"):
            with self.subTest(reason=reason):
                block = self._compute(_set_state(self.capture, APPLY, "closed", reason))
                item = self._item(block, "governed-apply")
                self.assertEqual(("unknown", "closed_reason_unrecognized"), (item["status"], item["reason"]))
                self.assertEqual("unknown", block["status"])
                self.assertIn("governed-apply", block["blocking"])
                result = health.assess(
                    EPIC_66, self.loaded,
                    github_graph.FixtureGraphSource(_set_state(self.capture, APPLY, "closed", reason)),
                )
                self.assertEqual([], [e.message for e in self.validator.iter_errors(result)])

    def test_unbound_required_item_is_incomplete(self) -> None:
        document = copy.deepcopy(self.document)
        for item in document["spec"]["workItems"]:
            if item["id"] == "governed-apply":
                item.pop("issue")
                item["title"] = "Governed apply"
                item["owner"] = "mctlhq/.github"
        block = self._compute(self.capture, document)
        self.assertEqual(("incomplete", "unbound"), (
            self._item(block, "governed-apply")["status"],
            self._item(block, "governed-apply")["reason"],
        ))

    def test_issue_observed_as_not_found_is_incomplete(self) -> None:
        block = self._compute(mutations.mark_missing(self.capture, APPLY))
        item = self._item(block, "governed-apply")
        self.assertEqual(("incomplete", "issue_not_found"), (item["status"], item["reason"]))

    # -- the invariant -------------------------------------------------------

    def test_unobserved_required_item_is_unknown_never_incomplete(self) -> None:
        snapshot = _set_state(self.capture, APPLY, "closed", "completed")
        key = ("mctlhq/.github", 68)
        block = self._compute(snapshot, unobserved=frozenset({key}))
        item = self._item(block, "governed-apply")
        self.assertEqual(("unknown", "unobserved"), (item["status"], item["reason"]))
        self.assertEqual("unknown", block["status"])
        self.assertEqual(["governed-apply"], block["blocking"])

    def test_state_that_was_not_captured_is_unknown(self) -> None:
        snapshot = copy.deepcopy(_set_state(self.capture, APPLY, "closed", "completed"))
        for observation in snapshot["issues"]:
            if observation["requested"] == mutations.ref(APPLY):
                observation.pop("state")
                observation.pop("stateReason", None)
        block = self._compute(snapshot)
        self.assertEqual(("unknown", "state_not_observed"), (
            self._item(block, "governed-apply")["status"],
            self._item(block, "governed-apply")["reason"],
        ))
        self.assertEqual("unknown", block["status"])

    def test_an_observed_incomplete_item_outranks_unknown(self) -> None:
        """Incomplete is an observed fact; it holds even when another item is unknown."""

        block = self._compute(self.capture, unobserved=frozenset({("mctlhq/.github", 83)}))
        self.assertEqual("unknown", self._item(block, "roadmap-health")["status"])
        self.assertEqual("incomplete", self._item(block, "governed-apply")["status"])
        self.assertEqual("incomplete", block["status"])

    def test_partial_observation_reports_completion_through_health(self) -> None:
        partial = copy.deepcopy(self.capture)
        partial["issues"] = [
            item for item in partial["issues"] if item["requested"] != mutations.ref(HEALTH)
        ]
        result = self._assess(partial)
        self.assertEqual("observation_failed", result["state"])
        self.assertEqual("unknown", self._item(result["completion"], "roadmap-health")["status"])
        self.assertEqual([], [e.message for e in self.validator.iter_errors(result)])

    # -- contract ------------------------------------------------------------

    def test_schema_rejects_completion_that_contradicts_its_blockers(self) -> None:
        result = self._assess(self.capture)
        forged = copy.deepcopy(result)
        forged["completion"]["status"] = "complete"
        self.assertNotEqual([], list(self.validator.iter_errors(forged)))

        forged = copy.deepcopy(result)
        forged["completion"]["blocking"] = []
        self.assertNotEqual([], list(self.validator.iter_errors(forged)))

    def test_consistency_errors_reject_blocking_that_contradicts_the_items(self) -> None:
        block = self._assess(self.capture)["completion"]
        self.assertEqual([], completion.consistency_errors(block))

        forged = {
            "optional item listed as blocking": lambda b: b["blocking"].append("temporal-health"),
            "complete item listed as blocking": lambda b: b["blocking"].append("reconciler"),
            "counts disagree with items": lambda b: b["required"].__setitem__("complete", 3),
            "status disagrees with items": lambda b: b.__setitem__("status", "unknown"),
        }
        for name, mutate in forged.items():
            with self.subTest(case=name):
                copy_block = copy.deepcopy(block)
                mutate(copy_block)
                self.assertNotEqual([], completion.consistency_errors(copy_block))
                result = self._assess(self.capture)
                result["completion"] = copy_block
                self.assertNotEqual([], health.semantic_errors(result))

    def test_observed_snapshot_without_completion_is_rejected(self) -> None:
        partial = copy.deepcopy(self.capture)
        partial["issues"] = [i for i in partial["issues"] if i["requested"] != mutations.ref(HEALTH)]
        result = self._assess(partial)
        self.assertEqual("observation_failed", result["state"])
        self.assertIn("source", result)
        forged = copy.deepcopy(result)
        forged.pop("completion")
        self.assertNotEqual([], list(self.validator.iter_errors(forged)))

    def test_bindings_colliding_on_one_issue_are_not_credited(self) -> None:
        """A transfer that folds two work items onto one closed issue must not complete both."""

        moved = mutations.redirect(self.capture, APPLY, RECONCILER)
        result = self._assess(moved)
        block = result["completion"]
        for item_id in ("reconciler", "governed-apply"):
            item = self._item(block, item_id)
            self.assertEqual(("unknown", "binding_ambiguous"), (item["status"], item["reason"]))
        self.assertNotEqual("complete", block["status"])
        self.assertIn("BindingAmbiguous", [d["code"] for d in result["diagnostics"]])
        self.assertEqual([], completion.consistency_errors(block))
        self.assertEqual([], [e.message for e in self.validator.iter_errors(result)])

    def test_closed_issue_under_two_parents_is_not_credited(self) -> None:
        """The reconciler calls a binding with two observed parents ambiguous; so must completion."""

        closed = _set_state(self.capture, APPLY, "closed", "completed")
        self.assertEqual("complete", self._item(self._compute(closed), "governed-apply")["status"])

        result = self._assess(mutations.add_second_parent(closed, APPLY, HEALTH))
        block = result["completion"]
        item = self._item(block, "governed-apply")
        self.assertEqual(("unknown", "binding_ambiguous"), (item["status"], item["reason"]))
        self.assertIn("governed-apply", block["blocking"])
        self.assertIn("BindingAmbiguous", [d["code"] for d in result["diagnostics"]])
        self.assertEqual([], completion.consistency_errors(block))
        self.assertEqual([], [e.message for e in self.validator.iter_errors(result)])

    def test_invalid_epic_carries_no_completion(self) -> None:
        result = health.invalid(EPIC_66, None, {EPIC_66: ("schema failure",)})
        self.assertNotIn("completion", result)
        forged = copy.deepcopy(result)
        forged["completion"] = self._assess(self.capture)["completion"]
        self.assertNotEqual([], list(self.validator.iter_errors(forged)))

    def test_output_is_deterministic(self) -> None:
        shuffled = copy.deepcopy(self.capture)
        shuffled["issues"] = list(reversed(shuffled["issues"]))
        self.assertEqual(
            json.dumps(self._compute(self.capture), sort_keys=True),
            json.dumps(self._compute(shuffled), sort_keys=True),
        )


if __name__ == "__main__":
    unittest.main()
