"""Outcome classification, fail-closed checks, and verdict verification in claude-review.yml.

The shell under test is read straight out of the workflow file so tests fail if
the shipped workflow drifts. `gh` is stubbed with deterministic fixtures.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "claude-review.yml"
DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
STEPS = DOC["jobs"]["review"]["steps"]

HEAD = "a" * 40
BASE = "b" * 40
ALT_BASE = "c" * 40


def step(name: str) -> dict:
    matches = [s for s in STEPS if s.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


def idx(name: str) -> int:
    return [s.get("name") for s in STEPS].index(name)


LIMIT = "You've hit your limit"


def result(*, text: str = "boom", cost: float | None = 0, turns: int = 1, is_error: bool = True) -> list:
    r = {"type": "result", "subtype": "success", "is_error": is_error, "result": text,
         "num_turns": turns, "duration_ms": 10}
    if cost is not None:
        r["total_cost_usd"] = cost
    # The transcript carries limit-looking prose that must never decide anything.
    return [{"type": "assistant", "message": "resource limits, rate limit, usage limit"}, r]


def _parse_outputs(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


class Sandbox:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-review-classify-"))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(
            '#!/bin/bash\n'
            'printf "%s\\n" "$@" >> "$GH_LOG"\n'
            'if [ -n "${GH_MOCK_PY:-}" ]; then\n'
            '  exec python3 "$GH_MOCK_PY" "$@"\n'
            'fi\n',
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.log = self.root / "gh.log"
        self.output_file = self.root / "github_output.txt"

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, attempt: str, doc: object) -> None:
        (self.root / f"claude-execution-output-{attempt}.json").write_text(
            doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8"
        )

    def _run_step(self, script: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        self.output_file.write_text("", encoding="utf-8")
        env = dict(os.environ)
        env.update(
            PATH=f"{self.bin}:{env['PATH']}",
            RUNNER_TEMP=str(self.root),
            GH_LOG=str(self.log),
            GITHUB_OUTPUT=str(self.output_file),
            REPO="mctlhq/foo",
            PR_NUMBER="42",
            HEAD_SHA=HEAD,
            BASE_SHA=BASE,
            RUN_URL="https://example.invalid/run",
        )
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)

    def classify(self, *, has_fallback: bool, fallback_outcome: str = "skipped", script: str | None = None) -> subprocess.CompletedProcess:
        return self._run_step(
            script if script is not None else step("Classify reviewer outcome")["run"],
            {"HAS_FALLBACK": "true" if has_fallback else "false", "FALLBACK_OUTCOME": fallback_outcome},
        )

    def run_check(self, name: str, extra_env: dict[str, str]) -> subprocess.CompletedProcess:
        return self._run_step(step(name)["run"], extra_env)

    def run_verify_verdict(self, gh_py: str, *, dedup_skipped: str = "") -> subprocess.CompletedProcess:
        mock = self.root / "gh_mock.py"
        mock.write_text(gh_py, encoding="utf-8")
        return self._run_step(
            step("Verify terminal verdict posted")["run"],
            {"GH_MOCK_PY": str(mock), "DEDUP_SKIPPED": dedup_skipped},
        )

    def run_resolve_outcome(self, extra_env: dict[str, str]) -> subprocess.CompletedProcess:
        defaults = {
            "PRERUN_STALE": "",
            "SELF_EDIT": "",
            "CLASSIFY_SKIP": "",
            "REVIEW_FAILED": "",
            "FAILURE_OUTCOME": "",
            "VERDICT_OK": "",
            "VERDICT_OUTCOME": "",
            "VERDICT_HEAD": "",
            "VERDICT_BASE": "",
        }
        defaults.update(extra_env)
        return self._run_step(step("Resolve review outcome")["run"], defaults)

    def outputs(self) -> dict[str, str]:
        return _parse_outputs(self.output_file)

    def comment(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""


class ClassifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def test_billed_fallback_after_limited_primary_is_not_a_usage_limit(self) -> None:
        # mctl-telegram#598: primary instant/$0 limit, fallback billed and failed.
        self.sb.write("primary", result(text=LIMIT, cost=0))
        self.sb.write("fallback", result(text="", cost=0.1349, turns=2))
        proc = self.sb.classify(has_fallback=True, fallback_outcome="success")
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual("NO_VERDICT_OTHER", self.sb.outputs().get("outcome"))
        body = self.sb.comment()
        self.assertNotIn("hit the shared usage limit", body)
        self.assertIn("billed", body)
        self.assertIn("fallback", body)

    def test_billed_failure_with_limit_text_is_still_not_a_limit(self) -> None:
        self.sb.write("fallback", result(text=LIMIT, cost=0.5))
        proc = self.sb.classify(has_fallback=True, fallback_outcome="success")
        self.assertEqual(1, proc.returncode)
        self.assertEqual("NO_VERDICT_OTHER", self.sb.outputs().get("outcome"))

    def test_genuine_limit_on_final_attempt_exits_nonzero_and_emits_transient(self) -> None:
        # mctlhq/.github#287/#398: shared quota limit must fail the job (not silent green).
        self.sb.write("primary", result(text="boom", cost=0.2))
        self.sb.write("fallback", result(text=LIMIT, cost=0))
        proc = self.sb.classify(has_fallback=True, fallback_outcome="success")
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual("NO_VERDICT_TRANSIENT", self.sb.outputs().get("outcome"))
        self.assertIn("fallback token", self.sb.comment())

    def test_limited_primary_with_no_fallback_exits_nonzero_and_names_primary(self) -> None:
        self.sb.write("primary", result(text=LIMIT, cost=0))
        proc = self.sb.classify(has_fallback=False)
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual("NO_VERDICT_TRANSIENT", self.sb.outputs().get("outcome"))
        self.assertIn("primary token", self.sb.comment())

    def test_primary_limit_text_never_decides_when_fallback_ran(self) -> None:
        self.sb.write("primary", result(text=LIMIT, cost=0))
        self.sb.write("fallback", result(text="some other error", cost=0))
        proc = self.sb.classify(has_fallback=True, fallback_outcome="success")
        self.assertEqual(1, proc.returncode)
        self.assertEqual("NO_VERDICT_OTHER", self.sb.outputs().get("outcome"))

    def test_limit_words_in_transcript_only_are_not_a_limit(self) -> None:
        self.sb.write("primary", result(text="tool crashed", cost=0))
        proc = self.sb.classify(has_fallback=False)
        self.assertEqual(1, proc.returncode)
        self.assertEqual("NO_VERDICT_OTHER", self.sb.outputs().get("outcome"))
        self.assertIn("non-quota", self.sb.comment())

    def test_unobserved_final_attempt_is_unknown_never_a_limit(self) -> None:
        # Fallback ran but wrote nothing; the primary's limit must not leak in.
        self.sb.write("primary", result(text=LIMIT, cost=0))
        proc = self.sb.classify(has_fallback=True, fallback_outcome="failure")
        self.assertEqual(1, proc.returncode)
        self.assertEqual("NO_VERDICT_OTHER", self.sb.outputs().get("outcome"))
        self.assertIn("could not be read", self.sb.comment())
        for label, doc in {"malformed": "not json{", "no result": [{"type": "assistant"}]}.items():
            with self.subTest(label):
                self.sb.write("fallback", doc)
                p = self.sb.classify(has_fallback=True, fallback_outcome="failure")
                self.assertEqual(1, p.returncode)
                self.assertEqual("NO_VERDICT_OTHER", self.sb.outputs().get("outcome"))


class FailClosedOutputCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def test_primary_and_final_checks_fail_closed_on_missing_or_malformed_json(self) -> None:
        for file_name, name, extra in (
            ("claude-execution-output.json", "Check primary review actually succeeded", {"REVIEW_OUTCOME": "success"}),
            ("claude-execution-output-fallback.json", "Check final review outcome", {"PRIMARY_FAILED": "true", "HAS_FALLBACK": "true", "FALLBACK_OUTCOME": "success"}),
        ):
            path = self.sb.root / file_name
            for label, content in (
                ("missing", None),
                ("malformed", "{bad json"),
                ("not-array", json.dumps({"type": "result", "is_error": False})),
                ("no-result", json.dumps([{"type": "assistant"}])),
            ):
                with self.subTest(step=name, case=label):
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(content, encoding="utf-8")
                    proc = self.sb.run_check(name, extra)
                    self.assertEqual(0, proc.returncode, proc.stderr)
                    self.assertEqual("true", self.sb.outputs().get("failed"))

    def test_primary_skipped_is_not_marked_failed(self) -> None:
        proc = self.sb.run_check("Check primary review actually succeeded", {"REVIEW_OUTCOME": "skipped"})
        self.assertEqual(0, proc.returncode)
        self.assertEqual("false", self.sb.outputs().get("failed"))

    def test_valid_non_error_result_passes_both_checks(self) -> None:
        for file_name, name, extra in (
            ("claude-execution-output.json", "Check primary review actually succeeded", {"REVIEW_OUTCOME": "success"}),
            ("claude-execution-output-fallback.json", "Check final review outcome", {"PRIMARY_FAILED": "true", "HAS_FALLBACK": "true", "FALLBACK_OUTCOME": "success"}),
        ):
            (self.sb.root / file_name).write_text(json.dumps(result(text="ok", cost=0.1, is_error=False)), encoding="utf-8")
            proc = self.sb.run_check(name, extra)
            self.assertEqual(0, proc.returncode)
            self.assertEqual("false", self.sb.outputs().get("failed"))


class VerifyVerdictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    @staticmethod
    def _mock_script(*, reviews=None, graphql=None, compares=None, rest_rc=0, graphql_rc=0, compare_rc=0) -> str:
        return f"""
