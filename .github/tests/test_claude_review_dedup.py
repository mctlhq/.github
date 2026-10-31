"""The "already reviewed" dedup in claude-review.yml (mctlhq/.github#283).

When a branch is updated from its base, GitHub moves the latest approval's
commit_id onto the new merge head without any new review run, so commit_id is
no evidence that a head was reviewed. The dedup may skip the review only on a
claude[bot] verdict whose own body records the exact head and base and that
nobody has changed since it was submitted, and must keep a read it could not
complete apart from one that found no such verdict.

The step under test is read out of the workflow file itself and run against a
stand-in `gh` serving canned API responses, so a test can never pass against a
copy that has drifted from what ships.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "claude-review.yml"
WORKFLOW_DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
REVIEW_JOB = WORKFLOW_DOC["jobs"]["review"]
PROMPT = REVIEW_JOB["env"]["REVIEW_PROMPT"]
STEPS = REVIEW_JOB["steps"]
DEDUP = next(s for s in STEPS if s.get("id") == "dedup")

REPO = "mctlhq/foo"
PR = "42"
# mctl-agents#810: review 5650171210 was APPROVED on OLD_HEAD, and after
# `gh pr update-branch` the reviews API reports it on HEAD, the merge head.
HEAD = "92963dd3b92d547baa64a6601aa256c132702d3e"
OLD_HEAD = "7b1c9267f5cf80d738e3b2f3134415740402d764"
BASE = "9535df34e0fc59af032d4d4a7502f5d29f43570b"
OLD_BASE = "0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e"

EDIT_QUERY = ("query($ids: [ID!]!) { nodes(ids: $ids) { ... on PullRequestReview"
              " { id lastEditedAt updatedAt submittedAt } } }")

# Serves `gh api --paginate .../reviews` and `gh api graphql --input -` from a
# spec file and records every call, with the GraphQL request body appended.
# Review pages: "pages" prints each page as its own JSON text, as older gh
# does; "merged" prints one array, as current gh does. With fail_after, the
# first pages are printed and gh then exits non-zero, which is what a page
# failing mid-listing looks like. GraphQL: one node per requested id, in
# reverse order. A node is untouched since submission unless its id is listed
# in "edited" (an edit GitHub stamped: lastEditedAt set, updatedAt moved),
# "updated" (updatedAt moved, no stamp: a REST edit of a pending review, or
# GitHub moving the verdict) or "stamped" (lastEditedAt set, updatedAt not
# moved). "raw" replaces the whole response; "fail" prints a clean-looking
# response and still exits non-zero, as gh does when the response carries
# errors.
FAKE_GH = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
graphql = args == ["api", "graphql", "--input", "-"]
request = json.loads(sys.stdin.read()) if graphql else None
with open(os.environ["FAKE_GH_CALLS"], "a", encoding="utf-8") as calls:
    calls.write(json.dumps(args + [request] if graphql else args) + "\\n")
with open(os.environ["FAKE_GH_SPEC"], encoding="utf-8") as f:
    spec = json.load(f)
if args == ["api", "--paginate", spec["path"]]:
    fail_after = spec["fail_after"]
    pages = spec["pages"] if fail_after is None else spec["pages"][:fail_after]
    if spec["shape"] == "merged":
        sys.stdout.write(json.dumps([r for page in pages for r in page]))
    else:
        for page in pages:
            sys.stdout.write(page if isinstance(page, str) else json.dumps(page))
    if fail_after is not None:
        sys.stderr.write("gh: Server Error (HTTP 502)\\n")
        sys.exit(1)
    sys.exit(0)
if graphql:
    edits = spec["graphql"]
    submitted, later = "2026-10-26T09:56:57Z", "2026-10-27T10:00:00Z"
    nodes = [{"id": i,
              "lastEditedAt": later if i in edits["edited"] + edits["stamped"] else None,
              "updatedAt": later if i in edits["edited"] + edits["updated"] else submitted,
              "submittedAt": submitted}
             for i in request["variables"]["ids"]]
    raw = edits["raw"]
    sys.stdout.write(json.dumps({"data": {"nodes": nodes[::-1]}}) if raw is None else raw)
    if edits["fail"]:
        sys.stderr.write("gh: Something went wrong while executing your query.\\n")
        sys.exit(1)
    sys.exit(0)
if args[:2] == ["pr", "comment"]:
    sys.exit(0)
sys.stderr.write("unexpected gh call: %r\\n" % (args,))
sys.exit(97)
"""


