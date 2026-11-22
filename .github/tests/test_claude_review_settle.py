"""Burst-of-pushes handling in claude-review.yml's gate (mctlhq/.github#71).

The settle step waits before the staleness step reads the PR head, so only the
last run of a burst reaches the paid review job. Both steps are read out of the
workflow file and run against stand-in `gh` and `sleep` commands.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "claude-review.yml"
DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
# PyYAML reads the bare key `on` as True.
INPUTS = DOC.get("on", DOC.get(True))["workflow_call"]["inputs"]
STEPS = DOC["jobs"]["gate"]["steps"]
SETTLE = next(s for s in STEPS if s["name"] == "Let a burst of pushes settle")
STALE = next(s for s in STEPS if s.get("id") == "staleness")

SHA_A = "a" * 40
SHA_B = "b" * 40


class Base(unittest.TestCase):
    def run_step(self, step: dict, env: dict, gh_body: str = "exit 0") -> tuple:
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bin_ = d / "bin"
            bin_.mkdir()
            (bin_ / "gh").write_text(f"#!/bin/bash\n{gh_body}\n")
            (bin_ / "sleep").write_text(f'#!/bin/bash\necho "$@" >> "{d}/sleep.log"\n')
            for f in bin_.iterdir():
                f.chmod(0o755)
            out = d / "out"
            out.write_text("")
            e = {"PATH": f"{bin_}:/usr/bin:/bin", "GITHUB_OUTPUT": str(out), **env}
            r = subprocess.run(["bash", "-e", "-c", step["run"]], env=e,
                               capture_output=True, text=True)
            log = d / "sleep.log"
            return r, out.read_text(), log.read_text().split() if log.exists() else []


class SettleTests(Base):
    def test_waits_only_on_pull_request(self):
        self.assertEqual(SETTLE["if"], "github.event_name == 'pull_request'")

    def test_default_is_positive_and_sleeps(self):
        default = INPUTS["settle-seconds"]["default"]
        self.assertGreater(default, 0)
        r, _, sleeps = self.run_step(SETTLE, {"SETTLE_SECONDS": str(default)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sleeps, [str(default)])

    def test_zero_disables_wait(self):
        r, _, sleeps = self.run_step(SETTLE, {"SETTLE_SECONDS": "0"})
        self.assertEqual((r.returncode, sleeps), (0, []))

    def test_bad_values_fail_instead_of_skipping_the_wait(self):
        for bad in ("", "-1", "abc", "1.5", "301"):
            r, _, sleeps = self.run_step(SETTLE, {"SETTLE_SECONDS": bad})
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertEqual(sleeps, [], bad)

    def test_settle_runs_before_staleness_in_gate(self):
        names = [s.get("name") for s in STEPS]
        self.assertLess(names.index(SETTLE["name"]), names.index(STALE["name"]))

    def test_review_concurrency_still_sha_scoped(self):
        g = DOC["jobs"]["review"]["concurrency"]["group"]
        self.assertIn("needs.gate.outputs.sha", g)


class StalenessTests(Base):
    ENV = {"REPO": "o/r", "PR_NUMBER": "1", "TRIGGERED_FOR": SHA_A, "GH_TOKEN": "x"}

    def test_burst_earlier_run_is_stale_last_run_is_fresh(self):
        r, out, _ = self.run_step(STALE, self.ENV, f"echo {SHA_B}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stale=true", out)
        r, out, _ = self.run_step(STALE, {**self.ENV, "TRIGGERED_FOR": SHA_B}, f"echo {SHA_B}")
        self.assertIn("stale=false", out)

    def test_unreadable_head_is_not_stale_and_not_fresh(self):
        for body in ("exit 1", "echo ''"):
            r, out, _ = self.run_step(STALE, self.ENV, body)
            self.assertNotEqual(r.returncode, 0, body)
            self.assertNotIn("stale=", out, body)


if __name__ == "__main__":
    unittest.main()
