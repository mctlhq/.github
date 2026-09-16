from __future__ import annotations

import copy
import itertools
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml
from jsonschema import Draft202012Validator

ROADMAP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROADMAP / "scripts"))
sys.path.insert(0, str(ROADMAP / "tests"))

import github_graph  # noqa: E402
import health  # noqa: E402
import mutations  # noqa: E402
import reconcile  # noqa: E402

PILOT = ROADMAP / "epics" / "human-input.yaml"
CONVERGED = ROADMAP / "fixtures" / "human-input" / "converged-fixture.json"

API = "mctlhq/mctl-api#261"
CORE = "mctlhq/mctl-agents#333"
DOCS = "mctlhq/mctl-docs#106"
PORTAL = "mctlhq/mctl-portal#124"

S = health.HealthState


def _error(code: str = "X") -> health.Diagnostic:
    return health.Diagnostic(code=code, level=health.LEVEL_ERROR, message=code)


class HealthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.safe_load(PILOT.read_text(encoding="utf-8"))
        cls.converged = json.loads(CONVERGED.read_text(encoding="utf-8"))
        cls.loaded = reconcile.LoadedManifest(
            document=cls.document,
            sha256=__import__("hashlib").sha256(PILOT.read_bytes()).hexdigest(),
        )
        cls.validator = Draft202012Validator(
            json.loads((ROADMAP / "schemas" / "roadmap-health.schema.json").read_text())
        )

    def _assess(self, snapshot: dict) -> dict:
        return health.assess(PILOT, self.loaded, github_graph.FixtureGraphSource(snapshot))

    def _codes(self, result: dict, level: str | None = None) -> list[str]:
        return sorted(
            item["code"]
            for item in result["diagnostics"]
            if level is None or item["level"] == level
        )

    def _without(self, snapshot: dict, issue: str) -> dict:
        """A snapshot that never observed `issue` -- not one that saw it missing."""

        result = copy.deepcopy(snapshot)
        result["issues"] = [
            item for item in result["issues"] if item["requested"] != mutations.ref(issue)
        ]
        return result

    def assertSchemaValid(self, result: dict) -> None:
        self.assertEqual([], [error.message for error in self.validator.iter_errors(result)])

    # -- states --------------------------------------------------------------

    def test_converged_observation_is_healthy(self) -> None:
        result = self._assess(self.converged)
        self.assertEqual("healthy", result["state"])
        self.assertEqual(["BindingUnbound"], self._codes(result, "info"))
        self.assertSchemaValid(result)

    def test_observed_drift_is_drift(self) -> None:
        result = self._assess(mutations.drop_parent_edge(self.converged, API))
        self.assertEqual("drift", result["state"])
        self.assertEqual(["HierarchyMissingParent"], self._codes(result, "drift"))
        self.assertSchemaValid(result)

    def test_informational_entries_alone_are_still_healthy(self) -> None:
        result = self._assess(
            mutations.add_unexpected_child(self.converged, "mctlhq/.github#42", "mctlhq/mctl-web#5")
        )
        self.assertEqual("healthy", result["state"])
        self.assertIn("HierarchyUnexpectedChild", self._codes(result, "info"))

    def test_invalid_manifest_is_invalid_and_carries_no_observation(self) -> None:
        failures = {PILOT: ("spec.workItems[0]: unknown phase 'nope'",)}
        result = health.invalid(PILOT, None, failures)
        self.assertEqual("invalid", result["state"])
        self.assertEqual(["ManifestInvalid"], self._codes(result))
        self.assertNotIn("source", result)
        self.assertEqual("human-input", result["epic"]["name"])
        self.assertSchemaValid(result)

    def test_a_clean_manifest_in_an_invalid_corpus_is_invalid(self) -> None:
        other = ROADMAP / "epics" / "other.yaml"
        result = health.invalid(PILOT, None, {other: ("duplicate binding",)})
        self.assertEqual("invalid", result["state"])
        self.assertEqual(["CorpusInvalid"], self._codes(result))

    def test_unreadable_source_is_observation_failed(self) -> None:
        class _Failing:
            def snapshot(self, keys):
                raise github_graph.ObservationError("GET ...: HTTP 502")

        result = health.assess(PILOT, self.loaded, _Failing())
        self.assertEqual("observation_failed", result["state"])
        self.assertEqual(["ObservationFailed"], self._codes(result))
        self.assertSchemaValid(result)

    def test_schema_invalid_snapshot_is_observation_failed_not_invalid(self) -> None:
        """`invalid` is about authored desired state; unusable evidence is a failed read."""

        broken = copy.deepcopy(self.converged)
        broken["issues"][0].pop("subIssues")

        # A replayed fixture validates itself and fails before assess() sees it --
        # it must still get the same code as the payload itself, not a code that
        # depends on which adapter happened to notice.
        replayed = self._assess(broken)
        self.assertEqual("observation_failed", replayed["state"])
        self.assertEqual(["SnapshotInvalid"], self._codes(replayed, "error"))

        # Any other source hands the snapshot over as-is; assess() must still
        # classify it as unusable evidence, not as an invalid manifest.
        class _ReturnsBroken:
            def snapshot(self, keys):
                return broken

        result = health.assess(PILOT, self.loaded, _ReturnsBroken())
        self.assertEqual("observation_failed", result["state"])
        self.assertEqual(["SnapshotInvalid"], self._codes(result, "error"))
        self.assertSchemaValid(result)

    # -- the invariant -------------------------------------------------------

    def test_unobserved_issue_is_withheld_never_reported_absent(self) -> None:
        result = self._assess(self._without(self.converged, API))
        self.assertEqual("observation_failed", result["state"])
        self.assertEqual(["ObservationMissing"], self._codes(result, "error"))
        self.assertEqual([], self._codes(result, "drift"))
        absence = {"BindingIssueNotFound", "HierarchyMissingParent", "DependencyMissing"}
        self.assertFalse(absence & set(self._codes(result)))
        self.assertSchemaValid(result)

    def test_partial_drift_plus_unobservable_relationship_is_observation_failed(self) -> None:
        """The combined case: drift is visible, but a relationship could not be observed."""

        snapshot = self._without(
            mutations.add_dependency(self.converged, DOCS, PORTAL), API
        )
        result = self._assess(snapshot)

        self.assertEqual("observation_failed", result["state"])
        self.assertIn("ObservationMissing", self._codes(result, "error"))
        # The drift that WAS observed is kept as evidence ...
        self.assertEqual(["DependencyUnexpected"], self._codes(result, "drift"))
        # ... and nothing about the unobserved endpoint is reported as absent.
        api = mutations.ref(API)
        for item in result["diagnostics"]:
            if item["level"] == "drift":
                self.assertNotIn(api, list((item.get("subject") or {}).values()))
        self.assertSchemaValid(result)

    def test_healthy_is_impossible_without_an_observation(self) -> None:
        state, diagnostics = health.evaluate()
        self.assertEqual(S.OBSERVATION_FAILED, state)
        self.assertEqual(["ObservationAbsent"], [item.code for item in diagnostics])

    # -- precedence ----------------------------------------------------------

    def test_precedence_is_total_and_ordered(self) -> None:
        self.assertEqual(
            (S.OBSERVATION_FAILED, S.INVALID, S.DRIFT, S.HEALTHY), health.PRECEDENCE
        )
        drifting = self._assess(mutations.drop_parent_edge(self.converged, API))
        drift_diff = reconcile.reconcile(
            PILOT, self.document,
            github_graph.FixtureGraphSource(mutations.drop_parent_edge(self.converged, API)),
        )
        cases = {
            ("obs", "invalid", "drift"): S.OBSERVATION_FAILED,
            ("obs", "invalid"): S.OBSERVATION_FAILED,
            ("obs", "drift"): S.OBSERVATION_FAILED,
            ("invalid", "drift"): S.INVALID,
            ("drift",): S.DRIFT,
        }
        for combo, expected in cases.items():
            with self.subTest(combo=combo):
                state, _ = health.evaluate(
                    diff=drift_diff if "drift" in combo else None,
                    validation_errors=[_error("ManifestInvalid")] if "invalid" in combo else (),
                    observation_errors=[_error("ObservationFailed")] if "obs" in combo else (),
                )
                self.assertEqual(expected, state)
        self.assertEqual("drift", drifting["state"])

    def test_worst_picks_the_most_severe_state(self) -> None:
        for combo in itertools.permutations(list(S), 2):
            expected = min(combo, key=health.PRECEDENCE.index)
            self.assertEqual(expected, health.worst(combo))

    def test_a_more_severe_state_keeps_the_less_severe_evidence(self) -> None:
        drift_diff = reconcile.reconcile(
            PILOT, self.document,
            github_graph.FixtureGraphSource(mutations.drop_parent_edge(self.converged, API)),
        )
        state, diagnostics = health.evaluate(
            diff=drift_diff, observation_errors=[_error("ObservationFailed")]
        )
        self.assertEqual(S.OBSERVATION_FAILED, state)
        self.assertIn("HierarchyMissingParent", [item.code for item in diagnostics])

    # -- determinism and purity ---------------------------------------------

    def test_output_is_deterministic_and_order_independent(self) -> None:
        snapshot = self._without(mutations.add_dependency(self.converged, DOCS, PORTAL), API)
        shuffled = copy.deepcopy(snapshot)
        shuffled["issues"] = list(reversed(shuffled["issues"]))
        first = json.dumps(self._assess(snapshot), sort_keys=True)
        self.assertEqual(first, json.dumps(self._assess(copy.deepcopy(snapshot)), sort_keys=True))
        self.assertEqual(first, json.dumps(self._assess(shuffled), sort_keys=True))

    def test_evaluation_makes_no_network_calls(self) -> None:
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network")):
            self.assertEqual("healthy", self._assess(self.converged)["state"])
            self.assertEqual(
                "observation_failed",
                self._assess(self._without(self.converged, API))["state"],
            )

    # -- CLI -----------------------------------------------------------------

    def _run(self, argv: list[str]) -> tuple[int, dict | None]:
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "health.json"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = health.main([*argv, "--output", str(out)])
            payload = json.loads(out.read_text()) if out.exists() else None
        return code, payload

    def _snapshot_file(self, directory: Path, snapshot: dict) -> Path:
        path = directory / "snapshot.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        return path

    def test_cli_exit_codes_follow_the_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            cases = {
                "healthy": (self.converged, health.EXIT_HEALTHY),
                "drift": (mutations.drop_parent_edge(self.converged, API), health.EXIT_DRIFT),
                "observation_failed": (
                    self._without(self.converged, API), health.EXIT_OBSERVATION_FAILED,
                ),
            }
            for state, (snapshot, expected) in cases.items():
                with self.subTest(state=state):
                    path = self._snapshot_file(directory, snapshot)
                    code, payload = self._run([str(PILOT), "--snapshot", str(path)])
                    self.assertEqual(expected, code)
                    self.assertEqual(state, payload["state"])
                    self.assertSchemaValid(payload)

    def test_cli_invalid_corpus_exits_three_without_building_a_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "human-input.yaml").write_text(PILOT.read_text(), encoding="utf-8")
            broken = copy.deepcopy(self.document)
            broken["metadata"]["name"] = "human-input"  # duplicate name across the corpus
            broken["spec"]["github"]["issue"]["number"] = 999
            for item in broken["spec"]["workItems"]:
                if "issue" in item:
                    item["issue"]["number"] += 10000
            (directory / "copy.yaml").write_text(yaml.safe_dump(broken), encoding="utf-8")

            with mock.patch.object(reconcile, "_build_source") as built:
                code, payload = self._run(
                    ["--corpus", str(directory), "--snapshot", str(CONVERGED)]
                )
            built.assert_not_called()

        self.assertEqual(health.EXIT_INVALID, code)
        self.assertEqual("RoadmapHealthList", payload["kind"])
        self.assertEqual({"invalid"}, {item["state"] for item in payload["items"]})
        self.assertSchemaValid(payload)

    def test_cli_unloadable_snapshot_is_observation_failed(self) -> None:
        code, payload = self._run([str(PILOT), "--snapshot", "/nonexistent/snapshot.json"])
        self.assertEqual(health.EXIT_OBSERVATION_FAILED, code)
        self.assertEqual("observation_failed", payload["state"])
        self.assertEqual(["ObservationFailed"], self._codes(payload, "error"))
        # The epic stays identifiable on this path too.
        self.assertEqual(
            {"repository": "mctlhq/.github", "number": 42}, payload["epic"]["issue"]
        )
        self.assertSchemaValid(payload)

    def test_cli_malformed_snapshot_is_snapshot_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            for name, content in (("not json", "{not json"), ("not a snapshot", "{}")):
                with self.subTest(case=name):
                    path = directory / "snapshot.json"
                    path.write_text(content, encoding="utf-8")
                    code, payload = self._run([str(PILOT), "--snapshot", str(path)])
                    self.assertEqual(health.EXIT_OBSERVATION_FAILED, code)
                    self.assertEqual(["SnapshotInvalid"], self._codes(payload, "error"))
                    self.assertIn("issue", payload["epic"])
                    self.assertSchemaValid(payload)

    def test_invalid_manifest_with_malformed_metadata_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "broken.yaml"
            for metadata in ('"broken"', "[1, 2]", "null"):
                with self.subTest(metadata=metadata):
                    path.write_text(f"metadata: {metadata}\n", encoding="utf-8")
                    result = health.invalid(path, None, {path: ("schema failure",)})
                    self.assertEqual("invalid", result["state"])
                    self.assertNotIn("name", result["epic"])
                    self.assertSchemaValid(result)

    def test_schema_binds_state_to_diagnostics_not_only_counters(self) -> None:
        healthy = self._assess(self.converged)
        drifting = self._assess(mutations.drop_parent_edge(self.converged, API))
        failed = self._assess(self._without(self.converged, API))
        error = {"code": "ObservationFailed", "level": "error", "message": "x"}

        forged = {
            "healthy carrying an error diagnostic": (healthy, lambda d: d["diagnostics"].append(error)),
            "healthy carrying a drift diagnostic": (
                healthy,
                lambda d: d["diagnostics"].append({"code": "DependencyMissing", "level": "drift", "message": "x"}),
            ),
            "drift carrying an error diagnostic": (drifting, lambda d: d["diagnostics"].append(error)),
            "drift with no drift diagnostic": (
                drifting,
                lambda d: d.__setitem__("diagnostics", [i for i in d["diagnostics"] if i["level"] != "drift"]),
            ),
            "observation_failed with no error diagnostic": (
                failed,
                lambda d: d.__setitem__("diagnostics", [i for i in d["diagnostics"] if i["level"] != "error"]),
            ),
        }
        for name, (base, mutate) in forged.items():
            with self.subTest(case=name):
                document = copy.deepcopy(base)
                mutate(document)
                self.assertNotEqual([], list(self.validator.iter_errors(document)))

    def test_cli_requires_exactly_one_source(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(health.EXIT_USAGE, health.main([str(PILOT)]))


if __name__ == "__main__":
    unittest.main()