def verdict_commands(head: str = HEAD, base: str = BASE) -> list[tuple[str, str]]:
    """(event, body) for every verdict command in the prompt, filled in the
    way the reviewer fills it in for a run pinned to head and base."""
    rendered = (PROMPT.replace("${{ needs.gate.outputs.sha }}", head)
                .replace("${{ needs.gate.outputs.base_sha }}", base))
    found = re.findall(r'-f event="(APPROVE|REQUEST_CHANGES)" -f body="([^"]*)"', rendered)
    return [(event, body.replace("<reason>", "Docs-only change.")
             .replace("<n>", "2").replace("<summary>", "one P2 in the parser"))
            for event, body in found]


STATE = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}
AUTO = object()
MISSING = object()
_serial = itertools.count(1)


def review(body: str | None, *, state: str = "APPROVED", commit_id: str = HEAD,
           login: str = "claude[bot]", node_id: object = AUTO) -> dict:
    n = next(_serial)
    r = {
        "id": 5650171210 + n,
        "node_id": f"PRR_kwDOTest{n:012d}" if node_id is AUTO else node_id,
        "user": {"login": login, "type": "Bot"},
        "body": body,
        "state": state,
        "commit_id": commit_id,
        "submitted_at": "2026-10-26T09:56:57Z",
    }
    if node_id is MISSING:
        del r["node_id"]
    return r


def evidence(head: str = HEAD, base: str = BASE) -> str:
    return f"No P1/P2 findings (4 P3). Good to merge. Reviewed head={head} base={base}"


class Sandbox:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-review-dedup-"))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        self.output = self.root / "github_output"
        self.calls = self.root / "gh_calls"
        self.spec = self.root / "gh_spec.json"

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def run(self, pages: list, *, shape: str = "pages", fail_after: int | None = None,
            edited: tuple = (), updated: tuple = (), stamped: tuple = (),
            graphql_raw: str | None = None, graphql_fail: bool = False,
            head: str = HEAD, base: str = BASE) -> subprocess.CompletedProcess:
        self.output.write_text("", encoding="utf-8")
        self.calls.write_text("", encoding="utf-8")
        self.spec.write_text(json.dumps({
            "path": f"repos/{REPO}/pulls/{PR}/reviews",
            "pages": pages, "shape": shape, "fail_after": fail_after,
            "graphql": {"edited": list(edited), "updated": list(updated), "stamped": list(stamped),
                        "raw": graphql_raw, "fail": graphql_fail},
        }), encoding="utf-8")
        env = {
            **os.environ,
            "PATH": self.path,
            "GITHUB_OUTPUT": str(self.output),
            "GH_TOKEN": "not-a-real-token",
            "REPO": REPO,
            "PR_NUMBER": PR,
            "HEAD_SHA": head,
            "BASE_SHA": base,
            "FAKE_GH_SPEC": str(self.spec),
            "FAKE_GH_CALLS": str(self.calls),
        }
        # Actions runs `run:` blocks with `bash -e {0}`.
        return subprocess.run(["bash", "-e", "-c", DEDUP["run"]], env=env,
                              capture_output=True, text=True)

    def outcome(self) -> str | None:
        lines = self.output.read_text(encoding="utf-8").splitlines()
        values = [line.split("=", 1)[1] for line in lines if line.startswith("already_reviewed=")]
        assert len(values) <= 1, f"already_reviewed written {len(values)} times"
        return values[0] if values else None

    def _calls(self) -> list[list]:
        return [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]

    def comments(self) -> list[list[str]]:
        return [c for c in self._calls() if c[:2] == ["pr", "comment"]]

    def edit_requests(self) -> list[dict]:
        return [c[-1] for c in self._calls() if c[:2] == ["api", "graphql"]]


class DedupCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.close)

    def decide(self, pages: list, **kwargs: object) -> subprocess.CompletedProcess:
        proc = self.sandbox.run(pages, **kwargs)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        return proc

    def assertDecision(self, expected: str, pages: list, **kwargs: object) -> None:
        """Both pagination shapes must decide the same way."""
        for shape in ("pages", "merged"):
            with self.subTest(shape=shape):
                proc = self.decide(pages, shape=shape, **kwargs)
                self.assertEqual(expected, self.sandbox.outcome(), proc.stdout)
                comments = self.sandbox.comments()
                if expected == "true":
                    self.assertEqual(1, len(comments), "the skip is announced once")
                else:
                    self.assertEqual([], comments, "only a skip is announced")


class EvidenceTest(DedupCase):
    def test_verdict_written_from_the_prompt_is_deduplicated(self) -> None:
        # The common case the dedup exists for: a real review on this head
        # and base, then a reopen or a manual `@claude review`.
        for event, body in verdict_commands():
            with self.subTest(event=event):
                self.assertDecision("true", [[review(body, state=STATE[event])]])
        body = self.sandbox.comments()[0][-1]
        self.assertIn(HEAD, body)
        self.assertIn(BASE, body)

    def test_verdict_moved_onto_a_merge_head_is_not_evidence(self) -> None:
        # mctl-agents#810: written for OLD_HEAD against OLD_BASE, reported by
        # GitHub on HEAD after update-branch.
        for event, body in verdict_commands(OLD_HEAD, OLD_BASE):
            with self.subTest(event=event):
                self.assertDecision("false", [[review(body, state=STATE[event], commit_id=HEAD)]])
        proc = self.decide([[review(evidence(OLD_HEAD, OLD_BASE))]])
        self.assertIn("::notice::GitHub reports 1 claude[bot] verdict(s) on " + HEAD, proc.stdout)

    def test_verdict_from_before_the_evidence_line_is_not_evidence(self) -> None:
        # Bodies as claude[bot] actually posted them before this change.
        for state, body in (
            ("APPROVED", "No P1/P2 findings (4 P3). Good to merge."),
            ("APPROVED", "Trivial pinned-SHA bump of the reusable claude-review.yml workflow reference."),
            ("CHANGES_REQUESTED", "Has P1/P2 findings, changes requested: one P2 in the parser."),
        ):
            with self.subTest(state=state, body=body):
                self.assertDecision("false", [[review(body, state=state)]])

    def test_same_head_against_a_changed_base_is_not_evidence(self) -> None:
        # The PR's own files are unchanged, but what they are compared with
        # is not: a retargeted base, or main moving between the two runs.
        for event, body in verdict_commands(HEAD, OLD_BASE):
            with self.subTest(event=event):
                self.assertDecision("false", [[review(body, state=STATE[event])]])

    def test_evidence_must_name_the_exact_pair(self) -> None:
        for body in (
            f"Good to merge. Reviewed head={HEAD[:7]} base={BASE[:7]}",
            f"Good to merge. Reviewed head={BASE} base={HEAD}",
            f"Good to merge. reviewed head={HEAD} base={BASE}",
            f"Good to merge. Reviewed head={HEAD}",
            f"Good to merge. Reviewed base={BASE} head={HEAD}",
        ):
            with self.subTest(body=body):
                self.assertDecision("false", [[review(body)]])

    def test_evidence_and_githubs_own_record_must_agree(self) -> None:
        # A verdict that says HEAD but that GitHub reports on another commit
        # is not taken on its body alone.
        self.assertDecision("false", [[review(evidence(), commit_id=OLD_HEAD)]])

    def test_only_a_claude_verdict_counts(self) -> None:
        for login in ("mashkovd", "github-actions[bot]", "chatgpt-codex-connector[bot]", "claude"):
            with self.subTest(login=login):
                self.assertDecision("false", [[review(evidence(), login=login)]])
        for state in ("COMMENTED", "DISMISSED", "PENDING"):
            with self.subTest(state=state):
                self.assertDecision("false", [[review(evidence(), state=state)]])

    def test_evidence_may_sit_anywhere_in_the_body(self) -> None:
        body = f"{evidence()} The remaining P3s are edge cases worth a follow-up."
        self.assertDecision("true", [[review(body)]])

    def test_verdict_on_a_later_page_is_found(self) -> None:
        inline = [review("", state="COMMENTED") for _ in range(30)]
        self.assertDecision("true", [inline, [review(evidence())]])

    def test_empty_or_missing_body_is_not_evidence(self) -> None:
        for body in ("", None):
            with self.subTest(body=body):
                self.assertDecision("false", [[review(body)]])

    def test_no_reviews_at_all(self) -> None:
        proc = self.decide([[]])
        self.assertEqual("false", self.sandbox.outcome())
        self.assertNotIn("::notice::", proc.stdout)
        self.assertNotIn("::warning::", proc.stdout)


