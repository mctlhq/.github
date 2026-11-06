"""The Agent trailer check in agent-trailer.yml (mctlhq/.github#316).

A commit without a valid trailer is `unknown`, never human, and a commit list
that could not be read in full is an error, never a clean result.

The steps under test are read out of the workflow file itself and run against a
stand-in `gh` serving canned API responses, so a test can never pass against a
copy that has drifted from what ships.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "agent-trailer.yml"
WORKFLOW_DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
STEPS = WORKFLOW_DOC["jobs"]["check"]["steps"]
CLASSIFY = next(s for s in STEPS if s.get("id") == "classify")
LABEL = next(s for s in STEPS if s["name"] == "Apply the derived label")
ENFORCE = next(s for s in STEPS if s["name"] == "Enforce")

REPO = "mctlhq/foo"
PR = "42"
PULL = f"repos/{REPO}/pulls/{PR}"
COMMITS = f"{PULL}/commits?per_page=100"
LABELS = f"repos/{REPO}/issues/{PR}/labels?per_page=100"
SESSION = "Claude-Session: https://claude.ai/code/session_x"

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
routes = json.load(open(os.environ["GH_ROUTES"], encoding="utf-8"))
route = next((a for a in args if a.startswith("repos/")), None)
method = args[args.index("-X") + 1] if "-X" in args else "GET"
if method != "GET" or args[0] == "label":
    if routes.get("__write_exit__", 0):
        sys.stderr.write("::warning::echoed-text gh: Validation Failed (HTTP 422)\\n")
    sys.exit(routes.get("__write_exit__", 0))
if route not in routes:
    sys.exit(4)
entry = routes[route]
sys.stdout.write(entry["raw"] if "raw" in entry else json.dumps(entry["json"]))
sys.exit(entry.get("exit", 0))
"""


def commit(index, message, parents=1, author=None):
    return {
        "sha": f"{index:040x}",
        "commit": {"message": message},
        "parents": [{"sha": "f" * 40}] * parents,
        "author": author,
    }


def human(index, message, parents=1):
    return commit(index, message, parents, {"login": "mashkovd", "type": "User"})


def trailer(value):
    return f"fix: a change\n\nWhy it matters.\n\nAgent: {value}\n{SESSION}\n"


class Harness(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "bin").mkdir()
        fake = self.dir / "bin" / "gh"
        fake.write_text(FAKE_GH, encoding="utf-8")
        fake.chmod(0o755)
        self.routes_file = self.dir / "routes.json"
        self.log = self.dir / "gh.log"
        self.log.touch()
        self.output = self.dir / "output"
        self.summary = self.dir / "summary"

    def run_step(self, step, routes, **extra):
        self.routes_file.write_text(json.dumps(routes), encoding="utf-8")
        self.output.write_text("", encoding="utf-8")
        self.summary.write_text("", encoding="utf-8")
        env = {
            "PATH": f"{self.dir / 'bin'}:{os.path.dirname(sys.executable)}:{os.environ['PATH']}",
            "REPO": REPO, "PR": PR, "GH_LOG": str(self.log), "GH_ROUTES": str(self.routes_file),
            "GITHUB_OUTPUT": str(self.output), "GITHUB_STEP_SUMMARY": str(self.summary), **extra,
        }
        return subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)

    def outputs(self):
        lines = self.output.read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines)


