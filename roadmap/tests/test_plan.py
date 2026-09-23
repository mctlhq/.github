from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import yaml

ROADMAP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROADMAP / "scripts"))
sys.path.insert(0, str(ROADMAP / "tests"))

import github_graph  # noqa: E402
import mutations  # noqa: E402
import plan as plan_module  # noqa: E402
import reconcile  # noqa: E402
import validate  # noqa: E402

PILOT = ROADMAP / "epics" / "human-input.yaml"
CONVERGED = ROADMAP / "fixtures" / "human-input" / "converged-fixture.json"

ROOT_ISSUE = "mctlhq/.github#42"
CORE = "mctlhq/mctl-agents#333"
API = "mctlhq/mctl-api#261"
TELEGRAM = "mctlhq/mctl-telegram#571"
PORTAL = "mctlhq/mctl-portal#124"
DOCS = "mctlhq/mctl-docs#106"
FOREIGN = "mctlhq/mctl-web#5"


def _types(entries: list[dict]) -> list[str]:
    return sorted(entry["type"] for entry in entries)


class PlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.safe_load(PILOT.read_text(encoding="utf-8"))
        cls.converged = json.loads(CONVERGED.read_text(encoding="utf-8"))
        cls.plan_schema = plan_module.load_schema()

    # -- helpers ---------------------------------------------------------

    def _diff(self, snapshot: dict, document: dict | None = None) -> dict:
        source = github_graph.FixtureGraphSource(snapshot)
        return reconcile.reconcile(PILOT, document or self.document, source)

    def _plan(self, snapshot: dict, document: dict | None = None) -> dict:
        built = plan_module.plan(
            self._diff(snapshot, document), document=document or self.document
        )
        # Every plan this suite produces is held to the published contract; a
        # test that asserted on a document the schema would reject proves
        # nothing about what a consumer receives.
        self.assertEqual([], plan_module.schema_errors(built, self.plan_schema))
        return built

    # -- T1 --------------------------------------------------------------

    def test_converged_fixture_plans_no_operations(self) -> None:
        built = self._plan(self.converged)
        self.assertEqual([], built["operations"])
        self.assertEqual([], built["refusals"])
        self.assertEqual(
            {"operations": 0, "notes": 0, "refusals": 0}, built["summary"]
        )
        self.assertFalse(plan_module.has_operations(built))
        self.assertFalse(plan_module.is_refused(built))

    def test_plan_carries_the_exact_manifest_bytes_it_was_authorized_by(self) -> None:
        built = self._plan(self.converged)
        self.assertEqual(
            reconcile._sha256(PILOT), built["epic"]["manifest"]["sha256"]
        )
        self.assertEqual("roadmap/epics/human-input.yaml", built["epic"]["manifest"]["path"])
        self.assertEqual({"mode": "synthetic-fixture"}, built["source"])

    def test_a_diff_from_other_manifest_bytes_is_refused(self) -> None:
        diff = self._diff(self.converged)
        with self.assertRaises(plan_module.PlanRefused):
            plan_module.plan(
                diff, document=self.document, manifest_sha256="0" * 64
            )

    # -- T2 --------------------------------------------------------------

    def test_each_mutation_plans_exactly_one_matching_operation(self) -> None:
        cases = {
            "AddSubIssue": (
                lambda s: mutations.drop_parent_edge(s, API),
                {"child": API, "parent": ROOT_ISSUE},
                {"type": "ParentAbsent", "child": API},
            ),
            "MoveSubIssue": (
                lambda s: mutations.repoint_parent(s, API, CORE),
                {"child": API, "parent": ROOT_ISSUE, "observedParent": CORE},
                {"type": "ParentIs", "child": API, "parent": CORE},
            ),
            "AddDependency": (
                lambda s: mutations.drop_dependency(s, API, CORE),
                {"blocked": API, "blocker": CORE},
                {"type": "DependencyAbsent", "blocked": API, "blocker": CORE},
            ),
            "RemoveDependency": (
                lambda s: mutations.add_dependency(s, DOCS, PORTAL),
                {"blocked": DOCS, "blocker": PORTAL},
                {"type": "DependencyPresent", "blocked": DOCS, "blocker": PORTAL},
            ),
        }
        for kind, (mutate, endpoints, precondition) in cases.items():
            with self.subTest(operation=kind):
                built = self._plan(mutate(self.converged))
                self.assertEqual([kind], [op["type"] for op in built["operations"]])
                self.assertEqual([], built["refusals"])
                operation = built["operations"][0]
                for field, label in endpoints.items():
                    self.assertEqual(mutations.ref(label), operation[field])
                expected = {
                    key: value if key == "type" else mutations.ref(value)
                    for key, value in precondition.items()
                }
                self.assertEqual(expected, operation["precondition"])

    def test_restoring_the_fixture_returns_to_an_empty_plan(self) -> None:
        original = copy.deepcopy(self.converged)
        for mutate in (
            lambda s: mutations.drop_parent_edge(s, API),
            lambda s: mutations.repoint_parent(s, API, CORE),
            lambda s: mutations.drop_dependency(s, API, CORE),
            lambda s: mutations.add_dependency(s, DOCS, PORTAL),
        ):
            mutated = mutate(self.converged)
            self.assertEqual(original, self.converged, "mutator changed its input")
            self.assertTrue(plan_module.has_operations(self._plan(mutated)))
            self.assertFalse(plan_module.has_operations(self._plan(original)))

    def test_one_mutation_never_plans_an_operation_in_a_neighbouring_family(self) -> None:
        hierarchy = self._plan(mutations.drop_parent_edge(self.converged, API))
        self.assertEqual(
            ["AddSubIssue"], [op["type"] for op in hierarchy["operations"]]
        )
        dependency = self._plan(mutations.drop_dependency(self.converged, API, CORE))
        self.assertEqual(
            ["AddDependency"], [op["type"] for op in dependency["operations"]]
        )

    # -- T3 --------------------------------------------------------------

    def _drifted(self) -> dict:
        drifted = mutations.drop_parent_edge(self.converged, API)
        return mutations.drop_dependency(drifted, DOCS, CORE)

    def test_planning_twice_is_byte_identical(self) -> None:
        snapshot = self._drifted()
        first = json.dumps(self._plan(snapshot), indent=2, sort_keys=True)
        second = json.dumps(self._plan(snapshot), indent=2, sort_keys=True)
        self.assertEqual(first, second)

    def test_reordering_work_items_does_not_change_the_plan(self) -> None:
        """Input order is not evidence. Two orderings are the same desired state."""

        snapshot = self._drifted()
        reordered = copy.deepcopy(self.document)
        reordered["spec"]["workItems"] = list(
            reversed(reordered["spec"]["workItems"])
        )
        self.assertEqual(
            json.dumps(self._plan(snapshot), indent=2, sort_keys=True),
            json.dumps(self._plan(snapshot, reordered), indent=2, sort_keys=True),
        )

    def test_a_plan_carries_no_clock_and_no_absolute_path(self) -> None:
        rendered = json.dumps(self._plan(self._drifted()), sort_keys=True)
        self.assertNotIn(str(ROADMAP), rendered)
        self.assertNotIn(str(ROADMAP.parent), rendered)
        for forbidden in ("capturedAt", "timestamp", "generatedAt", "createdAt"):
            self.assertNotIn(forbidden, rendered)

    def test_op_ids_are_stable_and_derived_from_the_manifest_digest(self) -> None:
        built = self._plan(self._drifted())
        ids = [operation["opId"] for operation in built["operations"]]
        self.assertEqual(2, len(ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(64, len(built["planId"]))

        # A different manifest digest is a different authorization, so the same
        # graph change must not reuse the same idempotency key.
        relabelled = self._diff(self._drifted())
        relabelled["epic"]["manifest"]["sha256"] = "a" * 64
        other = plan_module.plan(
            relabelled, document=self.document, manifest_sha256="a" * 64
        )
        self.assertEqual(
            set(), set(ids) & {op["opId"] for op in other["operations"]}
        )
        self.assertNotEqual(built["planId"], other["planId"])

    # -- T4 --------------------------------------------------------------

    def test_binding_drift_refuses_the_whole_plan(self) -> None:
        cases = {
            "BindingIssueNotFound": lambda s: mutations.mark_missing(s, API),
            "BindingRedirected": lambda s: mutations.redirect(
                s, PORTAL, "mctlhq/mctl-web#999"
            ),
            "BindingAmbiguous": lambda s: mutations.add_second_parent(s, API, CORE),
        }
        for reason, mutate in cases.items():
            with self.subTest(reason=reason):
                built = self._plan(mutate(self.converged))
                self.assertEqual([], built["operations"])
                self.assertIn(
                    reason, [refusal["reason"] for refusal in built["refusals"]]
                )
                self.assertTrue(plan_module.is_refused(built))

    def test_a_refusal_survives_alongside_actionable_drift(self) -> None:
        """One unsettled identity empties the plan, it does not merely filter it."""

        snapshot = mutations.drop_parent_edge(self.converged, DOCS)
        snapshot = mutations.mark_missing(snapshot, PORTAL)
        built = self._plan(snapshot)
        self.assertEqual([], built["operations"])
        self.assertEqual(
            ["BindingIssueNotFound"], [item["reason"] for item in built["refusals"]]
        )

    def test_informational_entries_become_notes_and_never_operations(self) -> None:
        document, snapshot = mutations.unbind_observed(
            self.document, self.converged, "devloop-e2e"
        )
        built = self._plan(
            mutations.add_unexpected_child(snapshot, ROOT_ISSUE, FOREIGN), document
        )
        self.assertEqual([], built["operations"])
        self.assertEqual([], built["refusals"])
        self.assertEqual(
            ["BindingUnbound", "HierarchyUnexpectedChild"], _types(built["notes"])
        )
        note = next(
            item for item in built["notes"] if item["type"] == "HierarchyUnexpectedChild"
        )
        self.assertEqual(mutations.ref(FOREIGN), note["child"])
        self.assertEqual(mutations.ref(ROOT_ISSUE), note["observedParent"])

    # -- T5 --------------------------------------------------------------

    def test_an_operation_naming_a_foreign_issue_is_refused(self) -> None:
        for entry in (
            {
                "type": "HierarchyMissingParent",
                "severity": "drift",
                "owner": "human-input-core",
                "child": mutations.ref(FOREIGN),
                "expectedParent": mutations.ref(ROOT_ISSUE),
            },
            {
                "type": "HierarchyWrongParent",
                "severity": "drift",
                "owner": "human-input-api",
                "child": mutations.ref(API),
                "expectedParent": mutations.ref(ROOT_ISSUE),
                "observedParent": mutations.ref(FOREIGN),
            },
        ):
            with self.subTest(entry=entry["type"]):
                diff = self._diff(self.converged)
                diff["hierarchy"].append(entry)
                with self.assertRaises(plan_module.PlanRefused):
                    plan_module.plan(diff, document=self.document)

    def test_a_dependency_on_an_unauthored_issue_is_refused(self) -> None:
        diff = self._diff(self.converged)
        diff["dependency"].append(
            {
                "type": "DependencyUnexpected",
                "severity": "drift",
                "owner": "human-input-api",
                "blocked": mutations.ref(API),
                "blocker": mutations.ref(FOREIGN),
            }
        )
        with self.assertRaises(plan_module.PlanRefused):
            plan_module.plan(diff, document=self.document)

    def test_an_external_dependency_endpoint_is_authored_and_may_be_planned(self) -> None:
        """externalDependsOn names somebody else's issue, but the manifest names it."""

        built = self._plan(
            mutations.drop_dependency(self.converged, TELEGRAM, "mctlhq/mctl-telegram#443")
        )
        self.assertEqual(["AddDependency"], [op["type"] for op in built["operations"]])
        self.assertEqual(
            mutations.ref("mctlhq/mctl-telegram#443"), built["operations"][0]["blocker"]
        )

    def test_an_unknown_diff_entry_type_is_refused_rather_than_ignored(self) -> None:
        diff = self._diff(self.converged)
        diff["hierarchy"].append(
            {"type": "HierarchySomethingNewer", "severity": "drift", "owner": "epic"}
        )
        with self.assertRaises(plan_module.PlanRefused):
            plan_module.plan(diff, document=self.document)

    # -- CLI -------------------------------------------------------------

    def _run(self, snapshot: dict, extra: list[str] | None = None) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = plan_module.main(
                    [
                        str(PILOT),
                        "--corpus",
                        str(ROADMAP / "epics"),
                        "--snapshot",
                        str(path),
                    ]
                    + (extra or [])
                )
        return code, out.getvalue()

    def test_cli_exit_codes(self) -> None:
        code, rendered = self._run(self.converged)
        self.assertEqual(plan_module.EXIT_EMPTY, code)
        self.assertEqual([], json.loads(rendered)["operations"])

        code, rendered = self._run(mutations.drop_parent_edge(self.converged, API))
        self.assertEqual(plan_module.EXIT_OPERATIONS, code)
        self.assertEqual(1, len(json.loads(rendered)["operations"]))

        code, rendered = self._run(mutations.mark_missing(self.converged, API))
        self.assertEqual(plan_module.EXIT_REFUSED, code)

    def test_cli_requires_exactly_one_source(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(
                plan_module.EXIT_ERROR, plan_module.main([str(PILOT), "--corpus", str(ROADMAP / "epics")])
            )
            self.assertEqual(
                plan_module.EXIT_ERROR,
                plan_module.main(
                    [
                        str(PILOT),
                        "--corpus",
                        str(ROADMAP / "epics"),
                        "--snapshot",
                        str(CONVERGED),
                        "--live",
                    ]
                ),
            )

    def test_cli_validates_the_whole_corpus_before_reading_anything(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "human-input.yaml").write_text(
                PILOT.read_text(encoding="utf-8"), encoding="utf-8"
            )
            (directory / "broken.yaml").write_text("not: a manifest\n", encoding="utf-8")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = plan_module.main(
                    [
                        "--corpus",
                        str(directory),
                        "--snapshot",
                        str(CONVERGED),
                    ]
                )
        self.assertEqual(plan_module.EXIT_ERROR, code)

    def test_the_plan_schema_is_a_valid_schema(self) -> None:
        # _load_schema runs Draft202012Validator.check_schema.
        self.assertIsInstance(validate._load_schema(plan_module.DEFAULT_PLAN_SCHEMA), dict)


if __name__ == "__main__":
    unittest.main()