class EditTest(DedupCase):
    """Write access is enough to edit a claude[bot] review body, so a verdict
    changed after it was submitted proves nothing about what claude[bot]
    reviewed."""

    def test_edited_verdict_is_not_evidence(self) -> None:
        for event, body in verdict_commands():
            with self.subTest(event=event):
                verdict = review(body, state=STATE[event])
                self.assertDecision("false", [[verdict]], edited=(verdict["node_id"],))
        proc = self.decide([[verdict]], edited=(verdict["node_id"],))
        self.assertIn("has been edited or otherwise changed since it was submitted", proc.stdout)

    def test_verdict_changed_without_an_edit_stamp_is_not_evidence(self) -> None:
        # A REST edit of a pending review moved updatedAt and left
        # lastEditedAt unset; GitHub moving a verdict does the same.
        verdict = review(evidence())
        self.assertDecision("false", [[verdict]], updated=(verdict["node_id"],))

    def test_edit_stamp_alone_is_enough_to_disqualify(self) -> None:
        verdict = review(evidence())
        self.assertDecision("false", [[verdict]], stamped=(verdict["node_id"],))

    def test_one_unchanged_verdict_is_enough(self) -> None:
        edited, moved, kept = review(evidence()), review(evidence()), review(evidence())
        changed = {"edited": (edited["node_id"],), "updated": (moved["node_id"],)}
        self.assertDecision("true", [[edited, moved, kept]], **changed)
        self.assertDecision("false", [[edited, moved, kept]], stamped=(kept["node_id"],), **changed)

    def test_edit_history_is_read_for_exactly_the_verdicts_with_evidence(self) -> None:
        verdict = review(evidence())
        pages = [[
            verdict,
            review("No P1/P2 findings (4 P3). Good to merge."),
            review(evidence(OLD_HEAD, OLD_BASE)),
            review(evidence(), state="COMMENTED"),
            review(evidence(), login="mashkovd"),
            review(evidence(), commit_id=OLD_HEAD),
        ]]
        self.assertDecision("true", pages)
        [request] = self.sandbox.edit_requests()
        self.assertEqual({"ids": [verdict["node_id"]]}, request["variables"])
        self.assertEqual(EDIT_QUERY, " ".join(request["query"].split()))

    def test_no_edit_history_is_read_without_evidence(self) -> None:
        self.assertDecision("false", [[review("Good to merge."), review(evidence(OLD_HEAD, OLD_BASE))]])
        self.assertEqual([], self.sandbox.edit_requests())

    def test_node_id_matters_only_on_a_verdict_with_evidence(self) -> None:
        self.assertDecision("false", [[review("Good to merge.", node_id=MISSING)]])


# claude[bot] verdicts as GitHub's GraphQL API reported them on 2026-10-31:
# (node id, submittedAt, updatedAt). None has lastEditedAt set, and neither
# does any of the 28,232 submitted reviews in the org's 22 repositories.
OBSERVED = {
    # mctl-api#875: an approval nobody touched afterwards.
    "untouched": ("PRR_kwDORfnDZc8AAAABVN1ZDw", "2026-10-30T21:18:37Z", "2026-10-30T21:18:37Z"),
    # portfolio#84: created as a pending review 7 s before it was submitted.
    "submitted from pending": ("PRR_kwDOUVyImc8AAAABNV3KQQ", "2026-09-13T09:26:38Z", "2026-09-13T09:26:38Z"),
    # mctl-api#367: untouched too, but GitHub wrote updatedAt across a second
    # boundary. 16 of 6,950 untouched verdicts look like this.
    "one-second skew": ("PRR_kwDORfnDZc8AAAABO4ek4Q", "2026-09-23T16:26:02Z", "2026-09-23T16:26:03Z"),
    # mctl-agents#856: moved onto a new head 10 s after it was submitted, the
    # shortest of the 240 moves seen.
    "moved after 10 s": ("PRR_kwDOSNnDY88AAAABUorV3A", "2026-10-28T08:32:13Z", "2026-10-28T08:32:23Z"),
    # mctl-design#144: moved onto a commit created before the verdict and
    # pushed after it.
    "moved after 25 s": ("PRR_kwDOSfH2c88AAAABS25k4g", "2026-10-17T22:53:04Z", "2026-10-17T22:53:29Z"),
    # mctl-agents#810, the case in #283: moved four days later.
    "moved after 4 days": ("PRR_kwDOSNnDY88AAAABUMbFSg", "2026-10-26T09:56:57Z", "2026-10-30T07:52:45Z"),
}


