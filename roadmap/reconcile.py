#!/usr/bin/env python3
"""Reconcile the declared roadmap state against GitHub, and report divergence.

    roadmap-state.yaml   (what we believe)
              |
              v
    observe GitHub       (what is)
              |
              v
    reconcile()  ->  RoadmapSnapshot
                       ALIGNED | DRIFT | UNEXPECTED_SILENCE | OBSERVATION_FAILED

Why this shape rather than a watcher
------------------------------------
A watcher reports events: a PR appeared, an issue closed. It cannot report the
absence of an event, because absence and blindness look identical from the
outside. On 2026-09-11 a watcher in this org sat silent for six hours while six
pull requests opened: its `gh` call was failing and `|| true` swallowed the
error, so an empty result meant "no PRs" to every reader. Nothing about that
was visible until someone asked GitHub directly.

So this tool asserts state, and treats a failed observation as a first-class
red result. `OBSERVATION_FAILED` is never merged into "nothing found" and never
degrades to zero: if a probe cannot read the file it needs, that item is red,
not aligned. That single rule is what makes the earlier bug structurally
impossible here.

Read-only. It issues GitHub reads and writes nothing but its own output files.

Usage
-----
    reconcile.py --state roadmap/roadmap-state.yaml \
                 --json roadmap/snapshot.json \
                 --markdown roadmap/ROADMAP.md
    reconcile.py --selftest        # fixtures, no network

Exit codes: 0 everything ALIGNED, 1 at least one item not ALIGNED, 2 the run
itself could not be completed (bad state file, no credentials).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - environment problem, not a finding
    print("reconcile: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    raise SystemExit(2)

ALIGNED = "ALIGNED"
DRIFT = "DRIFT"
UNEXPECTED_SILENCE = "UNEXPECTED_SILENCE"
OBSERVATION_FAILED = "OBSERVATION_FAILED"

# Worst-first. OBSERVATION_FAILED outranks DRIFT because a divergence you could
# not observe is not a divergence you may report: claiming DRIFT on an
# unobserved assertion would be inventing a finding. SILENCE is last because it
# is a statement about time, and only meaningful once the state itself agrees.
SEVERITY = [OBSERVATION_FAILED, DRIFT, UNEXPECTED_SILENCE, ALIGNED]

PHASES = ("done", "now", "next", "later")


class ObservationFailure(Exception):
    """GitHub could not be observed. Never a value, never a zero."""


# ── duration parsing ──────────────────────────────────────────────────────

_DURATION = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    m = _DURATION.match(str(text))
    if not m:
        raise ValueError(f"unparsable duration {text!r} (want e.g. 6h, 30m, 2d)")
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2)]


# ── comparison expressions ────────────────────────────────────────────────

_EXPECT = re.compile(r"^\s*(==|!=|>=|<=|>|<)\s*(\d+)\s*$")


def compare(actual: int, expression: str) -> bool:
    m = _EXPECT.match(str(expression))
    if not m:
        raise ValueError(f"unparsable expectation {expression!r} (want e.g. '>= 1')")
    op, rhs = m.group(1), int(m.group(2))
    return {
        "==": actual == rhs,
        "!=": actual != rhs,
        ">=": actual >= rhs,
        "<=": actual <= rhs,
        ">": actual > rhs,
        "<": actual < rhs,
    }[op]


# ── GitHub access ─────────────────────────────────────────────────────────

ISSUE_REF = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<number>\d+)$")


def parse_ref(ref: str) -> tuple[str, str, int]:
    m = ISSUE_REF.match(str(ref))
    if not m:
        raise ValueError(f"unparsable issue reference {ref!r} (want owner/repo#123)")
    return m.group("owner"), m.group("repo"), int(m.group("number"))


class GitHub:
    """Thin `gh` wrapper. Every non-zero exit becomes ObservationFailure.

    Deliberately not tolerant: there is no code path here that turns a failed
    call into an empty list. That tolerance is the bug this tool exists to
    make impossible.
    """

    def __init__(self, runner=None):
        self._run = runner or self._subprocess

    @staticmethod
    def _subprocess(args: list[str]) -> str:
        try:
            proc = subprocess.run(
                ["gh", *args], capture_output=True, text=True, timeout=60)
        except FileNotFoundError as exc:
            raise ObservationFailure("gh is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise ObservationFailure(f"gh timed out: {' '.join(args)}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise ObservationFailure(
                f"gh exited {proc.returncode}: {detail[0] if detail else 'no output'}")
        return proc.stdout

    def graphql(self, query: str, **variables: Any) -> dict:
        args = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            flag = "-F" if isinstance(value, int) else "-f"
            args += [flag, f"{key}={value}"]
        raw = self._run(args)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ObservationFailure(f"graphql returned non-JSON: {exc}") from exc
        if payload.get("errors"):
            first = payload["errors"][0].get("message", "unknown error")
            raise ObservationFailure(f"graphql error: {first}")
        return payload["data"]

    def rest(self, path: str) -> Any:
        raw = self._run(["api", path])
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ObservationFailure(f"{path} returned non-JSON: {exc}") from exc

    def file_text(self, repo: str, path: str) -> str:
        import base64
        data = self.rest(f"repos/{repo}/contents/{path}")
        if isinstance(data, list):
            raise ObservationFailure(f"{repo}/{path} is a directory, expected a file")
        content = data.get("content")
        if content is None:
            raise ObservationFailure(f"{repo}/{path} carried no content")
        try:
            return base64.b64decode(content).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - any decode problem is a failure
            raise ObservationFailure(f"{repo}/{path} did not decode: {exc}") from exc

    def tree_files(self, repo: str, path: str) -> list[str]:
        """Every blob path under `path`, recursively."""
        data = self.rest(f"repos/{repo}/git/trees/HEAD?recursive=1")
        tree = data.get("tree")
        if tree is None:
            raise ObservationFailure(f"{repo}: git tree carried no entries")
        if data.get("truncated"):
            raise ObservationFailure(
                f"{repo}: git tree was truncated, so a count over it would be a guess")
        prefix = path.rstrip("/") + "/"
        return [e["path"] for e in tree
                if e.get("type") == "blob" and e.get("path", "").startswith(prefix)]


ISSUE_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      number title url state updatedAt
      timelineItems(last: 60, itemTypes: [CROSS_REFERENCED_EVENT]) {
        nodes {
          ... on CrossReferencedEvent {
            source {
              ... on PullRequest {
                number url state isDraft updatedAt reviewDecision headRefOid
                mergeable mergeStateStatus
                closingIssuesReferences(first: 20) {
                  nodes { number repository { nameWithOwner } }
                }
                reviews(last: 20, states: [APPROVED, CHANGES_REQUESTED]) {
                  nodes { state submittedAt commit { oid } }
                }
                reviewThreads(first: 100) {
                  nodes { isResolved isOutdated }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


@dataclass
class IssueObservation:
    number: int
    title: str
    url: str
    state: str                  # OPEN | CLOSED
    updated_at: str
    open_prs: list[dict] = field(default_factory=list)
    last_activity: str = ""


def observe_issue(gh: GitHub, ref: str) -> IssueObservation:
    owner, repo, number = parse_ref(ref)
    data = gh.graphql(ISSUE_QUERY, owner=owner, repo=repo, number=number)
    issue = (data.get("repository") or {}).get("issue")
    if issue is None:
        raise ObservationFailure(f"{ref} not found")

    open_prs, timestamps = [], [issue["updatedAt"]]
    slug = f"{owner}/{repo}"
    for node in (issue.get("timelineItems") or {}).get("nodes") or []:
        source = (node or {}).get("source") or {}
        if not source.get("number"):
            continue
        # A cross-reference is only a mention: any PR whose body names this
        # issue lands here, including one implementing a different issue that
        # merely cites this one as context. Counting those as
        # "implementation: active" produced a false DRIFT on mctl-agents#195,
        # attributing PR #340 (which implements #264) to it. Only a PR that
        # declares it closes this issue is implementation of it.
        closes = [
            n for n in (source.get("closingIssuesReferences") or {}).get("nodes") or []
            if n.get("number") == number
            and (n.get("repository") or {}).get("nameWithOwner") == slug
        ]
        if not closes:
            continue
        timestamps.append(source["updatedAt"])
        if source.get("state") == "OPEN":
            open_prs.append({
                "number": source["number"],
                "url": source["url"],
                "draft": bool(source.get("isDraft")),
                "review_decision": source.get("reviewDecision") or "NONE",
                "head_reviewed": _head_is_reviewed(source),
                "merge": _merge_state(source),
                "updated_at": source["updatedAt"],
            })
    return IssueObservation(
        number=issue["number"], title=issue["title"], url=issue["url"],
        state=issue["state"], updated_at=issue["updatedAt"],
        open_prs=sorted(open_prs, key=lambda p: p["number"]),
        last_activity=max(timestamps),
    )


# ── probes ────────────────────────────────────────────────────────────────

def run_probe(gh: GitHub, probe: dict) -> tuple[int, str]:
    """Return (count, description). Raises ObservationFailure, never returns 0
    to mean "could not look"."""
    kind = probe.get("kind")
    if kind == "file_line_match_count":
        text = gh.file_text(probe["repo"], probe["path"])
        pattern = re.compile(probe["pattern"], re.M)
        count = sum(1 for line in text.splitlines() if pattern.search(line))
        return count, f"{probe['repo']}/{probe['path']} lines matching /{probe['pattern']}/"
    if kind == "dir_file_match_count":
        files = gh.tree_files(probe["repo"], probe["path"])
        name_re = re.compile(probe["file_pattern"])
        candidates = [f for f in files if name_re.search(f.rsplit("/", 1)[-1])]
        content_re = re.compile(probe["content_pattern"])
        count = sum(1 for f in candidates if content_re.search(gh.file_text(probe["repo"], f)))
        return count, (f"{len(candidates)} file(s) under {probe['repo']}/{probe['path']} "
                       f"matching /{probe['content_pattern']}/")
    raise ObservationFailure(f"unknown probe kind {kind!r}")


# ── reconciliation ────────────────────────────────────────────────────────

@dataclass
class ItemResult:
    id: str
    title: str
    phase: str
    state: str
    epic: str | None = None
    issue: str | None = None
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    note: str | None = None


def _head_is_reviewed(pr: dict) -> bool:
    """Has anything decisive been said about the commit that is actually there?

    GitHub's `reviewDecision` is per reviewer, not per commit: a PR whose head
    moved after the last review keeps reporting the old verdict. Both
    mctl-gitops#1190 and mctl-agents#340 read as APPROVED on 2026-09-11 while
    their newest commits had been seen by nobody. Merging on that is merging
    an unreviewed diff, so the reconciler refuses to call it either way.
    """
    head = pr.get("headRefOid")
    if not head:
        return False
    for review in (pr.get("reviews") or {}).get("nodes") or []:
        if ((review.get("commit") or {}).get("oid")) == head:
            return True
    return False


def _merge_state(pr: dict) -> str:
    """Why this PR cannot be merged, in the caller's vocabulary.

    `mergeStateStatus: BLOCKED` on its own says nothing actionable, and a PR
    that is approved, green and still unmergeable is precisely the shape that
    reads as "ready" to a human skimming and as a generic disagreement to a
    reconciler. mctl-telegram#627 is the case: APPROVED on its head, every
    check passing, mergeable, and blocked by fourteen unresolved review
    conversations, because that repo's main requires resolution.

    `blocked-unresolved-check` is deliberate: a block we cannot explain is
    worth a person, and must not be quietly filed under one we can.
    """
    status = (pr.get("mergeStateStatus") or "").upper()
    if pr.get("isDraft"):
        return "draft"
    if (pr.get("mergeable") or "").upper() == "CONFLICTING" or status == "DIRTY":
        return "conflicted"
    if status == "CLEAN" or status == "HAS_HOOKS":
        return "ready"
    if status == "BEHIND":
        return "blocked-behind-base"
    if status == "UNSTABLE":
        return "blocked-checks"
    if status == "BLOCKED":
        # Review first, threads second. Both are often true at once, and a
        # reviewer asking for changes is the thing that has to move before
        # resolving threads means anything -- reporting the threads there
        # would name the symptom over the cause.
        if (pr.get("reviewDecision") or "") != "APPROVED":
            return "blocked-review"
        unresolved = sum(
            1 for t in (pr.get("reviewThreads") or {}).get("nodes") or []
            if not t.get("isResolved") and not t.get("isOutdated"))
        if unresolved:
            return f"blocked-conversations:{unresolved}"
        return "blocked-unresolved-check"
    return f"unknown:{status.lower() or 'none'}"


def _merge_summary(open_prs: list[dict]) -> str:
    if not open_prs:
        return "none"
    states = [p.get("merge", "unknown") for p in open_prs]
    for preferred in states:
        if preferred != "ready":
            return preferred
    return "ready"


def _review_state(open_prs: list[dict]) -> str:
    """'clean' | 'blocking-findings' | 'none' | 'unreviewed-head' | 'unknown'."""
    if not open_prs:
        return "none"
    if any(not p.get("head_reviewed") for p in open_prs):
        return "unreviewed-head"
    decisions = {p["review_decision"] for p in open_prs}
    if "CHANGES_REQUESTED" in decisions:
        return "blocking-findings"
    if "APPROVED" in decisions:
        return "clean"
    return "unknown"


def reconcile_item(gh: GitHub, item: dict, defaults: dict, now: dt.datetime) -> ItemResult:
    result = ItemResult(
        id=item["id"],
        title=item.get("title", item["id"]),
        phase=item.get("phase", "now"),
        state=ALIGNED,
        epic=item.get("epic"),
        issue=item.get("issue"),
        note=item.get("note"),
    )
    expected = item.get("expected") or {}
    observed_everything = True

    issue_obs = None
    if item.get("issue"):
        try:
            issue_obs = observe_issue(gh, item["issue"])
        except ObservationFailure as exc:
            result.state = OBSERVATION_FAILED
            result.reasons.append(f"could not observe {item['issue']}: {exc}")
            return result
        except ValueError as exc:
            result.state = OBSERVATION_FAILED
            result.reasons.append(str(exc))
            return result
        result.evidence["issue_state"] = issue_obs.state.lower()
        result.evidence["open_prs"] = [p["number"] for p in issue_obs.open_prs]
        result.evidence["review"] = _review_state(issue_obs.open_prs)
        result.evidence["merge"] = _merge_summary(issue_obs.open_prs)
        result.evidence["last_activity"] = issue_obs.last_activity

    mismatches: list[str] = []

    want_issue = expected.get("issue")
    if want_issue and issue_obs:
        if issue_obs.state.lower() != str(want_issue).lower():
            mismatches.append(
                f"issue expected {want_issue}, actual {issue_obs.state.lower()}")

    want_impl = expected.get("implementation")
    if want_impl and issue_obs:
        actual_impl = "active" if issue_obs.open_prs else "none"
        if actual_impl != want_impl:
            detail = (f"PR {', '.join('#' + str(p['number']) for p in issue_obs.open_prs)}"
                      if issue_obs.open_prs else "no open PR")
            mismatches.append(
                f"implementation expected {want_impl}, actual {actual_impl} ({detail})")

    want_review = expected.get("review")
    if want_review and issue_obs:
        actual_review = _review_state(issue_obs.open_prs)
        if actual_review != want_review:
            mismatches.append(
                f"review expected {want_review}, actual {actual_review}")

    want_merge = expected.get("merge")
    if want_merge and issue_obs:
        actual_merge = _merge_summary(issue_obs.open_prs)
        # A count is evidence, not identity: "blocked-conversations:14" and
        # ":9" are the same situation and must not churn the report.
        if actual_merge.split(":")[0] != str(want_merge).split(":")[0]:
            mismatches.append(f"merge expected {want_merge}, actual {actual_merge}")

    for probe in item.get("probes") or []:
        try:
            count, description = run_probe(gh, probe)
        except ObservationFailure as exc:
            result.state = OBSERVATION_FAILED
            result.reasons.append(f"probe {probe.get('id', '?')}: {exc}")
            observed_everything = False
            continue
        except (KeyError, re.error) as exc:
            result.state = OBSERVATION_FAILED
            result.reasons.append(f"probe {probe.get('id', '?')} is malformed: {exc}")
            observed_everything = False
            continue
        result.evidence.setdefault("probes", {})[probe.get("id", "?")] = count
        if not compare(count, probe["expect"]):
            mismatches.append(
                f"probe {probe.get('id', '?')}: {description} = {count}, "
                f"expected {probe['expect']}")

    if not observed_everything:
        return result

    if mismatches:
        result.state = DRIFT
        result.reasons.extend(mismatches)
        return result

    # Silence is only meaningful once the state itself agrees: an item in
    # DRIFT already has a reason to be looked at.
    silence_window = item.get("max_silence", defaults.get("max_silence"))
    if silence_window and issue_obs and expected.get("implementation") == "active":
        try:
            seconds = parse_duration(silence_window)
        except ValueError as exc:
            result.state = OBSERVATION_FAILED
            result.reasons.append(str(exc))
            return result
        last = dt.datetime.fromisoformat(issue_obs.last_activity.replace("Z", "+00:00"))
        quiet_for = (now - last).total_seconds()
        if quiet_for > seconds:
            result.state = UNEXPECTED_SILENCE
            result.reasons.append(
                f"believed to be under active implementation, but nothing has moved "
                f"on {item['issue']} or its PRs for "
                f"{int(quiet_for // 3600)}h (allowed {silence_window})")
    return result


def reconcile(gh: GitHub, state: dict, now: dt.datetime) -> dict:
    defaults = state.get("defaults") or {}
    items = [reconcile_item(gh, item, defaults, now) for item in state.get("items") or []]
    worst = ALIGNED
    for item in items:
        if SEVERITY.index(item.state) < SEVERITY.index(worst):
            worst = item.state
    return {
        "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "overall": worst,
        "counts": {s: sum(1 for i in items if i.state == s) for s in SEVERITY},
        "items": [asdict(i) for i in items],
    }


# ── rendering ─────────────────────────────────────────────────────────────

BADGE = {
    ALIGNED: "OK",
    DRIFT: "DRIFT",
    UNEXPECTED_SILENCE: "SILENT",
    OBSERVATION_FAILED: "UNOBSERVED",
}

PHASE_TITLE = {
    "done": "Done",
    "now": "Now",
    "next": "Next",
    "later": "Later",
}


def render_markdown(snapshot: dict) -> str:
    out = [
        "# mctlhq roadmap",
        "",
        "<!-- Generated by roadmap/reconcile.py from roadmap-state.yaml. Do not edit by hand. -->",
        "",
        f"Reconciled {snapshot['generated_at']} — **{snapshot['overall']}**.",
        "",
        "| state | meaning | count |",
        "|---|---|---|",
        f"| OK | declared state matches GitHub | {snapshot['counts'][ALIGNED]} |",
        f"| DRIFT | observed, and it disagrees with what we declared | {snapshot['counts'][DRIFT]} |",
        f"| SILENT | believed active, nothing has moved | {snapshot['counts'][UNEXPECTED_SILENCE]} |",
        f"| UNOBSERVED | could not be checked — not the same as nothing found | {snapshot['counts'][OBSERVATION_FAILED]} |",
        "",
    ]
    for phase in PHASES:
        rows = [i for i in snapshot["items"] if i["phase"] == phase]
        if not rows:
            continue
        out += [f"## {PHASE_TITLE[phase]}", ""]
        for item in rows:
            ref = f" — {item['issue']}" if item.get("issue") else ""
            out.append(f"### {BADGE[item['state']]} · {item['title']}{ref}")
            if item.get("epic"):
                out.append(f"Epic {item['epic']}.")
            for reason in item.get("reasons") or []:
                out.append(f"- {reason}")
            if item.get("note"):
                out += ["", item["note"].strip()]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def digest(snapshot: dict) -> str:
    """One short block, only about what is not ALIGNED."""
    lines = []
    for state in (OBSERVATION_FAILED, DRIFT, UNEXPECTED_SILENCE):
        for item in snapshot["items"]:
            if item["state"] != state:
                continue
            head = f"{BADGE[state]} {item['title']}"
            if item.get("issue"):
                head += f" ({item['issue']})"
            lines.append(head)
            lines += [f"    {r}" for r in item.get("reasons") or []]
    return "\n".join(lines)


def reportable_state(snapshot: dict) -> dict:
    """The part worth comparing between runs.

    Timestamps and counts move on their own; what a person needs to hear about
    is an item changing state or changing its reasons.
    """
    return {i["id"]: {"state": i["state"], "reasons": i["reasons"]}
            for i in snapshot["items"]}


# ── self-test ─────────────────────────────────────────────────────────────

def selftest() -> int:
    """Prove the four states, and above all that a failed read is not a zero."""
    failures: list[str] = []
    now = dt.datetime(2026, 9, 11, 18, 0, tzinfo=dt.timezone.utc)

    def check(condition, message):
        if not condition:
            failures.append(message)

    def fake_issue(state="OPEN", prs=(), updated="2026-09-11T17:00:00Z",
                   mentions=(), head_seen=True, merge_status="CLEAN",
                   unresolved=0):
        def node(n, d, closes, head_seen=True, merge_status="CLEAN", unresolved=0):
            return {"source": {
                "number": n, "url": f"u{n}", "state": "OPEN", "isDraft": False,
                "reviewDecision": d, "updatedAt": updated, "headRefOid": "abc123",
                "mergeable": "MERGEABLE", "mergeStateStatus": merge_status,
                "reviewThreads": {"nodes": [
                    {"isResolved": False, "isOutdated": False}
                ] * unresolved},
                "reviews": {"nodes": [
                    {"state": d, "submittedAt": updated,
                     "commit": {"oid": "abc123" if head_seen else "older99"}}
                ]},
                "closingIssuesReferences": {"nodes": (
                    [{"number": 1, "repository": {"nameWithOwner": "o/r"}}]
                    if closes else
                    [{"number": 7, "repository": {"nameWithOwner": "o/other"}}])}}}
        nodes = [node(n, d, True, head_seen=head_seen,
                      merge_status=merge_status, unresolved=unresolved)
                 for n, d in prs]
        nodes += [node(n, "NONE", False) for n in mentions]
        return {"repository": {"issue": {
            "number": 1, "title": "t", "url": "u", "state": state,
            "updatedAt": updated, "timelineItems": {"nodes": nodes}}}}

    class StubGH(GitHub):
        def __init__(self, issue=None, files=None, fail=None):
            super().__init__(runner=lambda args: (_ for _ in ()).throw(
                ObservationFailure("stub should not shell out")))
            self._issue, self._files, self._fail = issue, files or {}, fail

        def graphql(self, query, **variables):
            if self._fail:
                raise ObservationFailure(self._fail)
            return self._issue

        def file_text(self, repo, path):
            if self._fail:
                raise ObservationFailure(self._fail)
            if path not in self._files:
                raise ObservationFailure(f"{path} not found")
            return self._files[path]

        def tree_files(self, repo, path):
            if self._fail:
                raise ObservationFailure(self._fail)
            return [p for p in self._files if p.startswith(path.rstrip('/') + '/')]

    item_active = {"id": "x", "title": "X", "issue": "o/r#1", "phase": "now",
                   "expected": {"issue": "open", "implementation": "active",
                                "review": "blocking-findings"},
                   "max_silence": "6h"}

    # ALIGNED
    r = reconcile_item(StubGH(fake_issue(prs=[(9, "CHANGES_REQUESTED")])),
                       item_active, {}, now)
    check(r.state == ALIGNED, f"expected ALIGNED, got {r.state}: {r.reasons}")

    # DRIFT: the review turned clean while we still believe it is blocked.
    r = reconcile_item(StubGH(fake_issue(prs=[(9, "APPROVED")])), item_active, {}, now)
    check(r.state == DRIFT, f"expected DRIFT, got {r.state}")
    check(any("review expected" in x for x in r.reasons), f"bad reasons: {r.reasons}")

    # UNEXPECTED_SILENCE: state agrees, but nothing has moved for a day.
    r = reconcile_item(
        StubGH(fake_issue(prs=[(9, "CHANGES_REQUESTED")], updated="2026-09-10T05:00:00Z")),
        item_active, {}, now)
    check(r.state == UNEXPECTED_SILENCE, f"expected UNEXPECTED_SILENCE, got {r.state}")

    # OBSERVATION_FAILED: the call fails. This must NOT read as "no PRs", which
    # would otherwise look like DRIFT on implementation, or as ALIGNED for an
    # item expecting none.
    r = reconcile_item(StubGH(fail="gh exited 1: HTTP 502"), item_active, {}, now)
    check(r.state == OBSERVATION_FAILED, f"expected OBSERVATION_FAILED, got {r.state}")
    item_expect_none = dict(item_active, expected={"issue": "open", "implementation": "none"})
    r = reconcile_item(StubGH(fail="gh exited 1: HTTP 502"), item_expect_none, {}, now)
    check(r.state == OBSERVATION_FAILED,
          f"a failed read must not satisfy 'implementation: none', got {r.state}")

    # A probe that cannot read its file is red, not a zero that happens to
    # satisfy '== 0'.
    probe_item = {"id": "p", "title": "P", "phase": "now", "expected": {},
                  "probes": [{"id": "z", "kind": "file_line_match_count",
                              "repo": "o/r", "path": "missing.txt",
                              "pattern": "^zones/", "expect": "== 0"}]}
    r = reconcile_item(StubGH(files={}), probe_item, {}, now)
    check(r.state == OBSERVATION_FAILED,
          f"an unreadable probe must not satisfy '== 0', got {r.state}")

    readable = dict(probe_item,
                    probes=[dict(probe_item["probes"][0], path="a.txt")])
    r = reconcile_item(StubGH(files={"a.txt": "zones/mctl-ru\naccount\n"}),
                       readable, {}, now)
    check(r.state == DRIFT, f"expected DRIFT from a real count, got {r.state}")
    check(r.evidence["probes"]["z"] == 1, f"bad probe count: {r.evidence}")

    # A truncated git tree is a failed observation, not a short list.
    class TruncatedGH(GitHub):
        def __init__(self):
            super().__init__(runner=lambda args: json.dumps(
                {"tree": [{"type": "blob", "path": "d/a.yaml"}], "truncated": True}))
    try:
        TruncatedGH().tree_files("o/r", "d")
        failures.append("a truncated tree must raise, not return a partial list")
    except ObservationFailure:
        pass

    # A PR that merely mentions the issue is not implementation of it. This
    # false positive attributed PR #340 (implementing mctl-agents#264) to
    # mctl-agents#195 on the first live run.
    item_none = dict(item_active, expected={"issue": "open", "implementation": "none"})
    r = reconcile_item(StubGH(fake_issue(mentions=[340])), item_none, {}, now)
    check(r.state == ALIGNED,
          f"a mention must not read as implementation, got {r.state}: {r.reasons}")
    r = reconcile_item(StubGH(fake_issue(prs=[(9, "CHANGES_REQUESTED")], mentions=[340])),
                       item_active, {}, now)
    check(r.evidence["open_prs"] == [9],
          f"mentions leaked into open_prs: {r.evidence['open_prs']}")

    # A verdict that predates the current head is not a verdict on it.
    r = reconcile_item(StubGH(fake_issue(prs=[(9, "APPROVED")], head_seen=False)),
                       dict(item_active, expected={"issue": "open",
                                                   "implementation": "active",
                                                   "review": "clean"}), {}, now)
    check(r.state == DRIFT and any("unreviewed-head" in x for x in r.reasons),
          f"a stale APPROVED must not read as clean: {r.state} {r.reasons}")

    # mctl-telegram#627's shape: approved on its head, every check green,
    # mergeable -- and unmergeable, because that repo's main requires review
    # conversations to be resolved. It must read as neither ready nor a
    # generic disagreement.
    blocked = fake_issue(prs=[(9, "APPROVED")], merge_status="BLOCKED", unresolved=14)
    r = reconcile_item(StubGH(blocked),
                       dict(item_active, expected={"issue": "open",
                                                   "implementation": "active",
                                                   "review": "clean",
                                                   "merge": "ready"}), {}, now)
    check(r.state == DRIFT, f"an unmergeable approved PR must not be ALIGNED: {r.state}")
    check(r.evidence["merge"] == "blocked-conversations:14",
          f"merge reason not explicit: {r.evidence.get('merge')}")

    # When a reviewer is asking for changes AND threads are open, the review
    # is the cause and the threads the symptom.
    both = fake_issue(prs=[(9, "CHANGES_REQUESTED")], merge_status="BLOCKED",
                      unresolved=12)
    r = reconcile_item(StubGH(both),
                       dict(item_active, expected={"issue": "open",
                                                   "implementation": "active",
                                                   "review": "blocking-findings",
                                                   "merge": "blocked-review"}), {}, now)
    check(r.state == ALIGNED and r.evidence["merge"] == "blocked-review",
          f"review must outrank threads: {r.state} {r.evidence.get('merge')}")

    # Declaring the block we know about makes it aligned again: the count is
    # evidence, not identity, so 14 threads today and 9 tomorrow do not churn.
    r = reconcile_item(StubGH(blocked),
                       dict(item_active, expected={"issue": "open",
                                                   "implementation": "active",
                                                   "review": "clean",
                                                   "merge": "blocked-conversations"}),
                       {}, now)
    check(r.state == ALIGNED, f"a declared block should align: {r.state} {r.reasons}")

    # A block we cannot explain is its own answer, never folded into one we can.
    unexplained = fake_issue(prs=[(9, "APPROVED")], merge_status="BLOCKED", unresolved=0)
    r = reconcile_item(StubGH(unexplained),
                       dict(item_active, expected={"issue": "open",
                                                   "implementation": "active",
                                                   "review": "clean",
                                                   "merge": "ready"}), {}, now)
    check(r.evidence["merge"] == "blocked-unresolved-check",
          f"an unexplained block must say so: {r.evidence.get('merge')}")

    # Severity ordering.
    check(SEVERITY.index(OBSERVATION_FAILED) < SEVERITY.index(DRIFT),
          "OBSERVATION_FAILED must outrank DRIFT")

    for message in failures:
        print(f"FAIL: {message}", file=sys.stderr)
    if failures:
        return 1
    print("reconcile selftest: four states hold, and a failed read is never a zero")
    return 0


# ── entry point ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="roadmap/roadmap-state.yaml")
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--markdown", dest="md_out")
    parser.add_argument("--previous", help="prior snapshot.json, to report only on change")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        state = yaml.safe_load(pathlib.Path(args.state).read_text())
    except (OSError, yaml.YAMLError) as exc:
        print(f"reconcile: cannot read {args.state}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(state, dict) or not state.get("items"):
        print(f"reconcile: {args.state} declares no items", file=sys.stderr)
        return 2

    now = dt.datetime.now(dt.timezone.utc)
    snapshot = reconcile(GitHub(), state, now)

    if args.json_out:
        pathlib.Path(args.json_out).write_text(json.dumps(snapshot, indent=2) + "\n")
    if args.md_out:
        pathlib.Path(args.md_out).write_text(render_markdown(snapshot))

    changed = True
    if args.previous and pathlib.Path(args.previous).exists():
        try:
            before = json.loads(pathlib.Path(args.previous).read_text())
            changed = reportable_state(before) != reportable_state(snapshot)
        except (OSError, json.JSONDecodeError):
            # An unreadable previous snapshot is not evidence of no change.
            changed = True

    body = digest(snapshot)
    print(f"{snapshot['overall']}  " + "  ".join(
        f"{BADGE[s]}={snapshot['counts'][s]}" for s in SEVERITY))
    if body:
        print()
        print(body)

    if step_summary := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as fh:
            fh.write(f"### Roadmap: {snapshot['overall']}\n\n")
            fh.write(f"```\n{body or 'everything aligned'}\n```\n")
    if github_output := os.getenv("GITHUB_OUTPUT"):
        with open(github_output, "a") as fh:
            fh.write(f"overall={snapshot['overall']}\n")
            fh.write(f"changed={'true' if changed else 'false'}\n")

    return 0 if snapshot["overall"] == ALIGNED else 1


if __name__ == "__main__":
    raise SystemExit(main())
