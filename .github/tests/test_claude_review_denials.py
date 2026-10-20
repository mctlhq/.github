"""Reviewer permission-denial logging in claude-review.yml (mctlhq/.github#204).

The script under test is read out of the workflow file itself, so a test can
never pass against a copy that has drifted from what ships.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "claude-review.yml"
WORKFLOW_DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
REVIEW_JOB = WORKFLOW_DOC["jobs"]["review"]
SCRIPT = REVIEW_JOB["env"]["DENIALS_LOG_SH"]
STEPS = REVIEW_JOB["steps"]

# Stands for review or PR text: it must never reach the public job log.
SECRET = "SENTINEL-review-body-must-never-reach-the-log"


def denial(command: str | None = None, tool: str = "Bash") -> dict:
    tool_input = {} if command is None else {"command": command}
    return {"tool_name": tool, "tool_use_id": "tu-1", "tool_input": tool_input}


def result(denials: list | None) -> list:
    entry: dict = {"type": "result", "subtype": "success", "is_error": False}
    if denials is not None:
        entry["permission_denials"] = denials
    return [{"type": "system", "subtype": "init"}, entry]


@unittest.skipUnless(shutil.which("jq"), "jq is required")
class DenialsLogTest(unittest.TestCase):
    def run_script(self, content: object | None, raw: str | None = None) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            if raw is not None:
                Path(tmp, "claude-execution-output.json").write_text(raw)
            elif content is not None:
                Path(tmp, "claude-execution-output.json").write_text(json.dumps(content))
            env = {**os.environ, "RUNNER_TEMP": tmp, "DENIALS_ATTEMPT": "0"}
            proc = subprocess.run(["bash", "-c", SCRIPT], env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout

    def test_reports_each_denied_command_head(self) -> None:
        out = self.run_script(result([
            denial("gh pr review 12 --approve --body " + SECRET),
            denial("gh api -X POST repos/o/r/pulls/12/reviews"),
        ]))
        self.assertIn("2 permission denial(s)", out)
        self.assertIn("::warning::Reviewer attempt 0 was denied Bash: gh pr review 12 --approve --body ...", out)
        self.assertIn("denied Bash: gh api -X POST repos/o/r/pulls/12/reviews", out)

    def test_never_prints_review_text(self) -> None:
        commands = [
            "gh pr review 12 --approve --body " + SECRET,
            "gh pr review 12 --approve -b " + SECRET,
            f'gh api repos/o/r/pulls/12/reviews -f event=APPROVE -f body="{SECRET}"',
            f"gh api repos/o/r/pulls/12/reviews -f body={SECRET}",
            f"gh api repos/o/r/pulls/12/reviews --raw-field=body={SECRET}",
            f"gh api repos/o/r/pulls/12/reviews --input - <<'EOF'\n{{\"body\": \"{SECRET}\"}}\nEOF",
            f'echo "{SECRET}" | gh pr review 12 --approve --body-file -',
            f"gh pr comment 12 --body {SECRET} {SECRET} {SECRET}",
            " ".join(["gh"] + ["x"] * 20 + [SECRET]),
        ]
        out = self.run_script(result([denial(c) for c in commands]))
        self.assertIn(f"{len(commands)} permission denial(s)", out)
        self.assertNotIn(SECRET, out)

    def test_non_bash_tool_prints_only_its_name(self) -> None:
        out = self.run_script(result([{"tool_name": "Write", "tool_input": {"content": SECRET}}]))
        self.assertIn("was denied Write\n", out)
        self.assertNotIn(SECRET, out)

    def test_no_denials(self) -> None:
        self.assertIn("0 permission denial(s)", self.run_script(result([])))
        self.assertIn("0 permission denial(s)", self.run_script(result(None)))
        self.assertNotIn("::warning::", self.run_script(result([])))

    def test_unreadable_output_is_unknown_not_zero(self) -> None:
        for raw in ("not json", '{"type": "result"}'):
            out = self.run_script(None, raw=raw)
            self.assertIn("permission denials are unknown", out)
            self.assertNotIn("0 permission denial(s)", out)

    def test_missing_output_is_not_an_error(self) -> None:
        self.assertIn("wrote no execution output", self.run_script(None))

    def test_steps_follow_each_reviewer_attempt(self) -> None:
        names = [s.get("name") for s in STEPS]
        for step_name, review_id, attempt in (
            ("Log primary reviewer permission denials", "review", 0),
            ("Log fallback reviewer permission denials", "review2", 1),
        ):
            s = STEPS[names.index(step_name)]
            self.assertIn(f"steps.{review_id}.outcome", s["if"])
            self.assertTrue(s.get("continue-on-error"))
            self.assertEqual(s["env"]["DENIALS_ATTEMPT"], attempt)
        # The fallback rewrites the execution file, so the primary's denials
        # must be read before it runs.
        self.assertLess(names.index("Log primary reviewer permission denials"),
                        names.index("Reset git state before fallback"))


if __name__ == "__main__":
    unittest.main()