class ObservedRecordTest(DedupCase):
    """The decision on GitHub's own records for real verdicts. Each one is
    given the evidence line for this head and base, so only the record
    decides."""

    def assertObserved(self, expected: str, name: str) -> None:
        node, submitted, updated = OBSERVED[name]
        record = {"id": node, "lastEditedAt": None, "updatedAt": updated, "submittedAt": submitted}
        with self.subTest(record=name):
            self.assertDecision(expected, [[review(evidence(), node_id=node)]],
                                graphql_raw=json.dumps({"data": {"nodes": [record]}}))

    def test_untouched_verdicts_are_evidence(self) -> None:
        self.assertObserved("true", "untouched")
        self.assertObserved("true", "submitted from pending")

    def test_moved_verdicts_are_not_evidence(self) -> None:
        # Moves this close to submission fall in the same minute, so a check
        # coarser than the second, or with any tolerance, would accept them.
        self.assertObserved("false", "moved after 10 s")
        self.assertObserved("false", "moved after 25 s")
        self.assertObserved("false", "moved after 4 days")

    def test_one_second_skew_is_reviewed_again(self) -> None:
        # Exact equality is deliberate: a tolerance would also accept an edit
        # made inside it. Such a verdict costs one extra review.
        self.assertObserved("false", "one-second skew")


