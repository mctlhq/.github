"""Final-attempt classification in claude-review.yml (mctlhq/.github#53).

The shell under test is read out of the workflow file, so a test cannot pass
against a copy that has drifted from what ships. `gh` is a stub that records
the comment body instead of posting it.
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
STEPS = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["review"]["steps"]


def step(name: str) -> dict:
    matches = [s for s in STEPS if s.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


def idx(name: str) -> int:
    return [s.get("name") for s in STEPS].index(name)


LIMIT = "You've hit your limit"


def result(*, text: str = "boom", cost: float | None = 0, turns: int = 1) -> list:
    r = {"type": "result", "subtype": "success", "is_error": True, "result": text,
         "num_turns": turns, "duration_ms": 10}
    if cost is not None:
        r["total_cost_usd"] = cost
    # The transcript carries limit-looking prose that must never decide anything.
    return [{"type": "assistant", "message": "resource limits, rate limit, usage limit"}, r]


class Sandbox:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-review-classify-"))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text('#!/bin/bash\nprintf "%s\\n" "$@" >> "$GH_LOG"\n', encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.log = self.root / "gh.log"

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, attempt: str, doc: object) -> None:
        (self.root / f"claude-execution-output-{attempt}.json").write_text(
            doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")

    def classify(self, *, has_fallback: bool, fallback_outcome: str = "skipped") -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(
            PATH=f"{self.bin}:{env['PATH']}", RUNNER_TEMP=str(self.root), GH_LOG=str(self.log),
            REPO="mctlhq/foo", PR_NUMBER="42", RUN_URL="https://example.invalid/run",
            HAS_FALLBACK="true" if has_fallback else "false", FALLBACK_OUTCOME=fallback_outcome,
        )
        return subprocess.run(["bash", "-e", "-c", step("Classify reviewer outcome")["run"]],
                              env=env, capture_output=True, text=True)

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
        body = self.sb.comment()
        self.assertNotIn("hit the shared usage limit", body)
        self.assertIn("billed", body)
        self.assertIn("fallback", body)

    def test_billed_failure_with_limit_text_is_still_not_a_limit(self) -> None:
        self.sb.write("fallback", result(text=LIMIT, cost=0.5))
        self.assertEqual(1, self.sb.classify(has_fallback=True, fallback_outcome="success").returncode)

    def test_genuine_limit_on_final_attempt_still_exits_zero(self) -> None:
        self.sb.write("primary", result(text="boom", cost=0.2))
        self.sb.write("fallback", result(text=LIMIT, cost=0))
        proc = self.sb.classify(has_fallback=True, fallback_outcome="success")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("fallback token", self.sb.comment())

    def test_limited_primary_with_no_fallback_exits_zero_and_names_primary(self) -> None:
        self.sb.write("primary", result(text=LIMIT, cost=0))
        proc = self.sb.classify(has_fallback=False)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("primary token", self.sb.comment())

    def test_primary_limit_text_never_decides_when_fallback_ran(self) -> None:
        self.sb.write("primary", result(text=LIMIT, cost=0))
        self.sb.write("fallback", result(text="some other error", cost=0))
        self.assertEqual(1, self.sb.classify(has_fallback=True, fallback_outcome="success").returncode)

    def test_limit_words_in_transcript_only_are_not_a_limit(self) -> None:
        self.sb.write("primary", result(text="tool crashed", cost=0))
        proc = self.sb.classify(has_fallback=False)
        self.assertEqual(1, proc.returncode)
        self.assertIn("non-quota", self.sb.comment())

    def test_unobserved_final_attempt_is_unknown_never_a_limit(self) -> None:
        # Fallback ran but wrote nothing; the primary's limit must not leak in.
        self.sb.write("primary", result(text=LIMIT, cost=0))
        proc = self.sb.classify(has_fallback=True, fallback_outcome="failure")
        self.assertEqual(1, proc.returncode)
        self.assertIn("could not be read", self.sb.comment())
        for label, doc in {"malformed": "not json{", "no result": [{"type": "assistant"}]}.items():
            with self.subTest(label):
                self.sb.write("fallback", doc)
                self.assertEqual(1, self.sb.classify(has_fallback=True, fallback_outcome="failure").returncode)


class WiringTest(unittest.TestCase):
    def test_each_attempt_is_moved_aside_before_the_next_can_overwrite_it(self) -> None:
        self.assertLess(idx("Claude review"), idx("Preserve primary execution output"))
        self.assertLess(idx("Capture primary reviewer model usage"), idx("Preserve primary execution output"))
        self.assertLess(idx("Log primary reviewer permission denials"), idx("Preserve primary execution output"))
        self.assertLess(idx("Preserve primary execution output"), idx("Claude review (fallback token)"))
        self.assertLess(idx("Log fallback reviewer permission denials"), idx("Preserve fallback execution output"))
        self.assertLess(idx("Preserve fallback execution output"), idx("Check final review outcome"))
        for name in ("Preserve primary execution output", "Preserve fallback execution output"):
            self.assertEqual("always()", step(name)["if"])

    def test_final_check_reads_the_fallback_file_not_the_shared_one(self) -> None:
        run = step("Check final review outcome")["run"]
        self.assertIn("claude-execution-output-fallback.json", run)
        self.assertNotIn("claude-execution-output.json", run)


if __name__ == "__main__":
    unittest.main()
