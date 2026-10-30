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

import mutations  # noqa: E402
import validate  # noqa: E402

SPIKE_ISSUE = {"repository": "mctlhq/mctl-agents", "number": 900}


def _spike() -> dict:
    return {
        "apiVersion": "roadmap.mctl.ai/v1alpha1",
        "kind": "SpikeDefinition",
        "metadata": {"name": "example-spike", "owner": "mctlhq/mctl-agents"},
        "spec": {
            "title": "Example",
            "question": "Is it worth it?",
            "lifecycle": "proposed",
            "github": {"issue": dict(SPIKE_ISSUE)},
            "outcomes": ["YES", "NO"],
        },
    }


def _decided() -> dict:
    document = mutations.set_lifecycle(_spike(), "decided")
    document["spec"]["decision"] = {
        "verdict": "YES",
        "decidedAt": "2026-10-17",
        "decidedBy": "mashkovd",
    }
    return document


def _epic(item_issue=None, root=None) -> dict:
    item = {"id": "the-spike", "phase": "build", "required": True}
    if item_issue is not None:
        item["issue"] = item_issue
    else:
        item.update(title="t", owner="o")
    return {
        "apiVersion": "roadmap.mctl.ai/v1alpha1",
        "kind": "EpicDefinition",
        "metadata": {"name": "example-epic", "owner": "mctl-agents"},
        "spec": {
            "title": "Example",
            "goal": "g",
            "lifecycle": "active",
            "github": {
                "issue": root or {"repository": "mctlhq/.github", "number": 1}
            },
            "phases": [{"id": "build", "title": "Build"}],
            "workItems": [item],
            "completion": {"mode": "allRequired"},
            "successCriteria": ["done"],
        },
    }


class SpikeValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = {}
        for kind, path in (
            ("EpicDefinition", validate.DEFAULT_SCHEMA),
            ("SpikeDefinition", validate.SPIKE_SCHEMA),
        ):
            cls.schemas[kind] = json.loads(path.read_text(encoding="utf-8"))

    def errors(self, document: dict) -> list[str]:
        return validate.validate_manifest(document, self.schemas)

    def assertOneError(self, document: dict, prefix: str) -> None:
        errors = self.errors(document)
        self.assertTrue(errors, "expected a validation error")
        self.assertTrue(
            any(e.startswith(prefix) for e in errors), f"{prefix!r} not in {errors}"
        )

    def test_valid_documents(self) -> None:
        self.assertEqual(self.errors(_spike()), [])
        self.assertEqual(self.errors(_decided()), [])
        active = mutations.set_lifecycle(_spike(), "active")
        active["spec"]["timebox"] = {"decisionBy": "2026-12-01"}
        self.assertEqual(self.errors(active), [])

    def test_verdict_must_be_an_outcome(self) -> None:
        errors = self.errors(mutations.set_verdict(_decided(), "MAYBE"))
        self.assertEqual(
            errors,
            [
                "$.spec.decision.verdict: 'MAYBE' is not one of "
                "spec.outcomes [YES, NO]"
            ],
        )

    def test_decided_requires_decision(self) -> None:
        self.assertOneError(
            mutations.drop_path(_decided(), "spec", "decision"), "$.spec:"
        )

    def test_decision_requires_decided_lifecycle(self) -> None:
        for lifecycle in ("active", "archived"):
            document = mutations.set_lifecycle(_decided(), lifecycle)
            document["spec"]["timebox"] = {"decisionBy": "2026-12-01"}
            self.assertOneError(document, "$.spec:")

    def test_parked_requires_trigger(self) -> None:
        parked = mutations.set_lifecycle(_spike(), "parked")
        self.assertOneError(parked, "$.spec:")
        parked["spec"]["activation"] = {"reviewBy": "2026-12-01"}
        self.assertOneError(parked, "$.spec.activation:")
        parked["spec"]["activation"] = {"trigger": "next article"}
        self.assertEqual(self.errors(parked), [])

    def test_active_requires_decision_by(self) -> None:
        active = mutations.set_lifecycle(_spike(), "active")
        self.assertOneError(active, "$.spec:")
        active["spec"]["timebox"] = {}
        self.assertOneError(active, "$.spec.timebox:")

    def test_outcomes_shape(self) -> None:
        for outcomes in (["A", "A"], ["ONLY"], ["yes", "NO"]):
            document = _spike()
            document["spec"]["outcomes"] = outcomes
            self.assertOneError(document, "$.spec.outcomes")

    def test_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.yaml"
            document = mutations.set_lifecycle(_spike(), "active")
            text = yaml.safe_dump(document).replace("proposed", "active")
            path.write_text(text + "  timebox:\n    decisionBy: 2026-12-01\n")
            loaded = validate._load_manifest(path)
        self.assertEqual(loaded["spec"]["timebox"]["decisionBy"], "2026-12-01")
        self.assertEqual(self.errors(loaded), [])
        loaded["spec"]["timebox"]["decisionBy"] = "2026-02-30"
        self.assertEqual(
            self.errors(loaded),
            ["$.spec.timebox.decisionBy: '2026-02-30' is not a calendar date"],
        )
        import datetime

        loaded["spec"]["timebox"]["decisionBy"] = datetime.datetime(2026, 12, 1, 3)
        self.assertOneError(loaded, "$.spec.timebox.decisionBy")

    def test_observed_state_rejected(self) -> None:
        for field in ("state", "status", "prs", "comments"):
            document = _spike()
            document["spec"][field] = "x"
            self.assertOneError(document, "$.spec:")

    def test_evidence_report_relative(self) -> None:
        for report in ("/etc/passwd", "../x.md", "a/../../x.md"):
            document = _spike()
            document["spec"]["evidence"] = {"report": report}
            self.assertOneError(document, "$.spec.evidence.report")
        document = _spike()
        document["spec"]["evidence"] = {"report": "docs/spikes/x.md"}
        self.assertEqual(self.errors(document), [])

    def test_follow_ups(self) -> None:
        document = _decided()
        a = {"repository": "mctlhq/mctl-api", "number": 5}
        b = {"repository": "MctlHQ/MCTL-API", "number": 5}
        document["spec"]["decision"]["followUps"] = [a, b]
        self.assertOneError(document, "$.spec.decision.followUps[1]")
        document["spec"]["decision"]["followUps"] = [
            {"repository": "MCTLHQ/mctl-agents", "number": 900}
        ]
        self.assertOneError(document, "$.spec.decision.followUps[0]")

    def test_kind(self) -> None:
        for kind in ("Nope", None):
            document = _spike()
            if kind is None:
                del document["kind"]
            else:
                document["kind"] = kind
            self.assertOneError(document, "$.kind: unsupported kind")


