"""pr-steward usage records in steward-usage.yml (mctlhq/.github#50).

The validation program is read out of the workflow file itself and run with
the real jq, so a test can never pass against a copy that has drifted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "steward-usage.yml"
DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
JOB = DOC["jobs"]["publish"]
PROGRAM = JOB["env"]["STEWARD_USAGE_JQ"]

# mctl-api usage.Record JSON fields (ADR-012 ModelUsageRecord) minus the cost
# and id fields the ledger computes itself and every correlation the steward
# does not have.
ALLOWED = {
    "schema_version", "session_id", "result_uuid", "model_key", "canonical_model",
    "provider", "agent", "devloop_stage", "target_repo", "pr_number",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "web_search_requests", "outcome", "api_error_status", "stop_reason",
    "terminal_reason", "num_turns", "duration_api_ms", "retry_attempt", "recorded_at",
}


def record(**overrides):
    base = {
        "schema_version": 1, "session_id": "sess-1", "result_uuid": "res-1",
        "model_key": "claude-sonnet-5-5", "agent": "pr-steward", "devloop_stage": "shepherd",
        "target_repo": "mctlhq/mctl-telegram", "pr_number": 725,
        "input_tokens": 100, "output_tokens": 20, "outcome": "success",
        "retry_attempt": 0, "recorded_at": "2026-10-01T00:00:00Z",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def run(payload) -> list[dict]:
    proc = subprocess.run(["jq", PROGRAM], input=json.dumps(payload), capture_output=True,
                          text=True, check=True, timeout=30)
    return json.loads(proc.stdout)["records"]


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class StewardUsageProgramTest(unittest.TestCase):
    def test_a_valid_record_passes_unchanged(self):
        self.assertEqual(run({"records": [record()]}), [record()])

    def test_agent_and_stage_are_set_here_not_taken_from_the_payload(self):
        [out] = run({"records": [record(agent="investigator", devloop_stage="reviewer")]})
        self.assertEqual((out["agent"], out["devloop_stage"]), ("pr-steward", "shepherd"))

    def test_unknown_and_cost_fields_are_dropped(self):
        [out] = run({"records": [record(calculated_cost=9.9, pricing_version="x", id="forged",
                                        provider_reported_cost=1, work_item_id="wi_x",
                                        temporal_workflow_id="t", note="prose")]})
        self.assertLessEqual(set(out), ALLOWED)

    def test_absent_or_malformed_counters_stay_absent_never_zero(self):
        [out] = run({"records": [record(input_tokens=None, output_tokens=-5,
                                        cache_read_tokens=1.5, cache_write_tokens="7")]})
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            self.assertNotIn(key, out)

    def test_foreign_repo_drops_pr_attribution(self):
        [out] = run({"records": [record(target_repo="evil/repo")]})
        self.assertNotIn("target_repo", out)
        self.assertNotIn("pr_number", out)
        [out] = run({"records": [record(pr_number=0)]})
        self.assertNotIn("pr_number", out)

    def test_records_without_identity_are_dropped_and_count_is_capped(self):
        self.assertEqual(run({"records": [record(session_id=""), record(model_key=None)]}), [])
        self.assertEqual(len(run({"records": [record(result_uuid=str(i)) for i in range(50)]})), 20)

    def test_garbage_payloads_yield_no_records(self):
        for payload in (None, [], "x", {"records": "x"}, {"records": [1, "a", None]}, {}):
            self.assertEqual(run(payload), [], payload)


class WorkflowShapeTest(unittest.TestCase):
    def test_only_the_steward_app_and_no_token_permissions(self):
        self.assertEqual(DOC["permissions"], {})
        self.assertEqual(JOB["if"], "github.event.sender.login == 'mctl-claude-remote[bot]'")
        on = DOC.get("on", DOC.get(True))
        self.assertEqual(on, {"repository_dispatch": {"types": ["pr-steward-usage"]}})

    def test_payload_reaches_the_shell_only_through_env(self):
        steps = {s["name"]: s for s in JOB["steps"]}
        validate = steps["Validate steward usage records"]
        self.assertNotIn("${{", validate["run"])
        self.assertEqual(validate["env"]["PAYLOAD"], "${{ toJSON(github.event.client_payload) }}")
        upload = steps["Upload steward model usage records"]
        self.assertEqual(upload["with"]["name"], "model-usage-records")


if __name__ == "__main__":
    unittest.main()