class UnknownTest(DedupCase):
    """A read that did not complete is neither "reviewed" nor "not reviewed"."""

    def assertUnknown(self, pages: list, **kwargs: object) -> None:
        proc = self.decide(pages, **kwargs)
        self.assertEqual("unknown", self.sandbox.outcome(), proc.stdout)
        self.assertIn("::warning::Could not tell whether", proc.stdout)
        self.assertEqual([], self.sandbox.comments())

    def test_failed_first_page(self) -> None:
        for shape in ("pages", "merged"):
            with self.subTest(shape=shape):
                self.assertUnknown([[review(evidence())]], shape=shape, fail_after=0)

    def test_failure_after_a_page_is_unknown_whatever_that_page_held(self) -> None:
        # The prefix is valid JSON either way: only the exit status says the
        # listing is incomplete. Evidence on page one does not make it true,
        # and its absence does not make it false.
        for first in ([review(evidence())], [review("No P1/P2 findings (0 P3). Good to merge.")]):
            for shape in ("pages", "merged"):
                with self.subTest(first=first[0]["body"], shape=shape):
                    self.assertUnknown([first, [review(evidence())]], shape=shape, fail_after=1)

    def test_malformed_review_list(self) -> None:
        for raw in (
            "",
            "not json",
            '{"message": "Bad credentials"}',
            "{}",
            "[1, 2]",
            '["claude[bot]"]',
            '[{"user": "claude[bot]", "state": "APPROVED"}]',
            '[{"id": 1}] [{"id"',
        ):
            with self.subTest(raw=raw):
                self.assertUnknown([raw])

    def test_unusable_pinned_sha(self) -> None:
        for head, base in (("", BASE), (HEAD, ""), ("null", BASE), (HEAD[:7], BASE), (HEAD.upper(), BASE)):
            with self.subTest(head=head, base=base):
                self.assertUnknown([[review(evidence(head, base))]], head=head, base=base)

    def test_verdict_with_evidence_but_no_node_id(self) -> None:
        for node_id in (MISSING, None, "", 5650171210):
            with self.subTest(node_id=node_id):
                self.assertUnknown([[review(evidence(), node_id=node_id)]])
        self.assertEqual([], self.sandbox.edit_requests())

    def test_failed_edit_history_read(self) -> None:
        # The fake prints an unedited history and then fails: only the exit
        # status says the read did not succeed.
        self.assertUnknown([[review(evidence())]], graphql_fail=True)

    def test_unusable_edit_history(self) -> None:
        verdict = review(evidence())
        node = verdict["node_id"]
        fields = {"id": node, "lastEditedAt": None,
                  "updatedAt": "2026-10-26T09:56:57Z", "submittedAt": "2026-10-26T09:56:57Z"}
        clean = json.dumps({"data": {"nodes": [fields]}})
        for raw in (
            "",
            "not json",
            "{}",
            "[]",
            '{"data": null}',
            '{"data": {"nodes": null}}',
            '{"data": {"nodes": []}}',
            '{"data": {"nodes": [null]}}',
            '{"data": {"nodes": [{}]}}',
            json.dumps({"data": {"nodes": [{"id": node}]}}),
            json.dumps({"data": {"nodes": [dict(fields, id="PRR_kwDOSomethingElse")]}}),
            json.dumps({"data": {"nodes": [fields] * 2}}),
            json.dumps({"data": {"nodes": [fields]}, "errors": [{"message": "Could not resolve to a node"}]}),
            json.dumps({"data": {"nodes": [{k: v for k, v in fields.items() if k != "updatedAt"}]}}),
            json.dumps({"data": {"nodes": [{k: v for k, v in fields.items() if k != "submittedAt"}]}}),
            json.dumps({"data": {"nodes": [{k: v for k, v in fields.items() if k != "lastEditedAt"}]}}),
            json.dumps({"data": {"nodes": [dict(fields, updatedAt=None, submittedAt=None)]}}),
            json.dumps({"data": {"nodes": [dict(fields, updatedAt=1761472617, submittedAt=1761472617)]}}),
            json.dumps({"data": {"nodes": [dict(fields, lastEditedAt=False)]}}),
            clean + clean,
            clean[:-3],
        ):
            with self.subTest(raw=raw):
                self.assertUnknown([[verdict]], graphql_raw=raw)


class WiringTest(unittest.TestCase):
    EVIDENCE = "Reviewed head=${{ needs.gate.outputs.sha }} base=${{ needs.gate.outputs.base_sha }}"

    def test_every_verdict_command_ends_with_the_evidence_line(self) -> None:
        bodies = re.findall(r'-f event="(?:APPROVE|REQUEST_CHANGES)" -f body="([^"]*)"', PROMPT)
        self.assertEqual(3, len(bodies), "trivial approval, approval, request changes")
        for body in bodies:
            with self.subTest(body=body):
                self.assertTrue(body.endswith(" " + self.EVIDENCE))
        self.assertIn(f"`{self.EVIDENCE}`", PROMPT)

    def test_dedup_and_prompt_read_the_same_pinned_pair(self) -> None:
        env = REVIEW_JOB["env"]
        self.assertEqual("${{ needs.gate.outputs.sha }}", env["HEAD_SHA"])
        self.assertEqual("${{ needs.gate.outputs.base_sha }}", env["BASE_SHA"])
        self.assertNotIn("HEAD_SHA", DEDUP.get("env", {}))
        self.assertNotIn("BASE_SHA", DEDUP.get("env", {}))

    def test_only_found_evidence_skips_the_review(self) -> None:
        # `unknown` must run the review just as `false` does, so every gate
        # downstream compares against 'true' and nothing else.
        gated = [s for s in STEPS if "steps.dedup.outputs.already_reviewed" in str(s.get("if", ""))]
        self.assertEqual(
            ["Checkout", "Classify PR complexity and pick model",
             "Re-check staleness immediately before the review call",
             "Check fallback token availability", "Detect entry-workflow self-edit",
             "Claude review"],
            [s["name"] for s in gated])
        for s in gated:
            with self.subTest(step=s["name"]):
                refs = re.findall(r"steps\.dedup\.outputs\.already_reviewed\s*(\S+)\s*(\S+)", s["if"])
                self.assertEqual([("!=", "'true'")], refs)


if __name__ == "__main__":
    unittest.main()