class ClassifyTest(Harness):
    def classify(self, *commits):
        return self.run_step(CLASSIFY, {
            PULL: {"json": {"commits": len(commits)}},
            COMMITS: {"json": [list(commits)]},
        })

    def assert_result(self, result, label, unknown):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.outputs(), {"label": label, "unknown": str(unknown)})

    def test_each_allowed_value_is_attributed(self):
        for value in ("claude", "codex", "agy", "none"):
            with self.subTest(value=value):
                result = self.classify(human(1, trailer(value)))
                self.assert_result(result, f"agent:{value}", 0)
                self.assertNotIn("::warning::", result.stdout)

    def test_missing_trailer_is_unknown_and_warns_without_failing(self):
        result = self.classify(human(1, "fix: a change\n\nWhy it matters.\n"))
        self.assert_result(result, "agent:unknown", 1)
        self.assertIn(f"::warning::Commit {1:040x} has no Agent trailer", result.stdout)

    def test_every_commit_counts_not_just_one(self):
        result = self.classify(human(1, trailer("claude")), human(2, "fix: no trailer\n"))
        self.assert_result(result, "agent:mixed", 1)

    def test_two_agents_are_mixed(self):
        self.assert_result(self.classify(human(1, trailer("claude")), human(2, trailer("codex"))), "agent:mixed", 0)

    def test_unknown_value_is_invalid(self):
        for value in ("gemini", "Claude", "claude, codex", "agy please"):
            with self.subTest(value=value):
                result = self.classify(human(1, trailer(value)))
                self.assert_result(result, "agent:unknown", 1)
                self.assertIn("malformed Agent trailer", result.stdout)

    def test_conflicting_trailers_are_invalid(self):
        message = "fix: a change\n\nAgent: claude\nAgent: codex\n"
        self.assert_result(self.classify(human(1, message)), "agent:unknown", 1)

    def test_mention_outside_the_trailer_block_does_not_count(self):
        cases = {
            "subject only": "Agent: claude",
            "body paragraph": f"fix: a change\n\nAgent: claude\n\n{SESSION}\n",
            "inside prose": "fix: a change\n\nThe reviewer said\nAgent: claude\nabout it.\n",
        }
        for name, message in cases.items():
            with self.subTest(name):
                self.assert_result(self.classify(human(1, message)), "agent:unknown", 1)

    def test_windows_line_endings_still_parse(self):
        self.assert_result(self.classify(human(1, "fix: a change\r\n\r\nAgent: agy\r\n")), "agent:agy", 0)

    def test_merge_commits_are_exempt(self):
        result = self.classify(human(1, trailer("claude")), human(2, "Merge branch main", parents=2))
        self.assert_result(result, "agent:claude", 0)

    def test_bot_author_needs_no_trailer_and_gets_no_label(self):
        for author in ({"login": "dependabot[bot]", "type": "Bot"}, {"login": "some-app[bot]", "type": "User"}):
            with self.subTest(author=author):
                self.assert_result(self.classify(commit(1, "chore: bump\n", author=author)), "", 0)

    def test_bot_with_a_person_keeps_the_persons_label(self):
        bot = commit(2, "chore: bump\n", author={"login": "dependabot[bot]", "type": "Bot"})
        self.assert_result(self.classify(human(1, trailer("codex")), bot), "agent:codex", 0)

    def test_unmapped_author_is_not_a_bot(self):
        self.assert_result(self.classify(commit(1, "fix: a change\n", author=None)), "agent:unknown", 1)

    def test_messages_never_reach_logs_or_summary(self):
        canary = "::error::injected"
        result = self.classify(human(1, f"fix: {canary}\n\nAgent: {canary}\n"))
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(canary, result.stdout)
        self.assertNotIn(canary, self.summary.read_text(encoding="utf-8"))


class FailedReadTest(Harness):
    GOOD = [[human(1, trailer("claude"))]]

    def assert_failed(self, routes):
        result = self.run_step(CLASSIFY, routes)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("::error::", result.stdout)
        self.assertEqual(self.outputs(), {}, "a failed read must not produce a label or a count")

    def test_control_the_good_routes_pass(self):
        result = self.run_step(CLASSIFY, {PULL: {"json": {"commits": 1}}, COMMITS: {"json": self.GOOD}})
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_pull_read_failure(self):
        self.assert_failed({PULL: {"json": {}, "exit": 1}, COMMITS: {"json": self.GOOD}})

    def test_commit_read_failure_after_a_partial_body(self):
        self.assert_failed({PULL: {"json": {"commits": 1}}, COMMITS: {"json": self.GOOD, "exit": 1}})

    def test_malformed_body(self):
        self.assert_failed({PULL: {"json": {"commits": 1}}, COMMITS: {"raw": "<html>"}})

    def test_malformed_body_is_not_an_empty_pr(self):
        self.assert_failed({PULL: {"json": {"commits": 0}}, COMMITS: {"raw": "<html>"}})

    def test_missing_commit_count(self):
        self.assert_failed({PULL: {"json": {}}, COMMITS: {"json": self.GOOD}})

    def test_empty_list_when_commits_exist_is_not_clean(self):
        self.assert_failed({PULL: {"json": {"commits": 1}}, COMMITS: {"json": [[]]}})

    def test_truncated_list_is_not_clean(self):
        self.assert_failed({PULL: {"json": {"commits": 300}}, COMMITS: {"json": self.GOOD}})

    def test_entry_without_a_message(self):
        entry = {"sha": "a" * 40, "commit": {}, "parents": []}
        self.assert_failed({PULL: {"json": {"commits": 1}}, COMMITS: {"json": [[entry]]}})

    def test_entry_with_a_malformed_sha(self):
        entry = human(1, trailer("claude"))
        entry["sha"] = "not-a-sha"
        self.assert_failed({PULL: {"json": {"commits": 1}}, COMMITS: {"json": [[entry]]}})

    def test_no_pull_request_number(self):
        routes = {PULL: {"json": {"commits": 1}}, COMMITS: {"json": self.GOOD}}
        result = self.run_step(CLASSIFY, routes, PR="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no pull request number", result.stdout)
        self.assertEqual(self.outputs(), {})


