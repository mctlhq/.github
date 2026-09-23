"""Reviewer-stage model usage capture in claude-review.yml (mctlhq/.github#50).

The programs under test are the ones the workflow runs: they are read out of
the workflow file itself, so a test can never pass against a copy that has
drifted from what ships.
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

# The JSON field names of mctl-api's usage.Record (internal/usage/types.go),
# which is ADR-012's ModelUsageRecord. A produced key outside this set would be
# silently dropped by the ledger's decoder, so it is a bug either way.
LEDGER_FIELDS = {
    "schema_version", "id", "session_id", "result_uuid", "model_key",
    "canonical_model", "provider", "temporal_workflow_id", "argo_workflow_name",
    "agent", "devloop_stage", "target_repo", "issue_number", "pr_number",
    "work_item_id", "trace_id", "span_id", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    "web_search_requests", "provider_reported_cost", "calculated_cost",
    "pricing_version", "invoice_reconciled_cost", "outcome", "api_error_status",
    "stop_reason", "terminal_reason", "num_turns", "duration_api_ms",
    "retry_attempt", "recorded_at",
}

SECRET = "SENTINEL-prompt-or-completion-text-must-never-leave-the-file"

WORKFLOW_DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
REVIEW_JOB = WORKFLOW_DOC["jobs"]["review"]
ENV = REVIEW_JOB["env"]
STEPS = REVIEW_JOB["steps"]


def step(name: str) -> dict:
    matches = [s for s in STEPS if s.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


def step_index(name: str) -> int:
    return [s.get("name") for s in STEPS].index(name)


def model_usage(**overrides: object) -> dict:
    usage = {
        "inputTokens": 1200,
        "outputTokens": 340,
        "cacheReadInputTokens": 5000,
        "cacheCreationInputTokens": 800,
        "webSearchRequests": 0,
        "costUSD": 0.4213,
        "contextWindow": 200000,
        "maxOutputTokens": 32000,
    }
    usage.update(overrides)
    return {k: v for k, v in usage.items() if v is not None}


def execution_output(
    *,
    session_id: str | None = "sess-1",
    uuid: str | None = "res-1",
    subtype: str = "success",
    is_error: bool = False,
    models: dict | None = None,
    num_turns: int = 7,
    extra: dict | None = None,
) -> list:
    result = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "duration_ms": 91000,
        "duration_api_ms": 88000,
        "num_turns": num_turns,
        "result": f"Review verdict text {SECRET}",
        "total_cost_usd": 0.5,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "modelUsage": models if models is not None else {"claude-sonnet-5": model_usage()},
        "permission_denials": [],
    }
    if session_id is not None:
        result["session_id"] = session_id
    if uuid is not None:
        result["uuid"] = uuid
    result.update(extra or {})
    return [
        {"type": "system", "subtype": "init", "session_id": session_id, "model": "claude-sonnet-5"},
        {"type": "user", "message": {"content": f"Review this diff {SECRET}"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": SECRET}]}},
        result,
    ]


def run_records_jq(document: object, *, attempt: int = 0, pr: str = "42") -> list:
    proc = subprocess.run(
        [
            "jq", "--arg", "repo", "mctlhq/foo", "--arg", "pr", pr,
            "--argjson", "attempt", str(attempt),
            "--arg", "captured_at", "2026-09-23T12:00:00Z",
            ENV["USAGE_RECORDS_JQ"],
        ],
        input=json.dumps(document), capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


class RunnerSandbox:
    """A throwaway RUNNER_TEMP plus the files GitHub Actions hands a step."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-review-usage-"))
        self.output = self.root / "github_output"
        self.summary = self.root / "step_summary"
        self.output.touch()
        self.summary.touch()

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def execution_file(self) -> Path:
        return self.root / "claude-execution-output.json"

    @property
    def usage_dir(self) -> Path:
        return self.root / "model-usage"

    def write_execution(self, document: object) -> None:
        self.execution_file.write_text(
            document if isinstance(document, str) else json.dumps(document), encoding="utf-8"
        )

    def env(self, **extra: str) -> dict:
        env = dict(os.environ)
        env.update({k: str(v) for k, v in ENV.items() if k.startswith("USAGE_")})
        env.update(
            RUNNER_TEMP=str(self.root),
            GITHUB_OUTPUT=str(self.output),
            GITHUB_STEP_SUMMARY=str(self.summary),
            REPO="mctlhq/foo",
            PR_NUMBER="42",
        )
        env.update(extra)
        return env

    def capture(self, attempt: int) -> subprocess.CompletedProcess:
        # Exactly what the capture steps run: `bash -c "$USAGE_CAPTURE_SH"`.
        return subprocess.run(
            ["bash", "-c", ENV["USAGE_CAPTURE_SH"]],
            env=self.env(USAGE_ATTEMPT=str(attempt)), capture_output=True, text=True,
        )

    def publish(self) -> subprocess.CompletedProcess:
        # Actions runs `run:` blocks with `bash -e {0}`.
        return subprocess.run(
            ["bash", "-e", "-c", step("Publish reviewer model usage")["run"]],
            env=self.env(), capture_output=True, text=True,
        )

    def published(self) -> list:
        path = self.usage_dir / "model-usage-records.json"
        return json.loads(path.read_text(encoding="utf-8"))["records"]

    def outputs(self) -> dict:
        lines = self.output.read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)


