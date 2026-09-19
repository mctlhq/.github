from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml

ROADMAP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROADMAP / "scripts"))
sys.path.insert(0, str(ROADMAP / "tests"))

import apply as apply_module  # noqa: E402
import github_apply  # noqa: E402
import github_graph  # noqa: E402
import mutations  # noqa: E402
import plan as plan_module  # noqa: E402
import reconcile  # noqa: E402

PILOT = ROADMAP / "epics" / "human-input.yaml"
CONVERGED = ROADMAP / "fixtures" / "human-input" / "converged-fixture.json"

ROOT_ISSUE = "mctlhq/.github#42"
CORE = "mctlhq/mctl-agents#333"
API = "mctlhq/mctl-api#261"
TELEGRAM = "mctlhq/mctl-telegram#571"
PORTAL = "mctlhq/mctl-portal#124"
DOCS = "mctlhq/mctl-docs#106"
FOREIGN = "mctlhq/mctl-web#5"
EXTERNAL = "mctlhq/mctl-telegram#443"

REVISION = "0123456789abcdef0123456789abcdef01234567"


class _RecordingOpener:
    """Stands in for urllib's opener and records every call that reaches it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []

    def open(self, request, timeout=None):
        self.calls.append((request.get_method(), request.full_url, request.data))
        raise AssertionError("a refused request reached the transport")


class ApplyTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.safe_load(PILOT.read_text(encoding="utf-8"))
        cls.converged = json.loads(CONVERGED.read_text(encoding="utf-8"))
        desired = reconcile.desired_graph(cls.document)
        # Two sets, deliberately. `owned` is what the manifest may be written
        # against; `authored` additionally holds the `externalDependsOn` ref,
        # which may be the related end of a write but never its target.
        cls.owned = frozenset(desired.owned_keys())
        cls.authored = frozenset(desired.authored_keys())
        assert cls.owned < cls.authored, "the pilot must name an external ref"
        cls.result_schema = apply_module.load_schema()

    def _plan(self, snapshot: dict) -> dict:
        diff = reconcile.reconcile(
            PILOT, self.document, github_graph.FixtureGraphSource(snapshot)
        )
        return plan_module.plan(diff, document=self.document)

    def _apply(
        self,
        snapshot: dict,
        *,
        execute: bool = True,
        plan_document: dict | None = None,
        max_operations: int = apply_module.DEFAULT_MAX_OPERATIONS,
    ) -> tuple[list[dict], github_apply.FakeMutator]:
        mutator = github_apply.FakeMutator(snapshot, self.owned, self.authored)
        operations = apply_module.apply_plan(
            plan_document if plan_document is not None else self._plan(snapshot),
            source_factory=lambda: github_graph.FixtureGraphSource(snapshot),
            mutator=mutator,
            authored=self.authored,
            execute=execute,
            max_operations=max_operations,
        )
        return operations, mutator

    def _drift(self, snapshot: dict) -> dict:
        return reconcile.reconcile(
            PILOT, self.document, github_graph.FixtureGraphSource(snapshot)
        )


class ApplyTest(ApplyTestBase):
    # -- T6 --------------------------------------------------------------

    def test_applying_one_operation_converges_the_graph(self) -> None:
        snapshot = mutations.drop_parent_edge(self.converged, API)
        self.assertTrue(reconcile.has_drift(self._drift(snapshot)))

        operations, mutator = self._apply(snapshot)
        self.assertEqual([apply_module.APPLIED], [op["outcome"] for op in operations])
        self.assertEqual(["AddSubIssue"], [op["type"] for op in operations])
        self.assertEqual(1, len(mutator.writes))

        # The mutated snapshot is still the published contract, not a private
        # shape only this test can read.
        self.assertEqual([], github_graph.snapshot_errors(mutator.state))
        self.assertFalse(reconcile.has_drift(self._drift(mutator.state)))

    def test_reconcile_exits_zero_against_the_applied_snapshot(self) -> None:
        snapshot = mutations.drop_dependency(self.converged, API, CORE)
        _, mutator = self._apply(snapshot)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "applied.json"
            path.write_text(json.dumps(mutator.state), encoding="utf-8")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = reconcile.main(
                    [
                        str(PILOT),
                        "--corpus",
                        str(ROADMAP / "epics"),
                        "--snapshot",
                        str(path),
                    ]
                )
        self.assertEqual(reconcile.EXIT_CONVERGED, code)

    def test_every_operation_type_converges_its_own_family(self) -> None:
        cases = {
            "AddSubIssue": lambda s: mutations.drop_parent_edge(s, API),
            "MoveSubIssue": lambda s: mutations.repoint_parent(s, API, CORE),
            "AddDependency": lambda s: mutations.drop_dependency(s, API, CORE),
            "RemoveDependency": lambda s: mutations.add_dependency(s, DOCS, PORTAL),
        }
        for kind, mutate in cases.items():
            with self.subTest(operation=kind):
                snapshot = mutate(self.converged)
                operations, mutator = self._apply(snapshot)
                self.assertEqual([kind], [op["type"] for op in operations])
                self.assertEqual(
                    [apply_module.APPLIED], [op["outcome"] for op in operations]
                )
                self.assertEqual([], github_graph.snapshot_errors(mutator.state))
                self.assertFalse(reconcile.has_drift(self._drift(mutator.state)))

    # -- T7 --------------------------------------------------------------

    def test_replay_is_already_satisfied_and_writes_nothing(self) -> None:
        snapshot = mutations.drop_parent_edge(self.converged, API)
        document = self._plan(snapshot)
        self._apply(snapshot, plan_document=document)

        operations, mutator = self._apply(snapshot, plan_document=document)
        self.assertEqual(
            [apply_module.ALREADY_SATISFIED], [op["outcome"] for op in operations]
        )
        self.assertEqual([], mutator.writes)
        self.assertNotIn("reason", operations[0])

    def test_an_already_converged_graph_plans_and_writes_nothing(self) -> None:
        snapshot = copy.deepcopy(self.converged)
        operations, mutator = self._apply(snapshot)
        self.assertEqual([], operations)
        self.assertEqual([], mutator.writes)

    # -- T8 --------------------------------------------------------------

    def test_a_graph_changed_after_planning_is_skipped_not_overwritten(self) -> None:
        planned = mutations.drop_parent_edge(self.converged, API)
        document = self._plan(planned)

        # Somebody re-parented the issue between plan and apply.
        changed = mutations.repoint_parent(self.converged, API, CORE)
        operations, mutator = self._apply(changed, plan_document=document)
        self.assertEqual([apply_module.SKIPPED], [op["outcome"] for op in operations])
        self.assertEqual(
            [apply_module.PRECONDITION_CHANGED], [op["reason"] for op in operations]
        )
        self.assertEqual([], mutator.writes)

    def test_an_endpoint_that_moved_identity_is_skipped(self) -> None:
        planned = mutations.drop_dependency(self.converged, API, CORE)
        document = self._plan(planned)

        moved = mutations.redirect(planned, API, "mctlhq/mctl-web#999")
        operations, mutator = self._apply(moved, plan_document=document)
        self.assertEqual([apply_module.SKIPPED], [op["outcome"] for op in operations])
        self.assertEqual(
            [apply_module.PRECONDITION_CHANGED], [op["reason"] for op in operations]
        )
        self.assertEqual([], mutator.writes)

    def test_a_failed_write_is_recorded_and_the_run_continues(self) -> None:
        snapshot = mutations.drop_parent_edge(self.converged, API)
        snapshot = mutations.drop_dependency(snapshot, DOCS, CORE)
        document = self._plan(snapshot)
        self.assertEqual(2, len(document["operations"]))

        class _BrokenMutator(github_apply.FakeMutator):
            def _perform(self, request):
                if request.kind == github_apply.ADD_SUB_ISSUE:
                    raise github_apply.MutationFailed("HTTP 500")
                super()._perform(request)

        mutator = _BrokenMutator(snapshot, self.authored)
        operations = apply_module.apply_plan(
            document,
            source_factory=lambda: github_graph.FixtureGraphSource(snapshot),
            mutator=mutator,
            authored=self.authored,
            execute=True,
        )
        outcomes = {op["type"]: op["outcome"] for op in operations}
        self.assertEqual(apply_module.FAILED, outcomes["AddSubIssue"])
        self.assertEqual(apply_module.APPLIED, outcomes["AddDependency"])
        failed = next(op for op in operations if op["outcome"] == apply_module.FAILED)
        self.assertEqual(apply_module.WRITE_FAILED, failed["reason"])

    def test_a_half_finished_move_names_the_orphaned_child(self) -> None:
        snapshot = mutations.repoint_parent(self.converged, API, CORE)
        document = self._plan(snapshot)

        class _AddFails(github_apply.FakeMutator):
            def _perform(self, request):
                if request.kind == github_apply.ADD_SUB_ISSUE:
                    raise github_apply.MutationFailed("HTTP 422")
                super()._perform(request)

        mutator = _AddFails(snapshot, self.authored)
        operations = apply_module.apply_plan(
            document,
            source_factory=lambda: github_graph.FixtureGraphSource(snapshot),
            mutator=mutator,
            authored=self.authored,
            execute=True,
        )
        self.assertEqual([apply_module.FAILED], [op["outcome"] for op in operations])
        self.assertEqual(
            [apply_module.MOVE_INCOMPLETE], [op["reason"] for op in operations]
        )
        self.assertIn(mutations.ref(API), operations[0]["targets"])
        # The remove half did happen, so the child is genuinely orphaned now --
        # recorded, not hidden, and the next replay finishes the move.
        self.assertIsNone(
            next(
                item
                for item in mutator.state["issues"]
                if item["requested"] == mutations.ref(API)
            )["parent"]
        )

    # -- T9 --------------------------------------------------------------

    def test_without_execute_nothing_is_written(self) -> None:
        snapshot = mutations.drop_parent_edge(self.converged, API)
        before = copy.deepcopy(snapshot)
        operations, mutator = self._apply(snapshot, execute=False)
        self.assertEqual([apply_module.SKIPPED], [op["outcome"] for op in operations])
        self.assertEqual(
            [apply_module.NOT_EXECUTED], [op["reason"] for op in operations]
        )
        self.assertEqual([], mutator.writes)
        self.assertEqual(before, snapshot)

    def test_max_operations_refuses_rather_than_truncating(self) -> None:
        snapshot = mutations.drop_parent_edge(self.converged, API)
        snapshot = mutations.drop_dependency(snapshot, DOCS, CORE)
        before = copy.deepcopy(snapshot)
        with self.assertRaises(apply_module.ApplyRefused):
            self._apply(snapshot, max_operations=1)
        self.assertEqual(before, snapshot)

    def test_a_refused_plan_is_never_applied(self) -> None:
        snapshot = mutations.mark_missing(self.converged, API)
        before = copy.deepcopy(snapshot)
        with self.assertRaises(apply_module.ApplyRefused):
            self._apply(snapshot)
        self.assertEqual(before, snapshot)

    def test_a_hand_widened_plan_cannot_reach_a_foreign_issue(self) -> None:
        """Defense in depth: the assertion runs again against the live operation."""

        snapshot = mutations.drop_parent_edge(self.converged, API)
        document = self._plan(snapshot)
        document["operations"][0]["child"] = mutations.ref(FOREIGN)
        before = copy.deepcopy(snapshot)
        with self.assertRaises(plan_module.PlanRefused):
            self._apply(snapshot, plan_document=document)
        self.assertEqual(before, snapshot)

    def test_a_move_out_of_an_external_parent_is_refused(self) -> None:
        """`externalDependsOn` is not write authority over that issue.

        An owned child observed under an issue the manifest only *depends on*
        plans a legitimate `MoveSubIssue` back under the epic root -- and the
        remove half of that move is a DELETE against the foreign repository.
        The plan is authored (the external ref is in `authored_keys()`), so the
        only thing standing between it and the wire is the mutator's narrower
        target boundary.
        """

        snapshot = mutations.repoint_parent(self.converged, TELEGRAM, EXTERNAL)
        document = self._plan(snapshot)
        self.assertEqual(["MoveSubIssue"], [op["type"] for op in document["operations"]])
        self.assertEqual(
            mutations.ref(EXTERNAL), document["operations"][0]["observedParent"]
        )

        before = copy.deepcopy(snapshot)
        with self.assertRaises(github_apply.MutationRefused) as refused:
            self._apply(snapshot, plan_document=document)
        self.assertIn("outside the owned set", str(refused.exception))
        self.assertIn("mctl-telegram#443", str(refused.exception))
        self.assertEqual(before, snapshot)

    def test_an_external_blocker_is_still_a_legitimate_related_end(self) -> None:
        """The other half of the split: the wide set still buys what it should."""

        snapshot = mutations.drop_dependency(self.converged, TELEGRAM, EXTERNAL)
        operations, mutator = self._apply(snapshot)
        self.assertEqual(["AddDependency"], [op["type"] for op in operations])
        self.assertEqual([apply_module.APPLIED], [op["outcome"] for op in operations])
        self.assertEqual(1, len(mutator.writes))
        self.assertEqual(("mctlhq/mctl-telegram", 443), mutator.writes[0].related)

    def test_missing_issue_ids_are_refused_before_the_first_write(self) -> None:
        """The id map is a guard, not a mid-run surprise.

        `_issue_id_resolver` raises `MutationRefused` when an id is absent, and
        `_write` only catches `MutationFailed`, so an incomplete map used to
        unwind the run from whichever operation first needed a missing id --
        after the earlier ones had already been transmitted.
        """

        snapshot = mutations.drop_parent_edge(self.converged, API)
        snapshot = mutations.drop_dependency(snapshot, DOCS, CORE)
        document = self._plan(snapshot)
        self.assertEqual(2, len(document["operations"]))
        prepared = [
            apply_module._Prepared(
                path=PILOT,
                document=document,
                mutator=github_apply.FakeMutator(snapshot, self.owned, self.authored),
                source_factory=lambda: github_graph.FixtureGraphSource(snapshot),
                owned=self.owned,
                authored=self.authored,
                git_revision=REVISION,
            )
        ]
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "ids.json"
            # The API issue has an id; the core issue, which the AddDependency
            # names as its blocker, does not.
            path.write_text(json.dumps({API: 11}), encoding="utf-8")
            resolve = apply_module._issue_id_resolver(str(path))

        with self.assertRaises(apply_module.ApplyRefused) as refused:
            apply_module._check_issue_ids(prepared, resolve, live=True)
        self.assertIn("--issue-ids is missing an id for", str(refused.exception))
        self.assertIn(CORE, str(refused.exception))

    def test_the_id_map_rejects_a_value_that_is_not_a_positive_int(self) -> None:
        """The loader holds the same rule the write client holds at transmission.

        `isinstance(value, int)` admitted `0` and negatives -- which
        `github_apply._issue_id` refuses, i.e. exactly the mid-run
        `MutationRefused` guard 6 is a preflight against -- and admitted `True`,
        which `_issue_id` accepts too, so a boolean was transmitted as an id.
        """

        with tempfile.TemporaryDirectory() as raw:
            for value in (0, -1, True, "11", 1.0):
                path = Path(raw) / "ids.json"
                path.write_text(json.dumps({API: value}), encoding="utf-8")
                with self.subTest(value=value):
                    with self.assertRaises(apply_module.ApplyError):
                        apply_module._issue_id_resolver(str(path))
            path.write_text(json.dumps({API: 11}), encoding="utf-8")
            self.assertEqual(
                11, apply_module._issue_id_resolver(str(path))(("mctlhq/mctl-api", 261))
            )

    def test_the_write_side_redirect_handler_has_no_default_refusal_type(self) -> None:
        """A write refusal must not be able to surface as a read error."""

        with self.assertRaises(TypeError):
            github_graph._RefusedRedirectHandler()

    def test_a_live_run_with_no_issue_ids_at_all_is_refused_up_front(self) -> None:
        """An absent map is the same guard failure as an incomplete one.

        `--issue-ids` omitted leaves every operation unresolvable, so the run
        aborted on the first one with `MutationRefused` out of
        `LiveMutator._issue_id` -- the mid-run shape the guard exists to
        prevent, reached by the widest possible input. An offline run is a
        different case: `FakeMutator` needs no ids and must stay runnable.
        """

        snapshot = mutations.drop_parent_edge(self.converged, API)
        document = self._plan(snapshot)
        self.assertEqual(1, len(document["operations"]))
        prepared = [
            apply_module._Prepared(
                path=PILOT,
                document=document,
                mutator=github_apply.FakeMutator(snapshot, self.owned, self.authored),
                source_factory=lambda: github_graph.FixtureGraphSource(snapshot),
                owned=self.owned,
                authored=self.authored,
                git_revision=REVISION,
            )
        ]
        with self.assertRaises(apply_module.ApplyRefused) as refused:
            apply_module._check_issue_ids(prepared, None, live=True)
        self.assertIn("--issue-ids", str(refused.exception))

        apply_module._check_issue_ids(prepared, None, live=False)

    def test_a_live_run_with_nothing_to_write_needs_no_issue_ids(self) -> None:
        """The guard is about operations, not about the flag being present."""

        document = self._plan(self.converged)
        self.assertEqual([], document["operations"])
        prepared = [
            apply_module._Prepared(
                path=PILOT,
                document=document,
                mutator=github_apply.FakeMutator(
                    self.converged, self.owned, self.authored
                ),
                source_factory=lambda: github_graph.FixtureGraphSource(self.converged),
                owned=self.owned,
                authored=self.authored,
                git_revision=REVISION,
            )
        ]
        apply_module._check_issue_ids(prepared, None, live=True)

    # -- T10 -------------------------------------------------------------

    def test_the_write_client_refuses_before_transmission(self) -> None:
        owned = frozenset({("mctlhq/.github", 42), ("mctlhq/mctl-api", 261)})
        opener = _RecordingOpener()
        mutator = github_apply.LiveMutator(
            "token", owned, opener=opener, resolve_id=lambda key: 7
        )
        target = ("mctlhq/.github", 42)
        related = ("mctlhq/mctl-api", 261)

        cases = {
            "a GET": github_apply.MutationRequest(
                kind=github_apply.ADD_SUB_ISSUE,
                method="GET",
                template=github_apply.SUB_ISSUES,
                path="/repos/mctlhq/.github/issues/42/sub_issues",
                target=target,
                related=related,
            ),
            "an endpoint outside the allow-list": github_apply.MutationRequest(
                kind=github_apply.ADD_SUB_ISSUE,
                method="POST",
                template="/repos/{owner}/{repo}/issues/{number}/comments",
                path="/repos/mctlhq/.github/issues/42/comments",
                target=target,
                related=related,
                body={"body": "hello"},
            ),
            "a cross-origin URL": github_apply.MutationRequest(
                kind=github_apply.ADD_SUB_ISSUE,
                method="POST",
                template=github_apply.SUB_ISSUES,
                path="https://attacker.example/repos/mctlhq/.github/issues/42/sub_issues",
                target=target,
                related=related,
                body={"sub_issue_id": 7},
            ),
            "a target outside the owned set": github_apply.MutationRequest(
                kind=github_apply.ADD_SUB_ISSUE,
                method="POST",
                template=github_apply.SUB_ISSUES,
                path="/repos/mctlhq/mctl-web/issues/5/sub_issues",
                target=("mctlhq/mctl-web", 5),
                related=related,
                body={"sub_issue_id": 7},
            ),
        }
        for name, request in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(github_apply.MutationRefused):
                    mutator._perform(request)
        self.assertEqual([], opener.calls)

    def test_the_vocabulary_refuses_an_unowned_identity(self) -> None:
        opener = _RecordingOpener()
        mutator = github_apply.LiveMutator(
            "token",
            {("mctlhq/.github", 42)},
            opener=opener,
            resolve_id=lambda key: 7,
        )
        with self.assertRaises(github_apply.MutationRefused):
            mutator.add_sub_issue(("mctlhq/.github", 42), ("mctlhq/mctl-web", 5))
        with self.assertRaises(github_apply.MutationRefused):
            mutator.add_dependency(("mctlhq/mctl-web", 5), ("mctlhq/.github", 42))
        self.assertEqual([], opener.calls)

    def test_live_writes_need_an_issue_id_resolver(self) -> None:
        opener = _RecordingOpener()
        mutator = github_apply.LiveMutator(
            "token", {("mctlhq/.github", 42), ("mctlhq/mctl-api", 261)}, opener=opener
        )
        with self.assertRaises(github_apply.MutationRefused):
            mutator.add_sub_issue(("mctlhq/.github", 42), ("mctlhq/mctl-api", 261))
        self.assertEqual([], opener.calls)

    def test_the_write_client_inherits_the_read_clients_origin_rules(self) -> None:
        for base in (
            "http://api.github.com",
            "api.github.com",
            "https://api.github.com@attacker.example",
            "https://api.github.com?token=leak",
        ):
            with self.subTest(base=base):
                with self.assertRaises(github_apply.MutationRefused):
                    github_apply.LiveMutator(
                        "token", set(), api_base=base, opener=_RecordingOpener()
                    )

    def test_a_write_refuses_to_follow_a_redirect(self) -> None:
        """urllib would turn a redirected POST into a GET that answers 200."""

        mutator = github_apply.LiveMutator("token", set(), resolve_id=lambda key: 7)
        handlers = [
            h for h in mutator._opener.handlers
            if isinstance(h, urllib.request.HTTPRedirectHandler)
        ]
        self.assertEqual(1, len(handlers))
        self.assertIsInstance(handlers[0], github_graph._RefusedRedirectHandler)
        self.assertNotIsInstance(handlers[0], github_graph._SameOriginRedirectHandler)

        request = urllib.request.Request(
            "https://api.github.com/repos/mctlhq/.github/issues/42/sub_issues",
            data=b'{"sub_issue_id": 7}',
            method="POST",
        )
        # Same origin, and still refused: the read client follows this one, and
        # following it is what silently downgrades the write to a read.
        moved = "https://api.github.com/repositories/42/issues/1/sub_issues"
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                with self.assertRaises(github_apply.MutationRefused):
                    handlers[0].redirect_request(
                        request, None, code, "Moved", {}, moved
                    )

    def test_the_read_client_still_follows_a_same_origin_redirect(self) -> None:
        """The write rule must not be pushed onto the reader: it needs them."""

        source = github_graph.LiveGraphSource("token")
        handler = next(
            h for h in source._opener.handlers
            if isinstance(h, urllib.request.HTTPRedirectHandler)
        )
        self.assertIsInstance(handler, github_graph._SameOriginRedirectHandler)
        same = "https://api.github.com/repositories/42/issues/1"
        request = urllib.request.Request("https://api.github.com/repos/o/r/issues/1")
        self.assertEqual(
            same, handler.redirect_request(request, None, 301, "Moved", {}, same).full_url
        )

    def test_the_allow_list_is_four_relation_endpoints(self) -> None:
        self.assertEqual(4, len(github_apply.ALLOWED))
        self.assertEqual({"POST", "DELETE"}, {method for method, _ in github_apply.ALLOWED})
        for _, template in github_apply.ALLOWED:
            self.assertIn("/issues/{number}", template)

    def test_the_offline_mutator_holds_the_same_guard(self) -> None:
        snapshot = copy.deepcopy(self.converged)
        mutator = github_apply.FakeMutator(snapshot, {("mctlhq/.github", 42)})
        with self.assertRaises(github_apply.MutationRefused):
            mutator.add_sub_issue(("mctlhq/.github", 42), ("mctlhq/mctl-api", 261))
        self.assertEqual([], mutator.writes)
        self.assertEqual(self.converged, snapshot)

    # -- T11 -------------------------------------------------------------

    def _result(self, snapshot: dict, **kwargs) -> dict:
        document = self._plan(snapshot)
        operations, _ = self._apply(snapshot, plan_document=document)
        return apply_module.result(
            document,
            operations,
            mode=apply_module.MODE_EXECUTE,
            actor="mctl-agents[bot]",
            git_revision=REVISION,
            manifests_selected=1,
            proposal={"id": "rp-42", "url": "https://example.invalid/proposals/42"},
            schema=self.result_schema,
            **kwargs,
        )

    def test_the_result_validates_and_carries_the_full_audit_block(self) -> None:
        document = self._result(mutations.drop_parent_edge(self.converged, API))
        self.assertEqual([], apply_module.schema_errors(document, self.result_schema))

        audit = document["audit"]
        self.assertEqual("mctl-agents[bot]", audit["actor"])
        self.assertEqual("rp-42", audit["proposal"]["id"])
        self.assertEqual(REVISION, audit["manifest"]["gitRevision"])
        self.assertEqual(
            "roadmap/epics/human-input.yaml", audit["manifest"]["path"]
        )
        self.assertEqual(reconcile._sha256(PILOT), audit["manifest"]["sha256"])
        self.assertEqual(64, len(audit["planId"]))
        self.assertIn(mutations.ref(API), audit["targets"])
        self.assertEqual(
            {"applied": 1, "alreadySatisfied": 0, "skipped": 0, "failed": 0},
            document["summary"],
        )

    def test_an_unattributed_run_records_an_explicit_null_proposal(self) -> None:
        document = self._plan(self.converged)
        built = apply_module.result(
            document,
            [],
            mode=apply_module.MODE_PLAN_ONLY,
            actor="operator",
            git_revision=REVISION,
            manifests_selected=1,
            schema=self.result_schema,
        )
        self.assertIn("proposal", built["audit"])
        self.assertIsNone(built["audit"]["proposal"])
        self.assertEqual([], built["audit"]["targets"])

    def test_the_result_carries_no_issue_prose(self) -> None:
        rendered = json.dumps(
            self._result(mutations.drop_parent_edge(self.converged, API))
        )
        for word in ('"title"', '"body"', '"state"', '"labels"'):
            self.assertNotIn(word, rendered)

    def test_the_content_check_rejects_a_polluted_result(self) -> None:
        document = self._result(mutations.drop_parent_edge(self.converged, API))
        polluted = copy.deepcopy(document)
        polluted["audit"]["targets"][0]["title"] = "Durable agent clarification"
        errors = apply_module._forbidden_content_errors(polluted)
        self.assertTrue(errors)
        self.assertTrue(any("title" in error for error in errors))

        multiline = copy.deepcopy(document)
        multiline["audit"]["actor"] = "bot\nwith a body pasted in"
        self.assertTrue(apply_module._forbidden_content_errors(multiline))

    def test_a_result_that_would_leak_is_never_emitted(self) -> None:
        document = self._plan(self.converged)
        with self.assertRaises(apply_module.ApplyError):
            apply_module.result(
                document,
                [],
                mode=apply_module.MODE_PLAN_ONLY,
                actor="operator\nwith prose",
                git_revision=REVISION,
                manifests_selected=1,
                schema=self.result_schema,
            )

    def test_an_abbreviated_git_revision_is_not_an_audit_record(self) -> None:
        document = self._plan(self.converged)
        with self.assertRaises(apply_module.ApplyError):
            apply_module.result(
                document,
                [],
                mode=apply_module.MODE_PLAN_ONLY,
                actor="operator",
                git_revision=REVISION[:7],
                manifests_selected=1,
                schema=self.result_schema,
            )

    # -- T12 -------------------------------------------------------------

    def test_the_human_input_acceptance_path(self) -> None:
        """A merged manifest converges the epic with no hand-maintained graph."""

        snapshot = mutations.drop_parent_edge(self.converged, TELEGRAM)
        snapshot = mutations.drop_dependency(snapshot, DOCS, API)
        snapshot = mutations.drop_dependency(snapshot, TELEGRAM, "mctlhq/mctl-telegram#443")

        document = self._plan(snapshot)
        self.assertEqual(
            ["AddDependency", "AddDependency", "AddSubIssue"],
            sorted(op["type"] for op in document["operations"]),
        )

        operations, mutator = self._apply(snapshot, plan_document=document)
        self.assertEqual(
            {apply_module.APPLIED}, {op["outcome"] for op in operations}
        )
        self.assertEqual([], github_graph.snapshot_errors(mutator.state))

        drift = self._drift(mutator.state)
        self.assertFalse(reconcile.has_drift(drift))
        self.assertEqual(0, drift["summary"]["drift"])


@unittest.skipIf(shutil.which("git") is None, "git is not available")
class ApplyCliTest(ApplyTestBase):
    """CLI guards, exercised against a real one-manifest checkout."""

    def _repository(self, directory: Path) -> Path:
        epics = directory / "roadmap" / "epics"
        epics.mkdir(parents=True)
        manifest = epics / "human-input.yaml"
        manifest.write_text(PILOT.read_text(encoding="utf-8"), encoding="utf-8")
        for args in (
            ["init", "-q", "-b", "main"],
            ["add", "-A"],
            [
                "-c",
                "user.email=roadmap@example.invalid",
                "-c",
                "user.name=roadmap",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "seed",
            ],
        ):
            subprocess.run(
                ["git", "-C", str(directory), *args], check=True, capture_output=True
            )
        return manifest

    @contextmanager
    def _recorded(self):
        """Hold on to the mutators the CLI builds, so writes can be counted.

        The CLI owns its mutator, and the offline one mutates a dict in memory:
        re-reading the snapshot file afterwards would show no change whether a
        guard refused the run or not, which is exactly the distinction these
        tests exist to make. `Mutator.writes` is the real accounting, recorded
        the moment a request passes the guard.
        """

        created: list[github_apply.FakeMutator] = []
        original = github_apply.FakeMutator.__init__

        def _init(mutator, snapshot, owned, authored=None):
            original(mutator, snapshot, owned, authored)
            created.append(mutator)

        github_apply.FakeMutator.__init__ = _init
        try:
            yield created
        finally:
            github_apply.FakeMutator.__init__ = original

    def _run(
        self, snapshot: dict, extra: list[str], *, dirty: bool = False
    ) -> tuple[int, str, list]:
        with tempfile.TemporaryDirectory() as raw_repo, tempfile.TemporaryDirectory() as raw_work:
            repository = Path(raw_repo)
            manifest = self._repository(repository)
            if dirty:
                (repository / "roadmap" / "epics" / "scratch.txt").write_text(
                    "uncommitted\n", encoding="utf-8"
                )
            # The snapshot lives outside the checkout: writing it inside would
            # dirty the tree the git guard is there to check.
            path = Path(raw_work) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")

            out, err = StringIO(), StringIO()
            with self._recorded() as mutators:
                with redirect_stdout(out), redirect_stderr(err):
                    code = apply_module.main(
                        [
                            str(manifest),
                            "--corpus",
                            str(repository / "roadmap" / "epics"),
                            "--snapshot",
                            str(path),
                            "--actor",
                            "roadmap-tests",
                        ]
                        + extra
                    )
            writes = [write for mutator in mutators for write in mutator.writes]
            return code, out.getvalue(), writes

    def test_execute_converges_and_exits_zero(self) -> None:
        snapshot = mutations.drop_parent_edge(self.converged, API)
        with tempfile.TemporaryDirectory() as raw:
            capture = Path(raw) / "applied.json"
            code, rendered, writes = self._run(
                snapshot, ["--execute", "--capture", str(capture)]
            )
            applied = json.loads(capture.read_text(encoding="utf-8"))

        self.assertEqual(apply_module.EXIT_OK, code)
        self.assertEqual(1, len(writes))
        document = json.loads(rendered)
        self.assertEqual([], apply_module.schema_errors(document, self.result_schema))
        self.assertEqual("execute", document["mode"])
        self.assertEqual(1, document["summary"]["applied"])
        self.assertEqual(40, len(document["audit"]["manifest"]["gitRevision"]))
        self.assertEqual([], github_graph.snapshot_errors(applied))
        self.assertFalse(reconcile.has_drift(self._drift(applied)))

    def test_plan_only_writes_nothing_and_exits_one(self) -> None:
        snapshot = mutations.drop_parent_edge(self.converged, API)
        code, rendered, writes = self._run(snapshot, [])
        self.assertEqual(apply_module.EXIT_SKIPPED, code)
        self.assertEqual([], writes)
        document = json.loads(rendered)
        self.assertEqual("plan-only", document["mode"])
        self.assertEqual(0, document["summary"]["applied"])
        self.assertEqual([], document["audit"]["targets"])

    def test_a_dirty_checkout_refuses_with_zero_writes(self) -> None:
        code, _, writes = self._run(
            mutations.drop_parent_edge(self.converged, API), ["--execute"], dirty=True
        )
        self.assertEqual(apply_module.EXIT_REFUSED, code)
        self.assertEqual([], writes)

    def test_a_mismatched_approval_hash_refuses_with_zero_writes(self) -> None:
        code, _, writes = self._run(
            mutations.drop_parent_edge(self.converged, API),
            ["--execute", "--approved-sha256", "b" * 64],
        )
        self.assertEqual(apply_module.EXIT_REFUSED, code)
        self.assertEqual([], writes)

    def test_the_matching_approval_hash_is_accepted(self) -> None:
        code, _, writes = self._run(
            mutations.drop_parent_edge(self.converged, API),
            ["--execute", "--approved-sha256", reconcile._sha256(PILOT)],
        )
        self.assertEqual(apply_module.EXIT_OK, code)
        self.assertEqual(1, len(writes))

    def test_max_operations_refuses_with_zero_writes(self) -> None:
        snapshot = mutations.drop_parent_edge(self.converged, API)
        snapshot = mutations.drop_dependency(snapshot, DOCS, CORE)
        code, _, writes = self._run(snapshot, ["--execute", "--max-operations", "1"])
        self.assertEqual(apply_module.EXIT_REFUSED, code)
        self.assertEqual([], writes)

    def test_a_refused_plan_exits_three_with_zero_writes(self) -> None:
        code, _, writes = self._run(
            mutations.mark_missing(self.converged, API), ["--execute"]
        )
        self.assertEqual(apply_module.EXIT_REFUSED, code)
        self.assertEqual([], writes)

    def test_a_move_out_of_an_external_parent_is_refused_before_any_write(self) -> None:
        """The owned-target boundary is a phase 1 guard, like every other one.

        `externalDependsOn` grants no write authority over a foreign issue, so
        a `MoveSubIssue` whose `observedParent` is that foreign ref must refuse
        before the first write of the run -- not unwind partway through phase
        2, which is where `check_request` alone would catch it.
        """

        snapshot = mutations.repoint_parent(self.converged, TELEGRAM, EXTERNAL)
        code, _, writes = self._run(snapshot, ["--execute"])
        self.assertEqual(apply_module.EXIT_REFUSED, code)
        self.assertEqual([], writes)

    def test_a_failed_operation_exits_four(self) -> None:
        snapshot = mutations.drop_parent_edge(self.converged, API)
        # The parent answers the re-read but refuses the write, so this is a
        # failed write rather than a changed precondition.
        original = github_apply.FakeMutator._observation

        def _broken(mutator, key):
            if key == ("mctlhq/.github", 42):
                raise github_apply.MutationFailed("HTTP 500")
            return original(mutator, key)

        github_apply.FakeMutator._observation = _broken
        try:
            code, rendered, writes = self._run(snapshot, ["--execute"])
        finally:
            github_apply.FakeMutator._observation = original
        self.assertEqual(apply_module.EXIT_FAILED, code)
        self.assertEqual(1, len(writes))
        document = json.loads(rendered)
        self.assertEqual(1, document["summary"]["failed"])
        self.assertEqual(
            apply_module.WRITE_FAILED, document["operations"][0]["reason"]
        )

    def test_live_without_a_token_is_an_auth_error_not_a_refusal(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""}):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = apply_module.main(
                    [
                        "--corpus",
                        str(ROADMAP / "epics"),
                        "--live",
                        "--actor",
                        "roadmap-tests",
                    ]
                )
        self.assertEqual(apply_module.EXIT_ERROR, code)

    def test_the_cli_requires_exactly_one_source(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = apply_module.main(
                ["--corpus", str(ROADMAP / "epics"), "--actor", "roadmap-tests"]
            )
        self.assertEqual(apply_module.EXIT_ERROR, code)

    def test_a_later_operation_raising_still_records_an_earlier_write(self) -> None:
        """One manifest, two operations: op 1's landed write must survive op 2.

        `_write` only catches `MutationFailed`. A `MutationRefused` (or any
        other exception) raised by the second operation used to unwind
        `apply_plan`'s list comprehension entirely, discarding the first
        operation's already-recorded `APPLIED` outcome even though its write
        had already landed -- the one outcome `README.md` and `apply.py`'s own
        module docstring say this tool may not produce.
        """

        snapshot = mutations.drop_parent_edge(self.converged, API)
        snapshot = mutations.drop_dependency(snapshot, DOCS, CORE)
        # AddDependency plans before AddSubIssue for this pilot; breaking the
        # second one lets the first one's write land first.
        original = github_apply.FakeMutator._perform

        def _broken(mutator, request):
            if request.kind == github_apply.ADD_SUB_ISSUE:
                raise github_apply.MutationRefused("simulated mid-manifest refusal")
            original(mutator, request)

        github_apply.FakeMutator._perform = _broken
        try:
            code, rendered, writes = self._run(snapshot, ["--execute"])
        finally:
            github_apply.FakeMutator._perform = original

        self.assertEqual(apply_module.EXIT_REFUSED, code)
        self.assertEqual(1, len(writes))

        document = json.loads(rendered)
        self.assertEqual([], apply_module.schema_errors(document, self.result_schema))
        self.assertEqual(1, document["summary"]["applied"])
        applied = [
            op for op in document["operations"] if op["outcome"] == apply_module.APPLIED
        ]
        self.assertEqual(["AddDependency"], [op["type"] for op in applied])


class ApplyMultiManifestTest(ApplyTestBase):
    """A run over more than one manifest is one run, not N runs in a trench coat.

    The whole-corpus invocation -- `apply.py --corpus roadmap/epics` with no
    positional argument -- is the default shape, so every guard has to mean the
    same thing there as it does for a single manifest: refuse the *run*, and
    refuse it before the first write.
    """

    OFFSET = 1000

    def _sibling_document(self) -> dict:
        """The pilot manifest again, on a disjoint set of issue numbers."""

        document = copy.deepcopy(self.document)
        # Named to sort *after* the pilot: the corpus is processed in path
        # order, and the test that asserts a partial audit record needs the
        # failing manifest to be the second one.
        document["metadata"]["name"] = "zz-sibling"
        document["spec"]["github"]["issue"]["number"] += self.OFFSET
        for item in document["spec"]["workItems"]:
            if "issue" in item:
                item["issue"]["number"] += self.OFFSET
            for external in item.get("externalDependsOn", []):
                external["number"] += self.OFFSET
        return document

    def _shift(self, snapshot: dict) -> dict:
        """The same graph, renumbered to match the sibling manifest."""

        result = copy.deepcopy(snapshot)
        for observation in result["issues"]:
            for ref in (
                observation.get("requested"),
                observation.get("resolved"),
                observation.get("parent"),
            ):
                if ref:
                    ref["number"] += self.OFFSET
            for ref in observation.get("subIssues", []) + observation.get(
                "blockedBy", []
            ):
                ref["number"] += self.OFFSET
        return result

    def _snapshot(self, first: dict, second: dict) -> dict:
        """One snapshot covering both manifests, as a live read would be."""

        merged = copy.deepcopy(first)
        merged["issues"] = first["issues"] + self._shift(second)["issues"]
        self.assertEqual([], github_graph.snapshot_errors(merged))
        return merged

    def _checkout(self, directory: Path) -> Path:
        epics = directory / "roadmap" / "epics"
        epics.mkdir(parents=True)
        (epics / "human-input.yaml").write_text(
            PILOT.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (epics / "zz-sibling.yaml").write_text(
            yaml.safe_dump(self._sibling_document(), sort_keys=False),
            encoding="utf-8",
        )
        for args in (
            ["init", "-q", "-b", "main"],
            ["add", "-A"],
            [
                "-c",
                "user.email=roadmap@example.invalid",
                "-c",
                "user.name=roadmap",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "seed",
            ],
        ):
            subprocess.run(
                ["git", "-C", str(directory), *args], check=True, capture_output=True
            )
        return epics

    @contextmanager
    def _recorded(
        self,
        fail_above: int | None = None,
        raising: type[BaseException] = github_apply.MutationRefused,
    ):
        """Capture the CLI's mutators, optionally breaking the sibling's writes.

        `fail_above` makes any write whose target issue number is above the
        threshold raise `raising` -- i.e. the sibling manifest fails while the
        pilot has already been applied, which is the shape that used to exit 3
        with no audit record at all.

        `raising` is a parameter because the interesting failures are not all
        `Exception`s: an operator's Ctrl-C during a long `--live --execute` run
        arrives as `KeyboardInterrupt`, which unwinds past every `except`
        clause `main` names.
        """

        created: list[github_apply.FakeMutator] = []
        original_init = github_apply.FakeMutator.__init__
        original_perform = github_apply.FakeMutator._perform

        def _init(mutator, snapshot, owned, authored=None):
            original_init(mutator, snapshot, owned, authored)
            created.append(mutator)

        def _perform(mutator, request):
            if fail_above is not None and request.target[1] > fail_above:
                raise raising("the sibling manifest is cursed")
            original_perform(mutator, request)

        github_apply.FakeMutator.__init__ = _init
        github_apply.FakeMutator._perform = _perform
        try:
            yield created
        finally:
            github_apply.FakeMutator.__init__ = original_init
            github_apply.FakeMutator._perform = original_perform

    def _run(
        self,
        first: dict,
        second: dict,
        extra: list[str],
        *,
        fail_above: int | None = None,
        raising: type[BaseException] = github_apply.MutationRefused,
    ) -> tuple[int | None, str, list]:
        with tempfile.TemporaryDirectory() as raw_repo, tempfile.TemporaryDirectory() as raw_work:
            epics = self._checkout(Path(raw_repo))
            path = Path(raw_work) / "snapshot.json"
            path.write_text(json.dumps(self._snapshot(first, second)), encoding="utf-8")

            out, err = StringIO(), StringIO()
            with self._recorded(fail_above, raising) as mutators:
                with redirect_stdout(out), redirect_stderr(err):
                    argv = [
                        "--corpus",
                        str(epics),
                        "--snapshot",
                        str(path),
                        "--actor",
                        "roadmap-tests",
                    ] + extra
                    try:
                        code = apply_module.main(argv)
                    except KeyboardInterrupt:
                        # An interrupt has no exit code to report, and the
                        # caller is asserting on what `main` emitted before it
                        # re-raised. Any other exception escaping `main` is a
                        # real failure and is left to propagate.
                        code = None
            writes = [write for mutator in mutators for write in mutator.writes]
            return code, out.getvalue(), writes

    def test_both_manifests_converge_in_one_run(self) -> None:
        code, rendered, writes = self._run(
            mutations.drop_parent_edge(self.converged, API),
            mutations.drop_parent_edge(self.converged, DOCS),
            ["--execute"],
        )
        self.assertEqual(apply_module.EXIT_OK, code)
        self.assertEqual(2, len(writes))
        document = json.loads(rendered)
        self.assertEqual("RoadmapApplyResultList", document["kind"])
        self.assertEqual(
            [1, 1], [item["summary"]["applied"] for item in document["items"]]
        )

    def test_the_operation_cap_is_summed_across_the_run(self) -> None:
        """One operation each, two manifests, a cap of one: the run is refused.

        Per manifest both plans fit, which is exactly why this used to pass a
        cap of 25 while transmitting up to 25 x N writes.
        """

        code, rendered, writes = self._run(
            mutations.drop_parent_edge(self.converged, API),
            mutations.drop_parent_edge(self.converged, DOCS),
            ["--execute", "--max-operations", "1"],
        )
        self.assertEqual(apply_module.EXIT_REFUSED, code)
        self.assertEqual([], writes)
        self.assertEqual("", rendered)

    def test_a_guard_on_the_second_manifest_fires_before_the_first_writes(self) -> None:
        """Approval is per manifest digest; the refusal is per run.

        `--approved-sha256` can only ever match one of the two manifests, so the
        other refuses. The point of the test is the write count: the guard is
        evaluated for every selected manifest before the first mutation, so the
        manifest that *was* approved is not applied on the way to the refusal.
        """

        digest = hashlib.sha256(PILOT.read_bytes()).hexdigest()
        code, rendered, writes = self._run(
            mutations.drop_parent_edge(self.converged, API),
            mutations.drop_parent_edge(self.converged, DOCS),
            ["--execute", "--approved-sha256", digest],
        )
        self.assertEqual(apply_module.EXIT_REFUSED, code)
        self.assertEqual([], writes)
        self.assertEqual("", rendered)

    def test_a_failure_after_a_write_still_emits_the_audit_record(self) -> None:
        """The one outcome this tool may not produce: an unrecorded write.

        Guards are hoisted, but transmission itself can still fail from the
        second manifest onwards. What must not happen is exit 3 with no output
        while a real mutation is sitting on GitHub -- the operator's only
        evidence is the `RoadmapApplyResult`.
        """

        code, rendered, writes = self._run(
            mutations.drop_parent_edge(self.converged, API),
            mutations.drop_parent_edge(self.converged, DOCS),
            ["--execute"],
            fail_above=self.OFFSET,
        )
        self.assertEqual(apply_module.EXIT_REFUSED, code)
        # The pilot's write landed; the sibling's was refused mid-flight.
        self.assertEqual(1, len(writes))

        document = json.loads(rendered)
        self.assertEqual([], apply_module.schema_errors(document, self.result_schema))
        self.assertEqual("human-input", document["epic"]["name"])
        self.assertEqual(1, document["summary"]["applied"])
        self.assertEqual(40, len(document["audit"]["manifest"]["gitRevision"]))
        # Two manifests were selected and one result came back: the artifact
        # says so on its own, without the exit code beside it.
        self.assertEqual(2, document["audit"]["manifestsSelected"])

    def test_an_interrupt_after_a_write_still_emits_the_audit_record(self) -> None:
        """Ctrl-C is not a licence to drop the record of a write that landed.

        `KeyboardInterrupt` derives from `BaseException`, so it unwinds past
        both `except` tuples in `main` -- the shape that used to end at exit
        130 with an empty stdout while the pilot's mutation was already on
        GitHub.
        """

        code, rendered, writes = self._run(
            mutations.drop_parent_edge(self.converged, API),
            mutations.drop_parent_edge(self.converged, DOCS),
            ["--execute"],
            fail_above=self.OFFSET,
            raising=KeyboardInterrupt,
        )
        self.assertIsNone(code)
        self.assertEqual(1, len(writes))

        document = json.loads(rendered)
        self.assertEqual([], apply_module.schema_errors(document, self.result_schema))
        self.assertEqual(1, document["summary"]["applied"])
        self.assertEqual(2, document["audit"]["manifestsSelected"])



if __name__ == "__main__":
    unittest.main()