class SpikeCorpusTest(unittest.TestCase):
    def corpus(self, *documents: dict, invalid=None) -> dict:
        items = [(Path(f"m{i}.yaml"), d) for i, d in enumerate(documents)]
        return {
            str(p): m
            for p, m in validate.corpus_errors(items, invalid or set()).items()
        }

    def bound_epic(self) -> dict:
        return _epic(item_issue=dict(SPIKE_ISSUE))

    def test_epic_binding_resolves(self) -> None:
        spike = mutations.bind_spike_epic(_spike(), "example-epic", "the-spike")
        self.assertEqual(self.corpus(self.bound_epic(), spike), {})

    def test_epic_binding_failures(self) -> None:
        cases = {
            ("nope", "the-spike"): ("$.spec.epic.name: unknown", self.bound_epic()),
            ("example-epic", "missing"): (
                "$.spec.epic.workItem: EpicDefinition example-epic",
                self.bound_epic(),
            ),
            ("example-epic", "the-spike"): (
                "$.spec.epic.workItem: work item 'the-spike' of EpicDefinition "
                "example-epic is not bound",
                _epic(),
            ),
        }
        for (name, item), (prefix, epic) in cases.items():
            spike = mutations.bind_spike_epic(_spike(), name, item)
            messages = self.corpus(epic, spike)["m1.yaml"]
            self.assertTrue(messages[0].startswith(prefix), messages)
        other = _epic(item_issue={"repository": "mctlhq/mctl-api", "number": 7})
        spike = mutations.bind_spike_epic(_spike(), "example-epic", "the-spike")
        messages = self.corpus(other, spike)["m1.yaml"]
        self.assertIn("is bound to mctlhq/mctl-api#7", messages[0])

    def test_duplicate_binding_across_kinds(self) -> None:
        messages = self.corpus(self.bound_epic(), _spike())["m1.yaml"]
        self.assertTrue(messages[0].startswith("$.spec.epic:"), messages)
        spike = mutations.bind_spike_epic(_spike(), "other", "x")
        messages = self.corpus(self.bound_epic(), spike)["m1.yaml"]
        self.assertTrue(any(m.startswith("$.spec.epic:") for m in messages))

    def test_spike_vs_spike(self) -> None:
        a = _spike()
        b = _spike()
        b["spec"]["github"]["issue"]["repository"] = "MCTLHQ/MCTL-AGENTS"
        result = self.corpus(a, b)
        self.assertEqual(set(result), {"m0.yaml", "m1.yaml"})
        self.assertTrue(
            any("bound by more than one SpikeDefinition" in m for m in result["m0.yaml"])
        )
        self.assertTrue(any("metadata.name" in m for m in result["m1.yaml"]))

    def test_spike_issue_is_epic_root(self) -> None:
        epic = _epic(root=dict(SPIKE_ISSUE))
        messages = self.corpus(epic, _spike())["m1.yaml"]
        self.assertIn("is the root issue of EpicDefinition example-epic", messages[0])

    def test_invalid_epic_reported_as_such(self) -> None:
        spike = mutations.bind_spike_epic(_spike(), "example-epic", "the-spike")
        messages = self.corpus(spike, invalid={"example-epic"})["m0.yaml"]
        self.assertIn("failed validation", messages[0])

    def test_epics_only_corpus_unchanged(self) -> None:
        a, b = _epic(), _epic()
        result = self.corpus(a, b)
        self.assertEqual(
            result["m0.yaml"][0],
            "GitHub issue mctlhq/.github#1 is bound across manifests: "
            "m0.yaml (epic) and m1.yaml (epic)",
        )
        epics = [
            (p, validate._load_manifest(p))
            for p in sorted((ROADMAP / "epics").glob("*.yaml"))
        ]
        self.assertEqual(validate.corpus_errors(epics), {})


class SpikeCliTest(unittest.TestCase):
    def run_main(self, *argv: str) -> tuple[int, str]:
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()):
            code = validate.main(list(argv))
        return code, out.getvalue()

    def test_committed_corpus(self) -> None:
        code, out = self.run_main(str(ROADMAP / "epics"), str(ROADMAP / "spikes"))
        self.assertEqual(code, 0, out)
        spikes = sorted((ROADMAP / "spikes").glob("*.yaml"))
        # Not pinned to an exact count: every new manifest would otherwise
        # have to edit this line. The PASS check below covers each file.
        self.assertGreaterEqual(len(spikes), 5)
        for path in spikes:
            self.assertIn(f"PASS {path}", out)
        decided = yaml.safe_load((ROADMAP / "spikes" / "openspec-contract.yaml").read_text())
        self.assertEqual(decided["spec"]["decision"]["verdict"], "ADAPTER_ONLY")

    def test_default_paths(self) -> None:
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("spikes", out)
        self.assertIn("epics", out)

    def test_mutations_fail_then_restore(self) -> None:
        base = yaml.safe_load((ROADMAP / "spikes" / "openspec-contract.yaml").read_text())
        mutants = [
            mutations.set_verdict(base, "MAYBE"),
            mutations.drop_path(base, "spec", "decision"),
            mutations.set_lifecycle(base, "archived"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.yaml"
            for mutant in mutants:
                path.write_text(yaml.safe_dump(mutant))
                code, out = self.run_main(str(path))
                self.assertEqual(code, 1, out)
                self.assertIn(f"FAIL {path}", out)
            path.write_text(yaml.safe_dump(copy.deepcopy(base)))
            self.assertEqual(self.run_main(str(path))[0], 0)

    def test_unreadable_spike_schema(self) -> None:
        code, _ = self.run_main("--spike-schema", "/nonexistent.json")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
