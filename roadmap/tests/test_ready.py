from __future__ import annotations

import copy
import hashlib
import inspect
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROADMAP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROADMAP / "scripts"))
sys.path.insert(0, str(ROADMAP / "tests"))

import github_graph  # noqa: E402
import health  # noqa: E402
import mutations  # noqa: E402
import ready  # noqa: E402
import reconcile  # noqa: E402

LIFECYCLE = ROADMAP / "epics" / "lifecycle-ownership.yaml"
HUMAN_INPUT = ROADMAP / "epics" / "human-input.yaml"
UNIFIED_IDENTITY = ROADMAP / "epics" / "unified-identity.yaml"
EPIC_66 = ROADMAP / "epics" / "roadmap-control-plane.yaml"

CONVERGED_HUMAN_INPUT = ROADMAP / "fixtures" / "human-input" / "converged-fixture.json"
CAPTURE_66 = ROADMAP / "fixtures" / "roadmap-control-plane" / "live-capture.json"


class ReadyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lifecycle = yaml.safe_load(LIFECYCLE.read_text(encoding="utf-8"))
        cls.human_input = yaml.safe_load(HUMAN_INPUT.read_text(encoding="utf-8"))
        cls.unified_identity = yaml.safe_load(UNIFIED_IDENTITY.read_text(encoding="utf-8"))
        cls.epic_66 = yaml.safe_load(EPIC_66.read_text(encoding="utf-8"))
        cls.converged_human_input = json.loads(CONVERGED_HUMAN_INPUT.read_text(encoding="utf-8"))
        cls.capture_66 = json.loads(CAPTURE_66.read_text(encoding="utf-8"))
        cls.validator = Draft202012Validator(
            json.loads((ROADMAP / "schemas" / "roadmap-ready-set.schema.json").read_text())
        )

    # -- helpers --------------------------------------------------------------

    def _loaded(self, path: Path, document: dict) -> reconcile.LoadedManifest:
        return reconcile.LoadedManifest(
            document=document, sha256=hashlib.sha256(path.read_bytes()).hexdigest()
        )

    def _assess(self, path: Path, document: dict, snapshot: dict):
        result, unobserved = ready.assess(
            path, self._loaded(path, document), github_graph.FixtureGraphSource(snapshot)
        )
        self.assertEqual([], [e.message for e in self.validator.iter_errors(result)])
        self.assertEqual([], ready.consistency_errors(result))
        return result, unobserved

    def _item(self, result: dict, item_id: str) -> dict:
        return next(item for item in result["items"] if item["id"] == item_id)

    # -- T1/T2/T3/T4: lifecycle-ownership dependency chain --------------------

    def test_linear_chain_only_the_root_item_starts_ready(self) -> None:
        base = mutations.synthetic_snapshot(self.lifecycle)
        result, _ = self._assess(LIFECYCLE, self.lifecycle, base)
        self.assertEqual(["ownership-contract"], result["ready"])
        self.assertEqual("blocked", self._item(result, "devloop-ownership")["state"])

        closed = mutations.set_state(base, "mctlhq/mctl-agents#350", "closed", "completed")
        result, _ = self._assess(LIFECYCLE, self.lifecycle, closed)
        self.assertEqual(["devloop-ownership"], result["ready"])
        self.assertEqual("blocked", self._item(result, "ownership-inspection")["state"])

    def test_fan_out_reports_both_successors_ready_together(self) -> None:
        base = mutations.synthetic_snapshot(self.lifecycle)
        closed = mutations.set_state(base, "mctlhq/mctl-agents#350", "closed", "completed")
        closed = mutations.set_state(closed, "mctlhq/mctl-agents#351", "closed", "completed")
        result, _ = self._assess(LIFECYCLE, self.lifecycle, closed)
        self.assertEqual(["executor-fencing", "ownership-inspection"], result["ready"])

    def test_fan_in_with_one_open_predecessor_is_blocked_on_exactly_it(self) -> None:
        graph = mutations.synthetic_snapshot(self.lifecycle)
        for issue in (
            "mctlhq/mctl-agents#350",
            "mctlhq/mctl-agents#351",
            "mctlhq/mctl-api#293",
            "mctlhq/mctl-agents#352",
        ):
            graph = mutations.set_state(graph, issue, "closed", "completed")
        result, _ = self._assess(LIFECYCLE, self.lifecycle, graph)
        item = self._item(result, "guarded-recovery")
        self.assertEqual("blocked", item["state"])
        self.assertEqual(
            [{"kind": "workItem", "id": "ownership-reconciler", "status": "incomplete", "reason": "open"}],
            item["blockers"],
        )

    def test_blocked_predecessor_outranks_unknown_predecessor(self) -> None:
        # `dependsOn` a `_BLOCKING` predecessor (open) and an `_INDETERMINATE`
        # one (unbound) at once: non-readiness is already proven, so the item
        # must be `blocked`, not `unknown`, even though an indeterminate
        # predecessor is also present. See ready.py:191-194.
        document = {
            "spec": {
                "workItems": [
                    {
                        "id": "blocked-pred",
                        "phase": "p",
                        "required": True,
                        "issue": {"repository": "mctlhq/x", "number": 1},
                    },
                    {
                        "id": "unbound-pred",
                        "phase": "p",
                        "required": True,
                    },
                    {
                        "id": "req",
                        "phase": "p",
                        "required": True,
                        "issue": {"repository": "mctlhq/x", "number": 2},
                        "dependsOn": ["blocked-pred", "unbound-pred"],
                    },
                ]
            }
        }

        def _observation(number: int) -> dict:
            key = {"repository": "mctlhq/x", "number": number}
            return {
                "requested": key,
                "resolved": key,
                "found": True,
                "state": "open",
                "parent": None,
                "subIssues": [],
                "blockedBy": [],
            }

        snapshot = {
            "apiVersion": "roadmap.mctl.ai/v1alpha1",
            "kind": "GitHubGraphSnapshot",
            "source": {"mode": "synthetic-fixture"},
            "issues": [_observation(1), _observation(2)],
        }
        graph = github_graph.observed_graph(snapshot)
        result = ready.compute(document, graph)
        self.assertEqual([], ready.consistency_errors(result))
        item = next(entry for entry in result["items"] if entry["id"] == "req")
        self.assertEqual("blocked", item["state"])
        self.assertEqual(
            [
                {"kind": "workItem", "id": "blocked-pred", "status": "incomplete", "reason": "open"},
                {"kind": "workItem", "id": "unbound-pred", "status": "incomplete", "reason": "unbound"},
            ],
            item["blockers"],
        )

    def test_guarded_recovery_ready_once_every_predecessor_is_delivered(self) -> None:
        graph = mutations.synthetic_snapshot(self.lifecycle)
        for issue in ("mctlhq/mctl-api#293", "mctlhq/mctl-agents#352", "mctlhq/mctl-agents#353"):
            graph = mutations.set_state(graph, issue, "closed", "completed")
        result, _ = self._assess(LIFECYCLE, self.lifecycle, graph)
        item = self._item(result, "guarded-recovery")
        self.assertEqual("ready", item["state"])
        self.assertEqual([], item["blockers"])
        self.assertIn("guarded-recovery", result["ready"])

    def test_open_and_retired_reasons_partition_the_blocking_set(self) -> None:
        # ready.py reads a blocking reason two ways: as a predecessor's, where
        # every member of BLOCKING_REASONS blocks, and as the item's own, where
        # only `open` leaves it executable. A new blocking reason has to be
        # placed on one side deliberately -- this is what makes forgetting loud
        # instead of defaulting it into "retired".
        self.assertEqual(ready.BLOCKING_REASONS, ready.RETIRED_REASONS | {ready.OPEN})
        self.assertEqual(frozenset(), ready.RETIRED_REASONS & {ready.OPEN})

    def test_own_issue_closed_not_planned_is_blocked_never_ready(self) -> None:
        # Every predecessor of guarded-recovery is delivered, so the ONLY thing
        # between it and `ready` is its own issue -- which GitHub has closed as
        # not_planned. `_classify` calls that reason `_BLOCKING`, exactly like
        # `open`, so before this was fixed the item fell through to the
        # predecessor checks and came out `ready`: a wave launcher would have
        # been handed an issue GitHub had already closed. `consistency_errors`
        # did not catch it either -- it only asserted the own reason was one of
        # BLOCKING_REASONS, and `closed_not_planned` is.
        graph = mutations.synthetic_snapshot(self.lifecycle)
        for issue in ("mctlhq/mctl-api#293", "mctlhq/mctl-agents#352", "mctlhq/mctl-agents#353"):
            graph = mutations.set_state(graph, issue, "closed", "completed")

        for reason in ("not_planned", "duplicate"):
            with self.subTest(reason=reason):
                retired = mutations.set_state(graph, "mctlhq/mctl-api#294", "closed", reason)
                # _assess also runs the schema and consistency_errors.
                result, _ = self._assess(LIFECYCLE, self.lifecycle, retired)
                item = self._item(result, "guarded-recovery")
                self.assertEqual("blocked", item["state"])
                self.assertNotIn("guarded-recovery", result["ready"])
                # No predecessor is to blame, so the item names itself -- and a
                # `blocked` item with no blockers fails both the schema and
                # consistency_errors, so it cannot simply be left empty.
                self.assertEqual(
                    [
                        {
                            "kind": "workItem",
                            "id": "guarded-recovery",
                            "status": "incomplete",
                            "reason": "closed_%s" % reason,
                        }
                    ],
                    item["blockers"],
                )

    def test_retired_predecessor_still_blocks_its_dependent(self) -> None:
        # The other half of the asymmetry: on a PREDECESSOR the same reason
        # keeps meaning "evidence of undelivered work", so the dependent stays
        # `blocked` on it rather than going `unknown`. Tightening the item's own
        # axis must not have moved this one.
        graph = mutations.synthetic_snapshot(self.lifecycle)
        graph = mutations.set_state(graph, "mctlhq/mctl-agents#350", "closed", "not_planned")
        result, _ = self._assess(LIFECYCLE, self.lifecycle, graph)
        item = self._item(result, "devloop-ownership")
        self.assertEqual("blocked", item["state"])
        self.assertEqual(
            [
                {
                    "kind": "workItem",
                    "id": "ownership-contract",
                    "status": "incomplete",
                    "reason": "closed_not_planned",
                }
            ],
            item["blockers"],
        )

    def test_consistency_errors_reject_a_ready_item_whose_own_issue_is_not_open(self) -> None:
        # The self-check has to reject the pre-fix document on its own, so a
        # consumer that did not compute the ready set cannot be handed one.
        graph = mutations.synthetic_snapshot(self.lifecycle)
        for issue in ("mctlhq/mctl-api#293", "mctlhq/mctl-agents#352", "mctlhq/mctl-agents#353"):
            graph = mutations.set_state(graph, issue, "closed", "completed")
        result, _ = self._assess(LIFECYCLE, self.lifecycle, graph)
        self.assertEqual("ready", self._item(result, "guarded-recovery")["state"])

        forged = copy.deepcopy(result)
        item = next(e for e in forged["items"] if e["id"] == "guarded-recovery")
        item["completion"]["reason"] = "closed_not_planned"
        self.assertTrue(
            any("state ready has own reason" in error
                for error in ready.consistency_errors(forged)),
            ready.consistency_errors(forged),
        )

        # A self-blocker is legitimate only for a retirement; anything else is a
        # cycle validate.py should already have rejected.
        forged = copy.deepcopy(result)
        item = next(e for e in forged["items"] if e["id"] == "devloop-ownership")
        item["blockers"] = [
            {
                "kind": "workItem",
                "id": "devloop-ownership",
                "status": "incomplete",
                "reason": "open",
            }
        ]
        self.assertTrue(
            any("names itself as a blocker" in error
                for error in ready.consistency_errors(forged)),
            ready.consistency_errors(forged),
        )

    # -- T5/T6/T7: required vs optional -----------------------------------

    def test_human_input_fresh_graph_ready_is_exactly_the_core(self) -> None:
        result, unobserved = self._assess(HUMAN_INPUT, self.human_input, self.converged_human_input)
        self.assertEqual(frozenset(), unobserved)
        required_ready = sorted(
            item["id"] for item in result["items"] if item["required"] and item["state"] == "ready"
        )
        self.assertEqual(["human-input-core"], required_ready)
        self.assertEqual(["human-input-core"], result["ready"])

    def test_optional_item_becomes_ready_and_is_listed(self) -> None:
        snapshot = mutations.set_state(
            self.converged_human_input, "mctlhq/mctl-api#261", "closed", "completed"
        )
        # portal-card also waits on the delegated surface identity it answers with.
        snapshot = mutations.set_state(snapshot, "mctlhq/mctl-api#350", "closed", "completed")
        result, _ = self._assess(HUMAN_INPUT, self.human_input, snapshot)
        item = self._item(result, "portal-card")
        self.assertFalse(item["required"])
        self.assertEqual("ready", item["state"])
        self.assertIn("portal-card", result["ready"])

    def test_optional_item_is_a_real_blocker_of_its_dependent(self) -> None:
        document = {
            "spec": {
                "workItems": [
                    {
                        "id": "opt",
                        "phase": "p",
                        "required": False,
                        "issue": {"repository": "mctlhq/x", "number": 1},
                    },
                    {
                        "id": "req",
                        "phase": "p",
                        "required": True,
                        "issue": {"repository": "mctlhq/x", "number": 2},
                        "dependsOn": ["opt"],
                    },
                ]
            }
        }

        def _observation(number: int) -> dict:
            key = {"repository": "mctlhq/x", "number": number}
            return {
                "requested": key,
                "resolved": key,
                "found": True,
                "state": "open",
                "parent": None,
                "subIssues": [],
                "blockedBy": [],
            }

        snapshot = {
            "apiVersion": "roadmap.mctl.ai/v1alpha1",
            "kind": "GitHubGraphSnapshot",
            "source": {"mode": "synthetic-fixture"},
            "issues": [_observation(1), _observation(2)],
        }
        graph = github_graph.observed_graph(snapshot)
        result = ready.compute(document, graph)
        self.assertEqual([], ready.consistency_errors(result))
        item = next(entry for entry in result["items"] if entry["id"] == "req")
        self.assertEqual("blocked", item["state"])
        self.assertEqual(
            [{"kind": "workItem", "id": "opt", "status": "incomplete", "reason": "open"}],
            item["blockers"],
        )

    # -- T8: unbound is never ready -------------------------------------

    def test_unbound_items_are_unknown_never_ready(self) -> None:
        result, _ = self._assess(HUMAN_INPUT, self.human_input, self.converged_human_input)
        item = self._item(result, "devloop-e2e")
        self.assertEqual("unknown", item["state"])
        self.assertEqual("unbound", item["completion"]["reason"])
        self.assertNotIn("devloop-e2e", result["ready"])

        snapshot = mutations.synthetic_snapshot(self.unified_identity)
        result, _ = self._assess(UNIFIED_IDENTITY, self.unified_identity, snapshot)
        item = self._item(result, "principal-model")
        self.assertEqual("unknown", item["state"])
        self.assertEqual("unbound", item["completion"]["reason"])
        self.assertEqual([], result["ready"])

    # -- T9: unobserved predecessor ---------------------------------------

    def test_unobserved_predecessor_is_unknown_never_ready(self) -> None:
        base = mutations.synthetic_snapshot(self.lifecycle)
        partial = copy.deepcopy(base)
        target = mutations.ref("mctlhq/mctl-agents#350")
        partial["issues"] = [item for item in partial["issues"] if item["requested"] != target]

        result, unobserved = self._assess(LIFECYCLE, self.lifecycle, partial)
        self.assertIn(("mctlhq/mctl-agents", 350), unobserved)

        item = self._item(result, "ownership-contract")
        self.assertEqual("unknown", item["state"])

        dependent = self._item(result, "devloop-ownership")
        self.assertEqual("unknown", dependent["state"])
        self.assertEqual(
            [{"kind": "workItem", "id": "ownership-contract", "status": "unknown", "reason": "unobserved"}],
            dependent["blockers"],
        )
        self.assertNotIn("ownership-contract", result["ready"])
        self.assertNotIn("devloop-ownership", result["ready"])

    # -- T10: binding faults ------------------------------------------------

    def test_not_found_predecessor_yields_unknown_dependent(self) -> None:
        base = mutations.synthetic_snapshot(self.lifecycle)
        missing = mutations.mark_missing(base, "mctlhq/mctl-agents#350")
        result, _ = self._assess(LIFECYCLE, self.lifecycle, missing)
        dependent = self._item(result, "devloop-ownership")
        self.assertEqual("unknown", dependent["state"])
        self.assertEqual(
            [{"kind": "workItem", "id": "ownership-contract", "status": "incomplete", "reason": "issue_not_found"}],
            dependent["blockers"],
        )

    def test_redirected_predecessor_still_satisfies_the_edge(self) -> None:
        base = mutations.synthetic_snapshot(self.lifecycle)
        closed = mutations.set_state(base, "mctlhq/mctl-agents#350", "closed", "completed")
        redirected = mutations.redirect(closed, "mctlhq/mctl-agents#350", "mctlhq/mctl-agents#9999")
        result, _ = self._assess(LIFECYCLE, self.lifecycle, redirected)
        self.assertEqual("complete", self._item(result, "ownership-contract")["state"])
        self.assertEqual("ready", self._item(result, "devloop-ownership")["state"])

    def test_ambiguous_binding_yields_unknown_dependent(self) -> None:
        base = mutations.synthetic_snapshot(self.lifecycle)
        ambiguous = mutations.add_second_parent(
            base, "mctlhq/mctl-agents#350", "mctlhq/mctl-agents#351"
        )
        result, _ = self._assess(LIFECYCLE, self.lifecycle, ambiguous)
        item = self._item(result, "ownership-contract")
        self.assertEqual(
            ("unknown", "binding_ambiguous"), (item["completion"]["status"], item["completion"]["reason"])
        )
        self.assertEqual("unknown", self._item(result, "devloop-ownership")["state"])

    # -- T11: external dependency, three ways --------------------------------

    def test_external_dependency_three_ways(self) -> None:
        closed_locals = mutations.set_state(
            self.converged_human_input, "mctlhq/mctl-agents#333", "closed", "completed"
        )
        closed_locals = mutations.set_state(closed_locals, "mctlhq/mctl-api#261", "closed", "completed")
        # The adapter's second external edge (delegated surface identity) is
        # satisfied throughout, so the three cases below isolate #443.
        closed_locals = mutations.set_state(closed_locals, "mctlhq/mctl-api#350", "closed", "completed")

        closed_external = mutations.set_state(
            closed_locals, "mctlhq/mctl-telegram#443", "closed", "completed"
        )
        result, _ = self._assess(HUMAN_INPUT, self.human_input, closed_external)
        item = self._item(result, "telegram-adapter")
        self.assertEqual("ready", item["state"])
        self.assertEqual([], item["blockers"])

        result, _ = self._assess(HUMAN_INPUT, self.human_input, closed_locals)
        item = self._item(result, "telegram-adapter")
        self.assertEqual("blocked", item["state"])
        self.assertEqual(
            [
                {
                    "kind": "external",
                    "issue": {"repository": "mctlhq/mctl-telegram", "number": 443},
                    "status": "incomplete",
                    "reason": "open",
                }
            ],
            item["blockers"],
        )

        without_external = copy.deepcopy(closed_locals)
        target = mutations.ref("mctlhq/mctl-telegram#443")
        without_external["issues"] = [
            item for item in without_external["issues"] if item["requested"] != target
        ]
        result, unobserved = self._assess(HUMAN_INPUT, self.human_input, without_external)
        item = self._item(result, "telegram-adapter")
        self.assertEqual("unknown", item["state"])
        self.assertIn(("mctlhq/mctl-telegram", 443), unobserved)

    # -- T12: byte identity ---------------------------------------------------

    def test_output_is_deterministic_under_shuffled_input_order(self) -> None:
        graph = github_graph.observed_graph(self.converged_human_input)
        first = json.dumps(ready.compute(self.human_input, graph), sort_keys=True)
        second = json.dumps(ready.compute(self.human_input, graph), sort_keys=True)
        self.assertEqual(first, second)

        shuffled_document = copy.deepcopy(self.human_input)
        shuffled_document["spec"]["workItems"] = list(
            reversed(shuffled_document["spec"]["workItems"])
        )
        shuffled_snapshot = copy.deepcopy(self.converged_human_input)
        shuffled_snapshot["issues"] = list(reversed(shuffled_snapshot["issues"]))
        shuffled_graph = github_graph.observed_graph(shuffled_snapshot)

        third = json.dumps(ready.compute(shuffled_document, shuffled_graph), sort_keys=True)
        self.assertEqual(first, third)

    # -- T13: runtime independence -------------------------------------------

    def test_compute_takes_no_runtime_argument_and_ignores_updated_at(self) -> None:
        signature = inspect.signature(ready.compute)
        self.assertEqual(["document", "observed", "unobserved"], list(signature.parameters))

        source = Path(ready.__file__).read_text(encoding="utf-8").lower()
        for forbidden in ("temporal", "devloop", "import argo"):
            self.assertNotIn(forbidden, source)

        graph = github_graph.observed_graph(self.converged_human_input)
        baseline = json.dumps(ready.compute(self.human_input, graph), sort_keys=True)

        touched = copy.deepcopy(self.converged_human_input)
        for observation in touched["issues"]:
            observation["updatedAt"] = "2020-01-01T00:00:00Z"
        touched_graph = github_graph.observed_graph(touched)
        mutated = json.dumps(ready.compute(self.human_input, touched_graph), sort_keys=True)
        self.assertEqual(baseline, mutated)

    # -- T14: no regression on the sibling axes ------------------------------

    def test_ready_completion_matches_health_completion_for_epic_66(self) -> None:
        loaded = self._loaded(EPIC_66, self.epic_66)
        health_result = health.assess(EPIC_66, loaded, github_graph.FixtureGraphSource(self.capture_66))
        self.assertEqual("healthy", health_result["state"])

        ready_result, unobserved = self._assess(EPIC_66, self.epic_66, self.capture_66)
        self.assertEqual(frozenset(), unobserved)

        for item in ready_result["items"]:
            completion_item = next(
                entry for entry in health_result["completion"]["items"] if entry["id"] == item["id"]
            )
            self.assertEqual(
                (completion_item["status"], completion_item["reason"]),
                (item["completion"]["status"], item["completion"]["reason"]),
            )

    # -- contract: schema -----------------------------------------------------

    def test_schema_rejects_unknown_field_state_and_undiscriminated_blocker(self) -> None:
        base, _ = self._assess(LIFECYCLE, self.lifecycle, mutations.synthetic_snapshot(self.lifecycle))
        self.assertEqual([], list(self.validator.iter_errors(base)))

        forged = copy.deepcopy(base)
        forged["unexpectedField"] = True
        self.assertNotEqual([], list(self.validator.iter_errors(forged)))

        forged = copy.deepcopy(base)
        forged["items"][0]["state"] = "not-a-state"
        self.assertNotEqual([], list(self.validator.iter_errors(forged)))

        forged = copy.deepcopy(base)
        forged["items"][0]["blockers"] = [{"status": "incomplete", "reason": "open"}]
        self.assertNotEqual([], list(self.validator.iter_errors(forged)))

    def test_list_envelope_for_more_than_one_manifest(self) -> None:
        lifecycle_result, _ = self._assess(
            LIFECYCLE, self.lifecycle, mutations.synthetic_snapshot(self.lifecycle)
        )
        human_input_result, _ = self._assess(
            HUMAN_INPUT, self.human_input, self.converged_human_input
        )
        rendered = ready.render([lifecycle_result, human_input_result])
        self.assertEqual("RoadmapReadySetList", rendered["kind"])
        self.assertEqual([], list(self.validator.iter_errors(rendered)))
        self.assertEqual(
            [item["epic"]["manifest"]["path"] for item in rendered["items"]],
            sorted(item["epic"]["manifest"]["path"] for item in rendered["items"]),
        )

        single = ready.render([lifecycle_result])
        self.assertEqual("RoadmapReadySet", single["kind"])

    # -- contract: consistency_errors ----------------------------------------

    def test_consistency_errors_reject_each_rule_in_isolation(self) -> None:
        snapshot = mutations.set_state(
            self.converged_human_input, "mctlhq/mctl-agents#333", "closed", "completed"
        )
        result, _ = self._assess(HUMAN_INPUT, self.human_input, snapshot)
        self.assertEqual([], ready.consistency_errors(result))

        # sanity on the fixture: one item in each of the four states.
        states = {item["id"]: item["state"] for item in result["items"]}
        self.assertEqual("complete", states["human-input-core"])
        self.assertEqual("ready", states["catalog-profile-rollout"])
        self.assertEqual("blocked", states["telegram-adapter"])
        self.assertEqual("unknown", states["devloop-e2e"])

        def _item(document: dict, item_id: str) -> dict:
            return next(entry for entry in document["items"] if entry["id"] == item_id)

        def ready_carries_blockers(document: dict) -> None:
            _item(document, "catalog-profile-rollout")["blockers"] = [
                {"kind": "workItem", "id": "human-input-core", "status": "incomplete", "reason": "open"}
            ]

        def ready_has_wrong_own_reason(document: dict) -> None:
            _item(document, "catalog-profile-rollout")["completion"]["reason"] = "unbound"

        def blocked_has_no_blocking_reason(document: dict) -> None:
            # "docs" depends on human-input-core (now complete) and
            # human-input-api (still open), and has no externalDependsOn, so
            # it carries exactly one blocker -- unlike telegram-adapter, whose
            # second, external, blocker would otherwise still satisfy the rule.
            _item(document, "docs")["blockers"][0]["reason"] = "unobserved"

        def complete_carries_a_blocker(document: dict) -> None:
            _item(document, "human-input-core")["blockers"] = [
                {"kind": "workItem", "id": "catalog-profile-rollout", "status": "incomplete", "reason": "open"}
            ]

        def unknown_has_no_indeterminate_evidence(document: dict) -> None:
            _item(document, "devloop-e2e")["completion"] = {"status": "incomplete", "reason": "open"}

        def ready_list_disagrees_with_items(document: dict) -> None:
            document["ready"] = document["ready"] + ["bogus-item"]

        def summary_disagrees_with_items(document: dict) -> None:
            document["summary"]["items"]["ready"] += 1

        def blocker_names_a_foreign_id(document: dict) -> None:
            _item(document, "docs")["blockers"][0]["id"] = "not-a-work-item"

        cases = {
            "ready item carrying blockers": ready_carries_blockers,
            "ready item with wrong own reason": ready_has_wrong_own_reason,
            "blocked item with no blocking-reason blocker": blocked_has_no_blocking_reason,
            "complete item carrying a blocker": complete_carries_a_blocker,
            "unknown item with no indeterminate evidence": unknown_has_no_indeterminate_evidence,
            "ready list disagrees with items": ready_list_disagrees_with_items,
            "summary disagrees with items": summary_disagrees_with_items,
            "blocker names a foreign id": blocker_names_a_foreign_id,
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                forged = copy.deepcopy(result)
                mutate(forged)
                errors = ready.consistency_errors(forged)
                self.assertEqual(1, len(errors), errors)

    # -- CLI -------------------------------------------------------------

    def test_cli_requires_exactly_one_source(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = ready.main(
                [str(LIFECYCLE), "--corpus", str(ROADMAP / "epics"), "--snapshot", "x", "--live"]
            )
        self.assertEqual(ready.EXIT_USAGE, code)

    def test_cli_prints_a_schema_valid_ready_set_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            snapshot_path = directory / "snapshot.json"
            snapshot_path.write_text(
                json.dumps(mutations.synthetic_snapshot(self.lifecycle)), encoding="utf-8"
            )
            output_path = directory / "ready.json"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = ready.main(
                    [
                        str(LIFECYCLE),
                        "--corpus",
                        str(ROADMAP / "epics"),
                        "--snapshot",
                        str(snapshot_path),
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(ready.EXIT_OK, code)
            payload = json.loads(output_path.read_text())
            self.assertEqual("RoadmapReadySet", payload["kind"])
            self.assertEqual([], list(self.validator.iter_errors(payload)))

    def test_cli_unreadable_snapshot_exits_observation_failed_and_prints_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            output_path = directory / "ready.json"
            with redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()):
                code = ready.main(
                    [
                        str(LIFECYCLE),
                        "--corpus",
                        str(ROADMAP / "epics"),
                        "--snapshot",
                        "/nonexistent/snapshot.json",
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(ready.EXIT_OBSERVATION_FAILED, code)
            self.assertFalse(output_path.exists())
            self.assertEqual("", out.getvalue())

    def test_cli_partial_observation_still_emits_a_document_and_exits_observation_failed(
        self,
    ) -> None:
        base = mutations.synthetic_snapshot(self.lifecycle)
        target = mutations.ref("mctlhq/mctl-agents#350")
        partial = copy.deepcopy(base)
        partial["issues"] = [item for item in partial["issues"] if item["requested"] != target]

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            snapshot_path = directory / "snapshot.json"
            snapshot_path.write_text(json.dumps(partial), encoding="utf-8")
            output_path = directory / "ready.json"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = ready.main(
                    [
                        str(LIFECYCLE),
                        "--corpus",
                        str(ROADMAP / "epics"),
                        "--snapshot",
                        str(snapshot_path),
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(ready.EXIT_OBSERVATION_FAILED, code)
            payload = json.loads(output_path.read_text())
            self.assertEqual("RoadmapReadySet", payload["kind"])
            self.assertEqual([], list(self.validator.iter_errors(payload)))


if __name__ == "__main__":
    unittest.main()
