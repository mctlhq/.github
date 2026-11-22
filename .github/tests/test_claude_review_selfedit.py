"""Reviewer self-edit outcome in claude-review.yml (mctlhq/.github#37).

A caller PR that edits its pinned claude-review.yml cannot be reviewed by the
action. That must end NON-FAILING with an explicit machine-readable outcome
(`self_edit=true`) and a PR comment saying it is not a review, never a red
check and never a synthesised approval. The detect and explain steps are read
out of the workflow file and run against a stand-in `gh`.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "claude-review.yml"
DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
JOB = DOC["jobs"]["review"]
STEPS = JOB["steps"]
DETECT = next(s for s in STEPS if s.get("id") == "selfedit")
EXPLAIN = next(s for s in STEPS if s.get("id") == "selfedit-notice")
REVIEW = next(s for s in STEPS if s.get("id") == "review")
SHA = "c" * 40
PATH = ".github/workflows/claude-review.yml"


def run(step: dict, gh_body: str, env: dict) -> tuple:
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        bin_ = d / "bin"
        bin_.mkdir()
        (bin_ / "gh").write_text(f"#!/bin/bash\n{gh_body}\n")
        (bin_ / "gh").chmod(0o755)
        out, summ = d / "out", d / "summary"
        out.write_text("")
        summ.write_text("")
        e = {"PATH": f"{bin_}:/usr/bin:/bin", "GITHUB_OUTPUT": str(out),
             "GITHUB_STEP_SUMMARY": str(summ), "RUNNER_TEMP": str(d),
             "REPO": "o/r", "PR_NUMBER": "1", "HEAD_SHA": SHA, **env}
        r = subprocess.run(["bash", "-e", "-c", step["run"]], env=e,
                           capture_output=True, text=True)
        calls = (d / "calls.log").read_text() if (d / "calls.log").exists() else ""
        return r, out.read_text(), summ.read_text(), calls


# gh stand-in for the explain step: record comment bodies, no existing comments.
GH_EXPLAIN = '''
echo "$@" >> "$RUNNER_TEMP/calls.log"
case "$1 $2" in
  "api --paginate") exit 0 ;;
esac
'''


class ExplainTests(unittest.TestCase):
    def explain(self, reason="differs", gh=GH_EXPLAIN):
        return run(EXPLAIN, gh, {"WF_PATH": PATH, "REASON": reason,
                                 "RETRIGGERABLE": "true", "GH_TOKEN": "x"})

    def test_ends_non_failing_with_explicit_outcome(self):
        for reason in ("differs", "absent"):
            r, out, summ, calls = self.explain(reason)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("self_edit=true", out)
            self.assertIn("self_edit=true", summ)
            self.assertIn("pr comment", calls)
            self.assertNotIn("::error::", r.stdout)

    def test_comment_keeps_marker_and_says_not_a_review(self):
        r, _, _, calls = self.explain()
        self.assertIn(f"<!-- claude-review:self-edit:{SHA} -->", calls)
        self.assertIn("This is not a review", calls)
        self.assertIn("must still be satisfied", calls)
        self.assertNotIn("stays red", calls)

    def test_no_approval_is_synthesised(self):
        r, _, _, calls = self.explain()
        self.assertNotIn("/reviews", calls)
        self.assertNotIn("APPROVE", calls)
        self.assertNotIn("APPROVE", EXPLAIN["run"])

    def test_comment_failure_is_still_loud(self):
        # Only the outcome softened; a failed announcement must not pass quietly.
        gh = 'case "$1 $2" in "pr comment") exit 1;; esac'
        r, out, _, _ = self.explain(gh=gh)
        self.assertNotEqual(r.returncode, 0)

    def test_already_announced_is_not_repeated(self):
        gh = (f'echo "$@" >> "$RUNNER_TEMP/calls.log"\n'
              f'case "$1 $2" in "api --paginate") echo "<!-- claude-review:self-edit:{SHA} -->";; esac')
        r, out, _, calls = self.explain(gh=gh)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("pr comment", calls)
        self.assertIn("self_edit=true", out)


class WiringTests(unittest.TestCase):
    def test_job_exposes_self_edit_output(self):
        self.assertIn("steps.selfedit.outputs.blocked == 'true'",
                      JOB["outputs"]["self_edit"])

    def test_claude_is_not_run_on_a_self_edit(self):
        self.assertIn("steps.selfedit.outputs.blocked != 'true'", REVIEW["if"])

    def test_notice_runs_only_when_blocked(self):
        self.assertEqual(EXPLAIN["if"], "steps.selfedit.outputs.blocked == 'true'")

    def test_no_failure_path_left_in_notice(self):
        self.assertNotIn("exit 1", EXPLAIN["run"].split("self_edit=true")[-1])


class DetectTests(unittest.TestCase):
    ENV = {"GH_TOKEN": "x",
           "WORKFLOW_REF": f"o/r/{PATH}@refs/pull/1/merge"}

    def detect(self, head, base):
        gh = f'''
case "$*" in
  "api repos/o/r -q .default_branch") echo main ;;
  *"ref=refs/pull/1/merge"*) echo {head} ;;
  *"ref=main"*) echo {base} ;;
esac'''
        return run(DETECT, gh, self.ENV)

    def test_edit_blocks(self):
        r, out, _, _ = self.detect("aaa", "bbb")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("blocked=true", out)

    def test_identical_does_not_block(self):
        r, out, _, _ = self.detect("aaa", "aaa")
        self.assertIn("blocked=false", out)


if __name__ == "__main__":
    unittest.main()
