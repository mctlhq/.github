"""Account fallback, failure classification and log redaction in agy-review.yml.

The programs under test are the ones the workflow runs: the "Run agy review"
and "Replay interrupted agy stderr" steps are read out of the workflow file
and executed under the same `bash --noprofile --norc -e -o pipefail` GitHub
uses, against a stub `agy` whose answer is chosen per account HOME. A test can
therefore never pass against a copy that has drifted from what ships.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "agy-review.yml"
WORKFLOW_DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
JOB = WORKFLOW_DOC["jobs"]["review"]
STEPS = JOB["steps"]

MARKER = "MARKER-session-parameter"
VERIFY_URL = f"https://accounts.google.com/signin/continue?sarp=1&plt={MARKER}&flowName=X"
INELIGIBLE = (
    "Eligibility check failed: Your current account is not eligible for "
    f"Antigravity. Verify your account to continue. Please verify: {VERIFY_URL}"
)

# What the stub agy prints for each mode: (stdout result.json, stderr).
MODES = {
    "ok": ({"status": "SUCCESS", "response": "Fine.\n<!-- VERDICT: PASS -->"}, ""),
    "quota": ({"status": "ERROR", "error": "Individual quota reached. Resets in 60h."}, ""),
    "ineligible": (
        {"status": "ERROR", "error": INELIGIBLE},
        f"error: Eligibility check failed: Your current account is not eligible for Antigravity.\n{VERIFY_URL}\n",
    ),
    "unavailable": ({"status": "ERROR", "error": "Eligibility check failed: rpc error: UNAVAILABLE (code 503)"}, ""),
    "declined": ({"status": "ERROR", "error": "The model declined to review this diff."}, ""),
    "forged": ({"status": "ERROR", "error": "x\n::error::forged by the diff"}, "::error::forged on stderr\n"),
}

STUB_AGY = """#!/bin/bash
mode=$(cat "$HOME/mode")
echo "$HOME" >> "$AGY_CALLS"
cat "$AGY_STUBS/$mode.err" >&2
cat "$AGY_STUBS/$mode.json"
[ "$mode" = ok ] && exit 0
exit 1
"""


def step(name: str) -> dict:
    matches = [s for s in STEPS if s.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


class Run:
    """One execution of the "Run agy review" step."""

    def __init__(self, primary: str, fallback: str | None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.scratch = root / "scratch"
        self.scratch.mkdir()
        (self.scratch / "prompt.md").write_text("prompt\n")
        stubs = root / "stubs"
        stubs.mkdir()
        for mode, (result, err) in MODES.items():
            (stubs / f"{mode}.json").write_text(json.dumps(result))
            (stubs / f"{mode}.err").write_text(err)
        # The step prepends $HOME/.local/bin to PATH, so the stub and a no-op
        # sleep live there; HOME is a throwaway so no real agy is ever found.
        home = root / "runner-home"
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "agy").write_text(STUB_AGY)
        (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
        for f in bin_dir.iterdir():
            f.chmod(0o755)
        accounts = {}
        for name, mode in (("primary", primary), ("fallback", fallback)):
            path = root / "agy" / name
            if mode is not None:
                path.mkdir(parents=True)
                (path / "mode").write_text(mode)
            accounts[name] = path
        self.calls = root / "calls"
        self.calls.touch()
        self.summary = root / "summary"
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(home),
            "AGY_CALLS": str(self.calls),
            "AGY_STUBS": str(stubs),
            "GITHUB_OUTPUT": str(root / "output"),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "AGY_HOME_PRIMARY": str(accounts["primary"]),
            "AGY_HOME_FALLBACK": str(accounts["fallback"]),
            "AGY_MODEL": "gemini-3.8-flash-medium",
            "AGY_EFFORT": "medium",
            "AGY_REDACT_SED": JOB["env"]["AGY_REDACT_SED"],
        }
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step("Run agy review")["run"]],
            cwd=self.scratch, env=env, capture_output=True, text=True, timeout=60,
        )
        self.rc = proc.returncode
        self.log = proc.stdout + proc.stderr
        self.env = env

    def accounts_called(self) -> list[str]:
        return [Path(p).name for p in self.calls.read_text().split()]

    def no_verdict(self) -> str | None:
        f = self.scratch / "no-verdict"
        return f.read_text().strip() if f.exists() else None

    def close(self) -> None:
        self.tmp.cleanup()


class FallbackAndClassificationTest(unittest.TestCase):
    def run_step(self, primary: str, fallback: str | None = "ok") -> Run:
        r = Run(primary, fallback)
        self.addCleanup(r.close)
        return r

    def test_ineligible_primary_retries_on_the_fallback(self) -> None:
        r = self.run_step("ineligible", "ok")
        self.assertEqual(r.rc, 0, r.log)
        self.assertEqual(r.accounts_called(), ["primary", "fallback"])
        self.assertIn("not eligible for Antigravity", r.summary.read_text())

    def test_both_accounts_ineligible_fails_closed(self) -> None:
        r = self.run_step("ineligible", "ineligible")
        self.assertEqual(r.rc, 1, r.log)
        self.assertEqual(r.accounts_called(), ["primary", "fallback"])
        self.assertEqual(r.no_verdict(), "0")

    def test_quota_primary_retries_on_the_fallback(self) -> None:
        r = self.run_step("quota", "ok")
        self.assertEqual(r.rc, 0, r.log)
        self.assertEqual(r.accounts_called(), ["primary", "fallback"])

    def test_quota_then_declined_is_classified_by_the_last_attempt(self) -> None:
        r = self.run_step("quota", "declined")
        self.assertEqual(r.rc, 1, r.log)
        self.assertEqual(r.no_verdict(), "0", "a fallback that declined must fail closed")

    def test_ineligible_then_quota_is_no_verdict(self) -> None:
        r = self.run_step("ineligible", "quota")
        self.assertEqual(r.rc, 1, r.log)
        self.assertEqual(r.no_verdict(), "1")

    def test_quota_without_a_fallback_account_is_no_verdict(self) -> None:
        r = self.run_step("quota", None)
        self.assertEqual(r.rc, 1, r.log)
        self.assertEqual(r.accounts_called(), ["primary"])
        self.assertEqual(r.no_verdict(), "1")

    def test_the_503_form_stays_on_the_same_account(self) -> None:
        r = self.run_step("unavailable", "ok")
        self.assertEqual(r.rc, 1, r.log)
        self.assertEqual(r.accounts_called(), ["primary"] * 3)
        self.assertEqual(r.no_verdict(), "1")

    def test_a_declined_review_does_not_switch_accounts(self) -> None:
        r = self.run_step("declined", "ok")
        self.assertEqual(r.rc, 1, r.log)
        self.assertEqual(r.accounts_called(), ["primary"])
        self.assertEqual(r.no_verdict(), "0")

    def test_success_uses_one_call(self) -> None:
        r = self.run_step("ok", "ok")
        self.assertEqual(r.rc, 0, r.log)
        self.assertEqual(r.accounts_called(), ["primary"])
        self.assertIn("VERDICT: PASS", (r.scratch / "review.md").read_text())


class RedactionTest(unittest.TestCase):
    def test_the_verification_link_never_reaches_the_log(self) -> None:
        r = Run("ineligible", "ineligible")
        self.addCleanup(r.close)
        self.assertNotIn(MARKER, r.log)
        self.assertIn("https://accounts.google.com/signin/continue<redacted>", r.log)

    def test_agy_output_cannot_forge_a_workflow_command(self) -> None:
        r = Run("forged", None)
        self.addCleanup(r.close)
        self.assertNotIn("::error::forged", r.log)

    def test_stderr_is_replayed_and_removed_after_each_attempt(self) -> None:
        r = Run("ineligible", None)
        self.addCleanup(r.close)
        self.assertIn("error: Eligibility check failed", r.log)
        self.assertFalse((r.scratch / "agy-stderr.log").exists())

    def test_the_filter_covers_fragments_and_other_schemes(self) -> None:
        cases = {
            "see https://host/cb#access_token=T1 now": "see https://host/cb<redacted> now",
            "open vscode://ext/auth?code=T2": "open vscode://ext/auth<redacted>",
            'url="https://h/p?a=T3&b=4"': 'url="https://h/p<redacted>"',
            "plain https://h/p stays": "plain https://h/p stays",
        }
        for line, want in cases.items():
            got = subprocess.run(
                ["sed", "-E", JOB["env"]["AGY_REDACT_SED"]],
                input=line + "\n", capture_output=True, text=True, check=True,
            ).stdout.rstrip("\n")
            self.assertEqual(got, want)

    def test_an_interrupted_attempt_is_replayed_through_the_same_filter(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "agy-stderr.log").write_text(f"killed mid-call {VERIFY_URL}\n::error::forged\n")
            replay = step("Replay interrupted agy stderr")
            self.assertIn("always()", replay["if"])
            proc = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", replay["run"]],
                cwd=d, capture_output=True, text=True, timeout=30,
                env={"PATH": os.environ["PATH"], "AGY_REDACT_SED": JOB["env"]["AGY_REDACT_SED"]},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = proc.stdout + proc.stderr
            self.assertIn("killed mid-call https://accounts.google.com/signin/continue<redacted>", out)
            self.assertNotIn(MARKER, out)
            self.assertNotIn("\n::error::forged", out)
            self.assertFalse((Path(d) / "agy-stderr.log").exists())


if __name__ == "__main__":
    unittest.main()