class RecordShapeTest(unittest.TestCase):
    def test_one_record_per_model_with_adr_012_fields(self) -> None:
        records = run_records_jq(execution_output(models={
            "claude-sonnet-5": model_usage(canonicalModel="claude-sonnet-5", provider="firstParty"),
            "claude-haiku-4-5": model_usage(inputTokens=10, outputTokens=2),
        }))
        self.assertEqual(["claude-sonnet-5", "claude-haiku-4-5"], [r["model_key"] for r in records])
        first = records[0]
        self.assertEqual(
            {
                "schema_version": 1,
                "session_id": "sess-1",
                "result_uuid": "res-1",
                "model_key": "claude-sonnet-5",
                "canonical_model": "claude-sonnet-5",
                "provider": "firstParty",
                "agent": "claude-review",
                "devloop_stage": "reviewer",
                "target_repo": "mctlhq/foo",
                "pr_number": 42,
                "input_tokens": 1200,
                "output_tokens": 340,
                "cache_read_tokens": 5000,
                "cache_write_tokens": 800,
                "web_search_requests": 0,
                "outcome": "success",
                "num_turns": 7,
                "duration_api_ms": 88000,
                "retry_attempt": 0,
                "recorded_at": "2026-09-23T12:00:00Z",
            },
            first,
        )
        self.assertEqual(10, records[1]["input_tokens"])
        for record in records:
            self.assertLessEqual(set(record), LEDGER_FIELDS)

    def test_absent_counters_stay_absent_and_zero_stays_zero(self) -> None:
        records = run_records_jq(execution_output(models={
            "m": {"inputTokens": 5, "outputTokens": 0, "costUSD": 0.1},
        }))
        (record,) = records
        self.assertEqual(0, record["output_tokens"])
        for absent in ("cache_read_tokens", "cache_write_tokens", "web_search_requests",
                       "reasoning_tokens", "canonical_model", "provider"):
            self.assertNotIn(absent, record)

    def test_malformed_counters_are_not_measured_rather_than_coerced(self) -> None:
        (record,) = run_records_jq(execution_output(models={
            "m": {"inputTokens": -3, "outputTokens": 1.5, "cacheReadInputTokens": "9"},
        }))
        for field in ("input_tokens", "output_tokens", "cache_read_tokens"):
            self.assertNotIn(field, record)

    def test_no_cost_and_no_id_is_ever_produced(self) -> None:
        # costUSD comes from the CLI's own price table: it is neither
        # provider-reported nor an mctl-catalog calculated cost, and without a
        # pricing_version the ledger would reject it (ADR-012 invariant 6).
        (record,) = run_records_jq(execution_output())
        for field in ("provider_reported_cost", "calculated_cost", "pricing_version",
                      "invoice_reconciled_cost", "id"):
            self.assertNotIn(field, record)

    def test_no_prompt_or_completion_text_leaves_the_execution_file(self) -> None:
        records = run_records_jq(execution_output(extra={"stop_reason": "end_turn"}))
        self.assertNotIn(SECRET, json.dumps(records))
        self.assertEqual("end_turn", records[0]["stop_reason"])

    def test_error_result_is_recorded_as_error_with_its_status(self) -> None:
        (record,) = run_records_jq(execution_output(
            subtype="success", is_error=True, extra={"api_error_status": 429},
        ))
        self.assertEqual("error", record["outcome"])
        self.assertEqual("429", record["api_error_status"])
        (record,) = run_records_jq(execution_output(subtype="error_max_turns"))
        self.assertEqual("error", record["outcome"])

    def test_only_the_last_result_entry_counts(self) -> None:
        document = execution_output(uuid="early")[-1:] + execution_output(uuid="final")
        records = run_records_jq(document)
        self.assertEqual(["final"], [r["result_uuid"] for r in records])

    def test_nothing_measurable_yields_no_records(self) -> None:
        for document in (
            [],
            {"not": "an array"},
            execution_output()[:-1],               # SDK died before the result
            execution_output(models={}),
            execution_output(session_id=None),     # the ledger would 400 the batch
        ):
            with self.subTest(document=str(document)[:60]):
                self.assertEqual([], run_records_jq(document))

    def test_missing_pr_number_is_omitted(self) -> None:
        (record,) = run_records_jq(execution_output(), pr="")
        self.assertNotIn("pr_number", record)


class CaptureAndPublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = RunnerSandbox()
        self.addCleanup(self.runner.close)

    def test_primary_only(self) -> None:
        self.runner.write_execution(execution_output())
        self.assertEqual(0, self.runner.capture(0).returncode)
        self.assertEqual(0, self.runner.publish().returncode)
        records = self.runner.published()
        self.assertEqual([0], [r["retry_attempt"] for r in records])
        self.assertEqual("1", self.runner.outputs()["count"])
        summary = self.runner.summary.read_text(encoding="utf-8")
        self.assertIn("| 0 | claude-sonnet-5 | success | 1200 | 340 | 5000 | 800 |", summary)
        self.assertNotIn(SECRET, summary)

    def test_fallback_that_did_not_rewrite_the_file_is_not_counted_twice(self) -> None:
        self.runner.write_execution(execution_output(subtype="success", is_error=True))
        self.runner.capture(0)
        self.runner.capture(1)  # same file, same session: a re-read, not a re-run
        self.runner.publish()
        records = self.runner.published()
        self.assertEqual(1, len(records))
        self.assertEqual(0, records[0]["retry_attempt"], "the first occurrence must win")

    def test_fallback_that_re_ran_the_model_is_a_second_charge(self) -> None:
        self.runner.write_execution(execution_output(session_id="primary", is_error=True))
        self.runner.capture(0)
        self.runner.write_execution(execution_output(session_id="fallback", uuid="res-2"))
        self.runner.capture(1)
        self.runner.publish()
        records = self.runner.published()
        self.assertEqual([("primary", 0), ("fallback", 1)],
                         [(r["session_id"], r["retry_attempt"]) for r in records])

    def test_without_a_result_uuid_num_turns_separates_results(self) -> None:
        self.runner.write_execution(execution_output(uuid=None, num_turns=3))
        self.runner.capture(0)
        self.runner.write_execution(execution_output(uuid=None, num_turns=4))
        self.runner.capture(1)
        self.runner.publish()
        self.assertEqual(2, len(self.runner.published()))

        self.runner.capture(1)  # identical re-read of the num_turns=4 result
        self.runner.publish()
        self.assertEqual(2, len(self.runner.published()))

    def test_capture_never_fails_the_job_and_reports_unmeasured_spend(self) -> None:
        cases = {
            "missing": None,
            "unreadable": "{ not json",
            "no result": json.dumps(execution_output()[:-1]),
        }
        for label, content in cases.items():
            with self.subTest(label):
                if self.runner.execution_file.exists():
                    self.runner.execution_file.unlink()
                if content is not None:
                    self.runner.write_execution(content)
                proc = self.runner.capture(0)
                self.assertEqual(0, proc.returncode, proc.stderr)
                self.assertIn("not measured", proc.stdout)
                self.assertFalse((self.runner.usage_dir / "attempt-0.json.tmp").exists())

    def test_publish_with_no_attempt_reports_zero_and_writes_nothing(self) -> None:
        proc = self.runner.publish()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("0", self.runner.outputs()["count"])
        self.assertFalse((self.runner.usage_dir / "model-usage-records.json").exists())

    def test_published_body_is_the_ingest_request_shape(self) -> None:
        self.runner.write_execution(execution_output())
        self.runner.capture(0)
        self.runner.publish()
        body = json.loads(
            (self.runner.usage_dir / "model-usage-records.json").read_text(encoding="utf-8")
        )
        self.assertEqual(["records"], list(body))


class WorkflowWiringTest(unittest.TestCase):
    """Where the steps sit is the guarantee, so the order is asserted."""

    def test_primary_is_captured_before_the_fallback_can_overwrite_it(self) -> None:
        capture = step_index("Capture primary reviewer model usage")
        self.assertLess(step_index("Claude review"), capture)
        self.assertLess(capture, step_index("Claude review (fallback token)"))

    def test_fallback_is_captured_after_it_ran(self) -> None:
        self.assertLess(step_index("Claude review (fallback token)"),
                        step_index("Capture fallback reviewer model usage"))

    def test_usage_is_published_before_the_fail_closed_classification(self) -> None:
        publish = step_index("Publish reviewer model usage")
        self.assertLess(step_index("Capture fallback reviewer model usage"), publish)
        self.assertLess(publish, step_index("Upload reviewer model usage records"))
        self.assertLess(step_index("Upload reviewer model usage records"),
                        step_index("Classify reviewer outcome"))

    def test_usage_steps_can_never_block_the_review(self) -> None:
        for name in ("Capture primary reviewer model usage",
                     "Capture fallback reviewer model usage",
                     "Publish reviewer model usage",
                     "Upload reviewer model usage records"):
            with self.subTest(name):
                self.assertIs(True, step(name).get("continue-on-error"))
                self.assertIn("always()", step(name)["if"])

    def test_captures_read_their_own_attempt(self) -> None:
        primary = step("Capture primary reviewer model usage")
        fallback = step("Capture fallback reviewer model usage")
        self.assertIn("steps.review.outcome", primary["if"])
        self.assertIn("steps.review2.outcome", fallback["if"])
        self.assertEqual(0, primary["env"]["USAGE_ATTEMPT"])
        self.assertEqual(1, fallback["env"]["USAGE_ATTEMPT"])
        for capture in (primary, fallback):
            self.assertEqual('bash -c "$USAGE_CAPTURE_SH"', capture["run"])
            self.assertIn("cancelled", capture["if"], "a cancelled attempt may still have been paid for")


if __name__ == "__main__":
    unittest.main()
