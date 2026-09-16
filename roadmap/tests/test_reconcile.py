from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml

ROADMAP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROADMAP / "scripts"))
sys.path.insert(0, str(ROADMAP / "tests"))

import github_graph  # noqa: E402
import mutations  # noqa: E402
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
EXTERNAL = "mctlhq/mctl-telegram#443"


def _types(entries: list[dict]) -> list[str]:
    return sorted(entry["type"] for entry in entries)


class _FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._raw = json.dumps(payload).encode() if payload is not None else b""
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingOpener:
    """Stands in for urllib's opener and remembers every call made through it."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.timeouts: list[float | None] = []

    def open(self, request, timeout=None):
        self.calls.append((request.get_method(), request.full_url, request.data))
        self.timeouts.append(timeout)
        url = request.full_url
        if url not in self.routes:
            url = url.split("?")[0]
        if url not in self.routes:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        payload = self.routes[url]
        if isinstance(payload, int):
            raise urllib.error.HTTPError(url, payload, "error", {}, None)
        if isinstance(payload, tuple):
            body, headers = payload
            return _FakeResponse(body, headers=headers)
        return _FakeResponse(payload)


class _RefusingSource:
    """A graph source that fails the test if anything asks it to observe."""

    def __init__(self):
        self.calls = 0

    def snapshot(self, keys):
        self.calls += 1
        raise AssertionError("network access attempted before validation passed")


class ReconcileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.safe_load(PILOT.read_text(encoding="utf-8"))
        cls.schema = validate._load_schema(validate.DEFAULT_SCHEMA)
        cls.diff_schema = json.loads(
            (ROADMAP / "schemas" / "roadmap-diff.schema.json").read_text(
                encoding="utf-8"
            )
        )
        cls.converged = json.loads(CONVERGED.read_text(encoding="utf-8"))

    def _diff(self, snapshot: dict, document: dict | None = None) -> dict:
        source = github_graph.FixtureGraphSource(snapshot)
        return reconcile.reconcile(PILOT, document or self.document, source)

    # -- T1, T10 ---------------------------------------------------------

    def test_converged_fixture_has_no_drift(self) -> None:
        result = self._diff(self.converged)
        self.assertEqual(0, result["summary"]["drift"])
        self.assertEqual([], result["hierarchy"])
        self.assertEqual([], result["dependency"])
        self.assertFalse(reconcile.has_drift(result))

    def test_unbound_work_item_is_informational_only(self) -> None:
        result = self._diff(self.converged)
        self.assertEqual(["BindingUnbound"], _types(result["binding"]))
        entry = result["binding"][0]
        self.assertEqual("devloop-e2e", entry["owner"])
        self.assertEqual("informational", entry["severity"])
        self.assertEqual(1, result["summary"]["informational"])

    # -- T2, T3 ----------------------------------------------------------

    def test_missing_parent_edge_is_reported_once(self) -> None:
        result = self._diff(mutations.drop_parent_edge(self.converged, API))
        self.assertEqual(["HierarchyMissingParent"], _types(result["hierarchy"]))
        self.assertEqual([], result["dependency"])
        entry = result["hierarchy"][0]
        self.assertEqual(mutations.ref(API), entry["child"])
        self.assertEqual(mutations.ref(ROOT_ISSUE), entry["expectedParent"])
        self.assertEqual(1, result["summary"]["drift"])

    def test_wrong_parent_reports_expected_and_observed(self) -> None:
        result = self._diff(mutations.repoint_parent(self.converged, API, CORE))
        self.assertEqual(["HierarchyWrongParent"], _types(result["hierarchy"]))
        entry = result["hierarchy"][0]
        self.assertEqual(mutations.ref(ROOT_ISSUE), entry["expectedParent"])
        self.assertEqual(mutations.ref(CORE), entry["observedParent"])
        self.assertEqual([], result["dependency"])

    def test_unowned_child_is_reported_but_is_not_drift(self) -> None:
        result = self._diff(
            mutations.add_unexpected_child(self.converged, ROOT_ISSUE, "mctlhq/mctl-web#5")
        )
        self.assertEqual(["HierarchyUnexpectedChild"], _types(result["hierarchy"]))
        entry = result["hierarchy"][0]
        self.assertEqual("informational", entry["severity"])
        self.assertEqual("epic", entry["owner"])
        self.assertEqual(mutations.ref("mctlhq/mctl-web#5"), entry["child"])
        self.assertEqual(mutations.ref(ROOT_ISSUE), entry["observedParent"])
        self.assertFalse(reconcile.has_drift(result))

    # -- T4, T5 ----------------------------------------------------------

    def test_missing_dependency_leaves_hierarchy_alone(self) -> None:
        result = self._diff(mutations.drop_dependency(self.converged, API, CORE))
        self.assertEqual(["DependencyMissing"], _types(result["dependency"]))
        self.assertEqual([], result["hierarchy"])
        entry = result["dependency"][0]
        self.assertEqual(mutations.ref(API), entry["blocked"])
        self.assertEqual(mutations.ref(CORE), entry["blocker"])

    def test_unauthored_dependency_is_reported(self) -> None:
        result = self._diff(mutations.add_dependency(self.converged, DOCS, PORTAL))
        self.assertEqual(["DependencyUnexpected"], _types(result["dependency"]))
        self.assertEqual([], result["hierarchy"])

    # -- T6 --------------------------------------------------------------

    def test_unresolvable_binding_does_not_cascade(self) -> None:
        result = self._diff(mutations.mark_missing(self.converged, API))
        self.assertEqual(
            ["BindingIssueNotFound", "BindingUnbound"], _types(result["binding"])
        )
        self.assertEqual([], result["hierarchy"])
        self.assertEqual([], result["dependency"])
        self.assertEqual(1, result["summary"]["drift"])

    # -- T7 --------------------------------------------------------------

    def test_redirect_is_binding_drift_and_relations_stay_green(self) -> None:
        moved = mutations.redirect(self.converged, PORTAL, "mctlhq/mctl-web#999")
        result = self._diff(moved)
        self.assertEqual(
            ["BindingRedirected", "BindingUnbound"], _types(result["binding"])
        )
        entry = next(
            item for item in result["binding"] if item["type"] == "BindingRedirected"
        )
        self.assertEqual(mutations.ref(PORTAL), entry["requested"])
        self.assertEqual(mutations.ref("mctlhq/mctl-web#999"), entry["resolved"])
        self.assertEqual([], result["hierarchy"])
        self.assertEqual([], result["dependency"])
        self.assertEqual(1, result["summary"]["drift"])

    # -- T8 --------------------------------------------------------------

    def test_mutators_are_pure_and_restoration_returns_to_green(self) -> None:
        original = copy.deepcopy(self.converged)
        cases = [
            lambda s: mutations.drop_parent_edge(s, API),
            lambda s: mutations.repoint_parent(s, API, CORE),
            lambda s: mutations.drop_dependency(s, API, CORE),
            lambda s: mutations.add_dependency(s, DOCS, PORTAL),
            lambda s: mutations.mark_missing(s, API),
            lambda s: mutations.redirect(s, PORTAL, "mctlhq/mctl-web#999"),
            lambda s: mutations.add_second_parent(s, API, CORE),
        ]
        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                mutated = mutate(self.converged)
                self.assertEqual(original, self.converged, "mutator changed its input")
                self.assertNotEqual(original, mutated, "mutator changed nothing")
                self.assertTrue(reconcile.has_drift(self._diff(mutated)))
                self.assertFalse(reconcile.has_drift(self._diff(original)))

    # -- T9 --------------------------------------------------------------

    def test_phase_order_creates_no_dependency_edge(self) -> None:
        document = copy.deepcopy(self.document)
        for item in document["spec"]["workItems"]:
            item.pop("dependsOn", None)
            item.pop("externalDependsOn", None)
        desired = reconcile.desired_graph(document)
        self.assertEqual((), desired.dependencies)
        self.assertTrue(len(document["spec"]["phases"]) > 1)

    # -- T11, T12 --------------------------------------------------------

    def _corpus(self, directory: Path, extra: dict) -> Path:
        (directory / "human-input.yaml").write_text(
            PILOT.read_text(encoding="utf-8"), encoding="utf-8"
        )
        other = directory / "other.yaml"
        other.write_text(yaml.safe_dump(extra, sort_keys=False), encoding="utf-8")
        return directory / "human-input.yaml"

    def _unrelated_manifest(self) -> dict:
        return {
            "apiVersion": "roadmap.mctl.ai/v1alpha1",
            "kind": "EpicDefinition",
            "metadata": {"name": "other", "owner": "mctl-agents"},
            "spec": {
                "title": "Other",
                "goal": "Exercise corpus invariants.",
                "lifecycle": "active",
                "github": {"issue": {"repository": "mctlhq/.github", "number": 99}},
                "phases": [{"id": "build", "title": "Build"}],
                "workItems": [
                    {
                        "id": "a",
                        "phase": "build",
                        "required": True,
                        "issue": {"repository": "mctlhq/mctl-web", "number": 7},
                    }
                ],
                "completion": {"mode": "allRequired"},
                "successCriteria": ["It works."],
            },
        }

    def _run_main(self, argv: list[str]) -> tuple[int, _RefusingSource]:
        source = _RefusingSource()
        with mock.patch.object(reconcile, "_build_source", return_value=source):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = reconcile.main(argv)
        return code, source

    def test_unselected_manifest_duplicate_binding_fails_before_any_read(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            other = self._unrelated_manifest()
            # Same issue the pilot binds, spelled in a different case.
            other["spec"]["workItems"][0]["issue"] = {
                "repository": "MCTLHQ/MCTL-API",
                "number": 261,
            }
            selected = self._corpus(directory, other)
            code, source = self._run_main(
                [str(selected), "--corpus", str(directory), "--snapshot", str(CONVERGED)]
            )
            self.assertEqual(reconcile.EXIT_ERROR, code)
            self.assertEqual(0, source.calls)

    def test_unselected_manifest_duplicate_name_fails_before_any_read(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            other = self._unrelated_manifest()
            other["metadata"]["name"] = "human-input"
            selected = self._corpus(directory, other)
            code, source = self._run_main(
                [str(selected), "--corpus", str(directory), "--snapshot", str(CONVERGED)]
            )
            self.assertEqual(reconcile.EXIT_ERROR, code)
            self.assertEqual(0, source.calls)

    # -- T13 -------------------------------------------------------------

    def test_live_source_refuses_non_get_before_transmission(self) -> None:
        opener = _RecordingOpener({})
        source = github_graph.LiveGraphSource("token", opener=opener)
        with self.assertRaises(github_graph.WriteAttempted):
            source._request("POST", "https://api.github.com/repos/o/r/issues/1")
        with self.assertRaises(github_graph.WriteAttempted):
            source._request(
                "GET", "https://api.github.com/repos/o/r/issues/1", body=b"{}"
            )
        self.assertEqual([], opener.calls)

    def test_live_source_requires_a_token(self) -> None:
        with self.assertRaises(github_graph.ObservationError):
            github_graph.LiveGraphSource("")

    # -- T14 -------------------------------------------------------------

    def _routes(self) -> dict[str, object]:
        base = "https://api.github.com/repos"
        return {
            f"{base}/mctlhq/example/issues/1": {
                "number": 1,
                "state": "open",
                "repository_url": f"{base}/mctlhq/example",
            },
            f"{base}/mctlhq/example/issues/1/parent": {
                "number": 9,
                "repository_url": f"{base}/mctlhq/example",
            },
            f"{base}/mctlhq/example/issues/1/sub_issues": [
                {"number": 2, "repository_url": f"{base}/mctlhq/example"}
            ],
            f"{base}/mctlhq/example/issues/1/dependencies/blocked_by": [
                {"number": 3, "repository_url": f"{base}/mctlhq/other"}
            ],
        }

    def test_live_source_normalizes_every_native_relation(self) -> None:
        opener = _RecordingOpener(self._routes())
        source = github_graph.LiveGraphSource("token", opener=opener)
        snapshot = source.snapshot([("mctlhq/example", 1)])

        self.assertEqual([], github_graph.snapshot_errors(snapshot))
        observation = snapshot["issues"][0]
        self.assertTrue(observation["found"])
        self.assertEqual({"repository": "mctlhq/example", "number": 9}, observation["parent"])
        self.assertEqual(
            [{"repository": "mctlhq/example", "number": 2}], observation["subIssues"]
        )
        self.assertEqual(
            [{"repository": "mctlhq/other", "number": 3}], observation["blockedBy"]
        )
        self.assertTrue(all(method == "GET" for method, _, _ in opener.calls))
        self.assertTrue(all(body is None for _, _, body in opener.calls))

    def test_live_source_reads_identity_from_the_body_not_the_url(self) -> None:
        base = "https://api.github.com/repos"
        routes = {
            f"{base}/mctlhq/old/issues/1": {
                "number": 77,
                "repository_url": f"{base}/mctlhq/new",
            }
        }
        source = github_graph.LiveGraphSource("token", opener=_RecordingOpener(routes))
        snapshot = source.snapshot([("mctlhq/old", 1)])
        observation = snapshot["issues"][0]
        self.assertEqual({"repository": "mctlhq/old", "number": 1}, observation["requested"])
        self.assertEqual({"repository": "mctlhq/new", "number": 77}, observation["resolved"])

    def test_relations_are_fetched_from_the_resolved_identity(self) -> None:
        """A transferred issue's relations live at its new address, not its old one."""

        base = "https://api.github.com/repos"
        routes = {
            f"{base}/mctlhq/old/issues/1": {
                "number": 77,
                "repository_url": f"{base}/mctlhq/new",
            },
            f"{base}/mctlhq/new/issues/77/parent": {
                "number": 5,
                "repository_url": f"{base}/mctlhq/new",
            },
            f"{base}/mctlhq/new/issues/77/sub_issues": [
                {"number": 8, "repository_url": f"{base}/mctlhq/new"}
            ],
            f"{base}/mctlhq/new/issues/77/dependencies/blocked_by": [
                {"number": 9, "repository_url": f"{base}/mctlhq/new"}
            ],
        }
        opener = _RecordingOpener(routes)
        source = github_graph.LiveGraphSource("token", opener=opener)
        observation = source.snapshot([("mctlhq/old", 1)])["issues"][0]

        self.assertEqual({"repository": "mctlhq/new", "number": 5}, observation["parent"])
        self.assertEqual(
            [{"repository": "mctlhq/new", "number": 8}], observation["subIssues"]
        )
        self.assertEqual(
            [{"repository": "mctlhq/new", "number": 9}], observation["blockedBy"]
        )
        relation_calls = [
            url for _, url, _ in opener.calls if "/issues/1/" in url
        ]
        self.assertEqual([], relation_calls, "relations were asked of the old address")

    def test_paginated_relations_are_read_to_the_last_page(self) -> None:
        """A relation list truncated to page one would pass as complete."""

        base = "https://api.github.com/repos/mctlhq/example/issues/1"
        issue_url = "https://api.github.com/repos/mctlhq/example"
        page_two = f"{base}/sub_issues?per_page=100&page=2"
        routes = {
            base: {"number": 1, "repository_url": issue_url},
            f"{base}/sub_issues?per_page=100": (
                [{"number": 2, "repository_url": issue_url}],
                {"Link": f'<{page_two}>; rel="next", <{page_two}>; rel="last"'},
            ),
            page_two: [{"number": 3, "repository_url": issue_url}],
        }
        opener = _RecordingOpener(routes)
        source = github_graph.LiveGraphSource("token", opener=opener)
        observation = source.snapshot([("mctlhq/example", 1)])["issues"][0]

        self.assertEqual(
            [
                {"repository": "mctlhq/example", "number": 2},
                {"repository": "mctlhq/example", "number": 3},
            ],
            observation["subIssues"],
        )
        self.assertIn(page_two, [url for _, url, _ in opener.calls])

    def test_malformed_provider_responses_are_errors_not_absences(self) -> None:
        """Unreadable data must never be reported as an observed lack of relations."""

        base = "https://api.github.com/repos/mctlhq/example/issues/1"
        issue = {"number": 1, "repository_url": "https://api.github.com/repos/mctlhq/example"}
        cases = {
            "issue body is not an object": {base: ["unexpected"]},
            "parent body is not an object": {base: issue, f"{base}/parent": ["unexpected"]},
            "sub-issues body is not an array": {
                base: issue,
                f"{base}/sub_issues": {"message": "unexpected"},
            },
            "blocked-by holds a non-object": {
                base: issue,
                f"{base}/sub_issues": [],
                f"{base}/dependencies/blocked_by": ["unexpected"],
            },
        }
        for name, routes in cases.items():
            with self.subTest(case=name):
                source = github_graph.LiveGraphSource(
                    "token", opener=_RecordingOpener(routes)
                )
                with self.assertRaises(github_graph.ObservationError):
                    source.snapshot([("mctlhq/example", 1)])

    def test_live_source_reports_a_missing_issue_as_not_found(self) -> None:
        source = github_graph.LiveGraphSource("token", opener=_RecordingOpener({}))
        snapshot = source.snapshot([("mctlhq/example", 1)])
        self.assertEqual(False, snapshot["issues"][0]["found"])
        self.assertNotIn("resolved", snapshot["issues"][0])

    def test_live_requests_carry_a_timeout(self) -> None:
        """A hung endpoint must fail, not stall a read-only run forever."""

        opener = _RecordingOpener(self._routes())
        source = github_graph.LiveGraphSource("token", opener=opener)
        source.snapshot([("mctlhq/example", 1)])
        self.assertTrue(opener.timeouts)
        self.assertTrue(all(value for value in opener.timeouts))

    def test_a_timed_out_read_is_an_observation_error(self) -> None:
        class _Hanging:
            def open(self, request, timeout=None):
                raise TimeoutError("timed out")

        source = github_graph.LiveGraphSource("token", opener=_Hanging())
        with self.assertRaises(github_graph.ObservationError):
            source.snapshot([("mctlhq/example", 1)])

    # -- T15 -------------------------------------------------------------

    def test_output_is_byte_identical_for_identical_input(self) -> None:
        first = json.dumps(self._diff(self.converged), sort_keys=True)
        second = json.dumps(self._diff(copy.deepcopy(self.converged)), sort_keys=True)
        self.assertEqual(first, second)

    def test_input_ordering_does_not_change_the_output(self) -> None:
        shuffled = copy.deepcopy(self.converged)
        shuffled["issues"] = list(reversed(shuffled["issues"]))
        for observation in shuffled["issues"]:
            for relation in ("subIssues", "blockedBy"):
                if relation in observation:
                    observation[relation] = list(reversed(observation[relation]))
        self.assertEqual(
            json.dumps(self._diff(self.converged), sort_keys=True),
            json.dumps(self._diff(shuffled), sort_keys=True),
        )

    def test_diff_carries_the_exact_manifest_bytes_digest(self) -> None:
        import hashlib

        expected = hashlib.sha256(PILOT.read_bytes()).hexdigest()
        self.assertEqual(expected, self._diff(self.converged)["epic"]["manifest"]["sha256"])

    # -- T16 -------------------------------------------------------------

    def test_output_validates_converged_and_drifting(self) -> None:
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(self.diff_schema)
        for name, snapshot in (
            ("converged", self.converged),
            ("hierarchy", mutations.drop_parent_edge(self.converged, API)),
            ("dependency", mutations.add_dependency(self.converged, DOCS, PORTAL)),
            ("binding", mutations.mark_missing(self.converged, API)),
            ("ambiguous", mutations.add_second_parent(self.converged, API, CORE)),
        ):
            with self.subTest(case=name):
                self.assertEqual([], sorted(validator.iter_errors(self._diff(snapshot))))

    # -- T17 -------------------------------------------------------------

    def test_exit_zero_for_a_converged_run(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = reconcile.main([str(PILOT), "--snapshot", str(CONVERGED)])
        self.assertEqual(reconcile.EXIT_CONVERGED, code)

    def test_exit_one_for_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            drifted = Path(raw) / "drifted.json"
            drifted.write_text(
                json.dumps(mutations.drop_parent_edge(self.converged, API)),
                encoding="utf-8",
            )
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = reconcile.main([str(PILOT), "--snapshot", str(drifted)])
        self.assertEqual(reconcile.EXIT_DRIFT, code)

    def test_exit_two_for_a_malformed_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            broken = Path(raw) / "broken.json"
            broken.write_text('{"kind": "GitHubGraphSnapshot"}', encoding="utf-8")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = reconcile.main([str(PILOT), "--snapshot", str(broken)])
        self.assertEqual(reconcile.EXIT_ERROR, code)

    def test_exit_two_without_a_source(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(reconcile.EXIT_ERROR, reconcile.main([str(PILOT)]))

    def test_exit_two_when_live_auth_is_missing(self) -> None:
        environment = {
            key: value
            for key, value in __import__("os").environ.items()
            if key not in ("GITHUB_TOKEN", "GH_TOKEN")
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = reconcile.main([str(PILOT), "--live"])
        self.assertEqual(reconcile.EXIT_ERROR, code)

    def test_exit_two_when_the_snapshot_never_observed_a_bound_issue(self) -> None:
        partial = copy.deepcopy(self.converged)
        partial["issues"] = [
            item
            for item in partial["issues"]
            if item["requested"] != mutations.ref(API)
        ]
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "partial.json"
            path.write_text(json.dumps(partial), encoding="utf-8")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = reconcile.main([str(PILOT), "--snapshot", str(path)])
        self.assertEqual(reconcile.EXIT_ERROR, code)

    # -- T18 -------------------------------------------------------------

    def test_two_observed_parents_are_ambiguous_not_wrong(self) -> None:
        result = self._diff(mutations.add_second_parent(self.converged, API, CORE))
        self.assertIn("BindingAmbiguous", _types(result["binding"]))
        entry = next(
            item for item in result["binding"] if item["type"] == "BindingAmbiguous"
        )
        self.assertEqual(2, len(entry["observedParents"]))
        self.assertEqual([], result["hierarchy"])

    def test_ambiguous_endpoint_does_not_cascade_into_dependencies(self) -> None:
        """An endpoint we refused to judge must not resurface as its neighbours' drift."""

        result = self._diff(mutations.add_second_parent(self.converged, API, CORE))
        self.assertEqual([], result["dependency"])
        self.assertEqual(1, result["summary"]["drift"])

    def test_unresolvable_binding_that_is_still_referenced_does_not_cascade(self) -> None:
        """Suppression has to hold even when the graph keeps pointing at the endpoint."""

        broken = mutations.mark_missing(self.converged, API)
        for observation in broken["issues"]:
            if observation["requested"] in (
                mutations.ref(DOCS),
                mutations.ref(PORTAL),
                mutations.ref(TELEGRAM),
            ):
                observation["blockedBy"].append(mutations.ref(API))
            if observation["requested"] == mutations.ref(ROOT_ISSUE):
                observation["subIssues"].append(mutations.ref(API))

        result = self._diff(broken)
        self.assertEqual(["BindingIssueNotFound"], [
            entry["type"] for entry in result["binding"] if entry["severity"] == "drift"
        ])
        self.assertEqual([], result["hierarchy"])
        self.assertEqual([], result["dependency"])
        self.assertEqual(1, result["summary"]["drift"])

    # -- T19 -------------------------------------------------------------

    def test_repository_case_differences_still_reconcile_green(self) -> None:
        document = copy.deepcopy(self.document)
        document["spec"]["github"]["issue"]["repository"] = "MCTLHQ/.GitHub"
        for item in document["spec"]["workItems"]:
            if "issue" in item:
                item["issue"]["repository"] = item["issue"]["repository"].upper()
        result = self._diff(self.converged, document=document)
        self.assertEqual(0, result["summary"]["drift"])

    # -- T20 -------------------------------------------------------------

    def test_synthetic_fixture_cannot_claim_a_live_capture(self) -> None:
        fixture = copy.deepcopy(self.converged)
        fixture["source"] = {
            "mode": "synthetic-fixture",
            "capturedAt": "2026-09-13T19:00:00Z",
        }
        self.assertNotEqual([], github_graph.snapshot_errors(fixture))

    def test_live_capture_requires_capture_provenance(self) -> None:
        fixture = copy.deepcopy(self.converged)
        fixture["source"] = {"mode": "live-capture"}
        self.assertNotEqual([], github_graph.snapshot_errors(fixture))

    def test_live_capture_timestamp_must_be_a_real_timestamp(self) -> None:
        """`format` is an annotation; evidence needs an assertion."""

        fixture = copy.deepcopy(self.converged)
        fixture["source"] = {
            "mode": "live-capture",
            "capturedAt": "yesterday",
            "apiBase": "https://api.github.com",
        }
        self.assertNotEqual([], github_graph.snapshot_errors(fixture))

    def test_a_timestamp_without_an_offset_is_not_rfc_3339(self) -> None:
        fixture = copy.deepcopy(self.converged)
        fixture["source"] = {
            "mode": "live-capture",
            "capturedAt": "2026-09-16T10:00:00",
            "apiBase": "https://api.github.com",
        }
        self.assertNotEqual([], github_graph.snapshot_errors(fixture))

    def test_duplicate_observation_of_one_issue_is_rejected(self) -> None:
        """Two observations of one request make normalization order-dependent."""

        fixture = copy.deepcopy(self.converged)
        fixture["issues"].append(copy.deepcopy(fixture["issues"][0]))
        self.assertNotEqual([], github_graph.snapshot_errors(fixture))

    def test_found_observation_must_carry_its_relations(self) -> None:
        """An unasked-for relation must not read as an observed absence."""

        fixture = copy.deepcopy(self.converged)
        fixture["issues"][0].pop("subIssues")
        self.assertNotEqual([], github_graph.snapshot_errors(fixture))

    # -- binding namespace and collisions --------------------------------

    def test_root_binding_survives_a_work_item_called_epic(self) -> None:
        document = copy.deepcopy(self.document)
        document["spec"]["workItems"][0]["id"] = "epic"
        for item in document["spec"]["workItems"]:
            item["dependsOn"] = [
                "epic" if target == "human-input-core" else target
                for target in item.get("dependsOn", [])
            ]
        desired = reconcile.desired_graph(document)
        self.assertEqual(mutations.ref(ROOT_ISSUE)["number"], desired.root[1])
        self.assertIn(validate.issue_key(mutations.ref(ROOT_ISSUE)), desired.authored_keys())
        self.assertIn(validate.issue_key(mutations.ref(CORE)), desired.authored_keys())

    def test_two_bindings_resolving_to_one_issue_are_ambiguous(self) -> None:
        """A transfer can make two work items name one live object."""

        moved = mutations.redirect(self.converged, PORTAL, DOCS)
        result = self._diff(moved)
        ambiguous = [
            entry for entry in result["binding"] if entry["type"] == "BindingAmbiguous"
        ]
        self.assertEqual(2, len(ambiguous))
        self.assertEqual(
            {mutations.ref(DOCS)["number"]},
            {entry["resolved"]["number"] for entry in ambiguous},
        )

    def test_external_reference_never_collides_with_an_owned_binding(self) -> None:
        """An external issue claims no ownership, so it cannot invalidate one."""

        moved = mutations.redirect(self.converged, EXTERNAL, PORTAL)
        result = self._diff(moved)
        self.assertEqual(
            [], [e for e in result["binding"] if e["type"] == "BindingAmbiguous"]
        )
        # portal-card's own binding stays comparable.
        self.assertEqual([], result["hierarchy"])

    def test_a_shared_external_reference_is_settled_once(self) -> None:
        """One issue is one endpoint, however many work items point at it."""

        document = copy.deepcopy(self.document)
        for item in document["spec"]["workItems"]:
            if item["id"] == "docs":
                item["externalDependsOn"] = [mutations.ref(EXTERNAL)]

        moved = mutations.redirect(self.converged, EXTERNAL, "mctlhq/mctl-telegram#900")
        result = self._diff(moved, document=document)
        redirected = [
            entry for entry in result["binding"] if entry["type"] == "BindingRedirected"
        ]
        self.assertEqual(1, len(redirected))
        self.assertEqual(mutations.ref(EXTERNAL), redirected[0]["requested"])

    # -- output contract and IO ------------------------------------------

    def test_diff_schema_rejects_a_mislabelled_entry(self) -> None:
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(self.diff_schema)
        result = self._diff(self.converged)

        promoted = copy.deepcopy(result)
        promoted["binding"][0]["severity"] = "drift"
        self.assertNotEqual([], sorted(validator.iter_errors(promoted)))

        stripped = copy.deepcopy(result)
        stripped["binding"] = [
            {
                "type": "BindingRedirected",
                "severity": "drift",
                "owner": "portal-card",
                "requested": mutations.ref(PORTAL),
            }
        ]
        self.assertNotEqual([], sorted(validator.iter_errors(stripped)))

    def test_capture_writes_the_snapshot_that_was_read(self) -> None:
        source = github_graph.FixtureGraphSource(self.converged)
        with tempfile.TemporaryDirectory() as raw:
            captured = Path(raw) / "capture.json"
            with mock.patch.object(reconcile, "_build_source", return_value=source):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    code = reconcile.main(
                        [str(PILOT), "--capture", str(captured)]
                    )
            self.assertEqual(reconcile.EXIT_CONVERGED, code)
            self.assertTrue(captured.exists())
            written = json.loads(captured.read_text(encoding="utf-8"))
        self.assertEqual([], github_graph.snapshot_errors(written))
        self.assertEqual(self.converged["issues"], written["issues"])

    def test_several_manifests_emit_a_schema_valid_envelope(self) -> None:
        from jsonschema import Draft202012Validator

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "human-input.yaml").write_text(
                PILOT.read_text(encoding="utf-8"), encoding="utf-8"
            )
            other = self._unrelated_manifest()
            (directory / "other.yaml").write_text(
                yaml.safe_dump(other, sort_keys=False), encoding="utf-8"
            )
            snapshot = copy.deepcopy(self.converged)
            for number, repository in ((99, "mctlhq/.github"), (7, "mctlhq/mctl-web")):
                ref = {"repository": repository, "number": number}
                snapshot["issues"].append(
                    {
                        "requested": ref,
                        "resolved": ref,
                        "found": True,
                        "parent": None,
                        "subIssues": [],
                        "blockedBy": [],
                    }
                )
            snapshot_path = directory / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            output = directory / "out.json"

            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                reconcile.main(
                    [
                        "--corpus",
                        str(directory),
                        "--snapshot",
                        str(snapshot_path),
                        "--output",
                        str(output),
                    ]
                )
            rendered = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual("RoadmapDiffList", rendered["kind"])
        self.assertEqual(2, len(rendered["items"]))
        self.assertEqual(
            [], sorted(Draft202012Validator(self.diff_schema).iter_errors(rendered))
        )
        names = [item["epic"]["name"] for item in rendered["items"]]
        self.assertEqual(["human-input", "other"], names)

    def test_exit_two_when_the_output_path_cannot_be_written(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = reconcile.main(
                [
                    str(PILOT),
                    "--snapshot",
                    str(CONVERGED),
                    "--output",
                    "/nonexistent-directory/diff.json",
                ]
            )
        self.assertEqual(reconcile.EXIT_ERROR, code)

    def test_committed_fixture_is_synthetic_and_valid(self) -> None:
        self.assertEqual([], github_graph.snapshot_errors(self.converged))
        self.assertEqual("synthetic-fixture", self.converged["source"]["mode"])
        self.assertNotIn("capturedAt", self.converged["source"])


if __name__ == "__main__":
    unittest.main()