import json, sys
args = sys.argv[1:]
if args[:2] == ['api', '--paginate']:
    if {rest_rc} != 0:
        sys.exit({rest_rc})
    print({repr(json.dumps(reviews if reviews is not None else []))})
elif args[:2] == ['api', 'graphql']:
    if {graphql_rc} != 0:
        sys.exit({graphql_rc})
    print({repr(json.dumps(graphql if graphql is not None else {}))})
elif args[0] == 'api' and '/compare/' in args[1]:
    if {compare_rc} != 0:
        sys.exit({compare_rc})
    compares = {json.dumps(compares if compares is not None else {})}
    for k, v in compares.items():
        if k in args[1]:
            print(v)
            sys.exit(0)
    sys.exit(1)
else:
    sys.exit(2)
"""

    def test_valid_unedited_claude_pass(self) -> None:
        script = self._mock_script(
            reviews=[{
                "node_id": "PRR_1",
                "user": {"login": "claude[bot]", "type": "Bot"},
                "commit_id": HEAD,
                "state": "APPROVED",
                "body": f"LGTM\nReviewed head={HEAD} base={BASE}",
            }],
            graphql={"data": {"nodes": [{"id": "PRR_1", "lastEditedAt": None, "updatedAt": "2026-01-01T00:00:00Z", "submittedAt": "2026-01-01T00:00:00Z"}]}},
        )
        proc = self.sb.run_verify_verdict(script)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(
            {"verdict_ok": "true", "outcome": "PASS", "reviewed_head": HEAD, "reviewed_base": BASE},
            self.sb.outputs(),
        )

    def test_edited_approval_is_rejected(self) -> None:
        for extra in ({"lastEditedAt": "2026-01-02T00:00:00Z"}, {"updatedAt": "2026-01-02T00:00:00Z"}):
            with self.subTest(extra=extra):
                node = {"id": "PRR_1", "lastEditedAt": None, "updatedAt": "2026-01-01T00:00:00Z", "submittedAt": "2026-01-01T00:00:00Z"}
                node.update(extra)
                script = self._mock_script(
                    reviews=[{
                        "node_id": "PRR_1",
                        "user": {"login": "claude[bot]", "type": "Bot"},
                        "commit_id": HEAD,
                        "state": "APPROVED",
                        "body": f"Reviewed head={HEAD} base={BASE}",
                    }],
                    graphql={"data": {"nodes": [node]}},
                )
                proc = self.sb.run_verify_verdict(script)
                self.assertEqual(1, proc.returncode)
                self.assertEqual("NO_VERDICT_OTHER", self.sb.outputs().get("outcome"))

    def test_missing_verdict_or_changes_requested_classified_accurately(self) -> None:
        # Missing terminal verdict fails closed.
        proc = self.sb.run_verify_verdict(self._mock_script(reviews=[]))
        self.assertEqual(1, proc.returncode)
        self.assertEqual("NO_VERDICT_OTHER", self.sb.outputs().get("outcome"))
        # Blocking CHANGES_REQUESTED is reported as BLOCKING_FINDINGS even with an approval.
        proc = self.sb.run_verify_verdict(self._mock_script(reviews=[
            {"node_id": "PRR_1", "user": {"login": "claude[bot]", "type": "Bot"}, "commit_id": HEAD, "state": "APPROVED", "body": f"Reviewed head={HEAD} base={BASE}"},
            {"node_id": "PRR_2", "user": {"login": "claude[bot]", "type": "Bot"}, "commit_id": HEAD, "state": "CHANGES_REQUESTED", "body": "fix this"},
        ]))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("BLOCKING_FINDINGS", self.sb.outputs().get("outcome"))

    def test_dedup_base_equivalence_and_divergence(self) -> None:
        reviews = [{
            "node_id": "PRR_1",
            "user": {"login": "claude[bot]", "type": "Bot"},
            "commit_id": HEAD,
            "state": "APPROVED",
            "body": f"Reviewed head={HEAD} base={ALT_BASE}",
        }]
        graphql = {"data": {"nodes": [{"id": "PRR_1", "lastEditedAt": None, "updatedAt": "2026-01-01T00:00:00Z", "submittedAt": "2026-01-01T00:00:00Z"}]}}
        mb = "d" * 40
        # Non-dedup run cannot accept ALT_BASE != BASE.
        proc = self.sb.run_verify_verdict(self._mock_script(reviews=reviews, graphql=graphql), dedup_skipped="false")
        self.assertEqual(1, proc.returncode)
        # Dedup run with identical merge_base accepts ALT_BASE and reports reviewed_base=ALT_BASE.
        proc = self.sb.run_verify_verdict(
            self._mock_script(reviews=reviews, graphql=graphql, compares={f"{ALT_BASE}...{HEAD}": mb, f"{BASE}...{HEAD}": mb}),
            dedup_skipped="true",
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(ALT_BASE, self.sb.outputs().get("reviewed_base"))
        # Dedup run with diverging merge_base refuses.
        proc = self.sb.run_verify_verdict(
            self._mock_script(reviews=reviews, graphql=graphql, compares={f"{ALT_BASE}...{HEAD}": mb, f"{BASE}...{HEAD}": "e" * 40}),
            dedup_skipped="true",
        )
        self.assertEqual(1, proc.returncode)

    def test_unreadable_graphql_or_rest_fails_closed(self) -> None:
        proc = self.sb.run_verify_verdict(self._mock_script(rest_rc=1))
        self.assertEqual(1, proc.returncode)
        proc = self.sb.run_verify_verdict(self._mock_script(
            reviews=[{"node_id": "PRR_1", "user": {"login": "claude[bot]", "type": "Bot"}, "commit_id": HEAD, "state": "APPROVED", "body": f"Reviewed head={HEAD} base={BASE}"}],
            graphql={"errors": [{"message": "nope"}]},
        ))
        self.assertEqual(1, proc.returncode)


class OutcomesAndMutationsTest(unittest.TestCase):
    def test_workflow_call_outputs_and_deterministic_steps_exist(self) -> None:
        outputs = DOC[True]["workflow_call"]["outputs"]
        for name in ("self_edit", "review_outcome", "reviewed_head", "reviewed_base"):
            self.assertIn(name, outputs)
        job_outputs = DOC["jobs"]["review"]["outputs"]
        self.assertEqual(
            {
                "self_edit": "${{ steps.selfedit.outputs.blocked == 'true' }}",
                "review_outcome": "${{ steps.outcome.outputs.review_outcome }}",
                "reviewed_head": "${{ steps.outcome.outputs.reviewed_head }}",
                "reviewed_base": "${{ steps.outcome.outputs.reviewed_base }}",
            },
            job_outputs,
        )
        for label in ("PASS", "BLOCKING_FINDINGS", "NO_VERDICT_TRANSIENT", "NO_VERDICT_OTHER", "STALE", "SELF_EDIT"):
            s = step(f"Classify outcome: {label}")
            self.assertIn(f"steps.outcome.outputs.review_outcome == '{label}'", s["if"])

    def test_resolve_outcome_priority(self) -> None:
        sb = Sandbox()
        self.addCleanup(sb.close)
        cases = [
            ({"PRERUN_STALE": "true", "SELF_EDIT": "true", "VERDICT_OK": "true", "VERDICT_OUTCOME": "PASS"}, "STALE", "", ""),
            ({"SELF_EDIT": "true", "VERDICT_OK": "true", "VERDICT_OUTCOME": "PASS"}, "SELF_EDIT", "", ""),
            ({"CLASSIFY_SKIP": "true", "VERDICT_OK": "true"}, "NO_VERDICT_OTHER", "", ""),
            ({"REVIEW_FAILED": "true", "FAILURE_OUTCOME": "NO_VERDICT_TRANSIENT", "VERDICT_OK": "true"}, "NO_VERDICT_TRANSIENT", "", ""),
            ({"REVIEW_FAILED": "true"}, "NO_VERDICT_OTHER", "", ""),
            ({"VERDICT_OK": "true", "VERDICT_OUTCOME": "PASS", "VERDICT_HEAD": HEAD, "VERDICT_BASE": BASE}, "PASS", HEAD, BASE),
            ({}, "NO_VERDICT_OTHER", "", ""),
        ]
        for env, expected, exp_head, exp_base in cases:
            with self.subTest(expected=expected):
                proc = sb.run_resolve_outcome(env)
                self.assertEqual(0, proc.returncode)
                out = sb.outputs()
                self.assertEqual(expected, out.get("review_outcome"))
                self.assertEqual(exp_head, out.get("reviewed_head"))
                self.assertEqual(exp_base, out.get("reviewed_base"))

    def test_mutations_of_fail_closed_and_transient_guards_are_killed(self) -> None:
        sb = Sandbox()
        self.addCleanup(sb.close)
        # Mutation 1: revert #287 limit branch back to exit 0 — genuine limit test MUST catch it.
        orig = step("Classify reviewer outcome")["run"]
        old_ret = '  exit 1\nfi\necho "outcome=NO_VERDICT_OTHER"'
        mutated = orig.replace(old_ret, '  exit 0\nfi\necho "outcome=NO_VERDICT_OTHER"')
        self.assertNotEqual(orig, mutated)
        sb.write("primary", result(text=LIMIT, cost=0))
        self.assertEqual(1, sb.classify(has_fallback=False, script=orig).returncode)
        self.assertEqual(0, sb.classify(has_fallback=False, script=mutated).returncode)

        # Mutation 2: fail open when execution output JSON is missing in review-check — test MUST catch it.
        check_orig = step("Check primary review actually succeeded")["run"]
        check_mut = check_orig.replace('echo "failed=true"', 'echo "failed=false"')
        self.assertNotEqual(check_orig, check_mut)
        (sb.root / "claude-execution-output.json").unlink(missing_ok=True)
        sb.run_check("Check primary review actually succeeded", {"REVIEW_OUTCOME": "success"})
        self.assertEqual("true", sb.outputs().get("failed"))
        sb._run_step(check_mut, {"REVIEW_OUTCOME": "success"})
        self.assertEqual("false", sb.outputs().get("failed"))


class WiringTest(unittest.TestCase):
    def test_each_attempt_is_moved_aside_before_the_next_can_overwrite_it(self) -> None:
        self.assertLess(idx("Claude review"), idx("Preserve primary execution output"))
        self.assertLess(idx("Capture primary reviewer model usage"), idx("Preserve primary execution output"))
        self.assertLess(idx("Log primary reviewer permission denials"), idx("Preserve primary execution output"))
        self.assertLess(idx("Preserve primary execution output"), idx("Claude review (fallback token)"))
        self.assertLess(idx("Log fallback reviewer permission denials"), idx("Preserve fallback execution output"))
        self.assertLess(idx("Preserve fallback execution output"), idx("Check final review outcome"))
        self.assertLess(idx("Classify reviewer outcome"), idx("Verify terminal verdict posted"))
        self.assertLess(idx("Verify terminal verdict posted"), idx("Resolve review outcome"))
        for name in ("Preserve primary execution output", "Preserve fallback execution output"):
            self.assertEqual("always()", step(name)["if"])

    def test_final_check_reads_the_fallback_file_not_the_shared_one(self) -> None:
        run = step("Check final review outcome")["run"]
        self.assertIn("claude-execution-output-fallback.json", run)
        self.assertNotIn("claude-execution-output.json", run)


if __name__ == "__main__":
    unittest.main()