class LabelTest(Harness):
    def apply(self, label, current, **routes):
        result = self.run_step(LABEL, {LABELS: {"json": [[{"name": n} for n in current]]}, **routes}, LABEL=label)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return result, [c for c in calls if c[:3] != ["api", "--paginate", "--slurp"]]

    def test_adds_a_new_label(self):
        result, writes = self.apply("agent:claude", ["bug"])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(writes[0][:2], ["label", "create"])
        self.assertIn("labels[]=agent:claude", writes[1])

    def test_replaces_a_stale_label_and_keeps_others(self):
        _, writes = self.apply("agent:codex", ["bug", "agent:claude"])
        self.assertEqual(writes[0][:3], ["api", "-X", "DELETE"])
        self.assertTrue(writes[0][3].endswith("/labels/agent%3Aclaude"))
        self.assertFalse(any("bug" in " ".join(w) for w in writes))

    def test_stale_label_with_reserved_characters_is_encoded(self):
        result, writes = self.apply("", ["agent: a/b#c?d"])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(writes[0][3].endswith("/labels/agent%3A%20a%2Fb%23c%3Fd"), writes[0][3])

    def test_no_write_when_already_correct(self):
        result, writes = self.apply("agent:claude", ["agent:claude"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(writes, [])

    def test_bot_only_removes_stale_and_adds_nothing(self):
        _, writes = self.apply("", ["agent:claude"])
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][2], "DELETE")

    def test_label_outside_the_allowlist_is_refused(self):
        result, writes = self.apply("agent:root", [])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(writes, [])

    def test_unreadable_labels_change_nothing(self):
        result = self.run_step(LABEL, {LABELS: {"raw": "<html>"}}, LABEL="agent:claude")
        self.assertNotEqual(result.returncode, 0)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 1)

    def test_failed_write_fails_the_step(self):
        result, _ = self.apply("agent:claude", [], __write_exit__=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTP status: 422", result.stdout)
        self.assertNotIn("echoed-text", result.stdout)


class EnforceTest(Harness):
    def test_enforce_fails_and_reports_the_count(self):
        result = self.run_step(ENFORCE, {}, UNKNOWN="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("2 commit(s)", result.stdout)

    def test_enforce_is_skipped_when_nothing_is_unknown(self):
        self.assertIn("steps.classify.outputs.unknown != '0'", ENFORCE["if"])


class WorkflowShapeTest(unittest.TestCase):
    def test_warns_by_default(self):
        inputs = WORKFLOW_DOC[True]["workflow_call"]["inputs"]  # YAML reads `on` as True.
        self.assertIs(inputs["enforce"]["default"], False)
        self.assertIn("inputs.enforce", ENFORCE["if"])

    def test_a_newer_push_cancels_the_older_run(self):
        concurrency = WORKFLOW_DOC["jobs"]["check"]["concurrency"]
        self.assertIs(concurrency["cancel-in-progress"], True)
        self.assertIn("github.event.pull_request.number", concurrency["group"])

    def test_labels_are_skipped_for_forks(self):
        self.assertIn("head.repo.full_name == github.repository", LABEL["if"])


if __name__ == "__main__":
    unittest.main()
