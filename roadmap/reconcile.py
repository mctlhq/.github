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
      timelineItems(last: 100, itemTypes: [CROSS_REFERENCED_EVENT]) {
        pageInfo { hasPreviousPage }
        nodes {
          ... on CrossReferencedEvent {
            source {
              ... on PullRequest {
                number url state isDraft updatedAt reviewDecision headRefOid
                mergeable mergeStateStatus
                closingIssuesReferences(first: 100) {
                  pageInfo { hasNextPage }
                  nodes { number repository { nameWithOwner } }
                }
                reviews(last: 100, states: [APPROVED, CHANGES_REQUESTED]) {
                  pageInfo { hasPreviousPage }
                  nodes { state submittedAt commit { oid } }
                }
                reviewThreads(first: 100) {
                  pageInfo { hasNextPage }
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


def nodes_of(connection: dict | None, label: str) -> list[dict]:
    """Every node of a paged connection, or a refusal.

    Truncation is detected with `pageInfo`, not `totalCount`. On a filtered
    connection the two disagree: `IssueTimelineItemsConnection` carries three
    separate counters precisely because `totalCount` does not describe what
    an `itemTypes:` filter returned, and `PullRequestReviewConnection` has no
    counter that accounts for `states:` at all. Comparing an unfiltered total
    against a filtered list would have made every issue past a hundred
    timeline events permanently OBSERVATION_FAILED — the truncation guard
    manufacturing the state it exists to prevent. `hasNextPage` /
    `hasPreviousPage` answer the only question being asked: is there more
    beyond the page we took.
    """
    connection = connection or {}
    nodes = connection.get("nodes")
    if nodes is None:
        raise ObservationFailure(f"{label}: connection carried no nodes")
    page = connection.get("pageInfo") or {}
    if page.get("hasNextPage") or page.get("hasPreviousPage"):
        raise ObservationFailure(
            f"{label}: more entries exist beyond the {len(nodes)} returned, "
            f"so anything counted over them would be a guess")
    return nodes


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
    for node in nodes_of(issue.get("timelineItems"), f"{ref} cross-references"):
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
            n for n in nodes_of(source.get("closingIssuesReferences"),
                                f"PR #{source['number']} closing references")
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

                # Kept raw. _merge_state is evaluated only if the item asserts
                # `merge:`; computing it here let a transient
                # `mergeable: UNKNOWN` on a just-pushed PR fail an item that
                # never asked about merging, hiding a fully observed
                # implementation DRIFT behind an unrelated blind spot.
                "raw": source,
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


# ── validating the declaration ────────────────────────────────────────────

ITEM_KEYS = {"id", "title", "epic", "issue", "also", "phase", "expected",
             "probes", "max_silence", "note", "unlocks"}
EXPECTED_KEYS = {"issue", "implementation", "review", "merge"}
REVIEW_VALUES = {"clean", "blocking-findings", "none", "unreviewed-head", "unknown"}
MERGE_VALUES = {"ready", "draft", "conflicted", "none", "blocked-review",
                "blocked-review-required", "blocked-checks", "blocked-behind-base",
                "blocked-conversations", "blocked-unresolved-check"}
PROBE_KEYS = {
    "file_line_match_count": {"id", "kind", "repo", "path", "pattern", "expect"},
    "dir_file_match_count": {"id", "kind", "repo", "path", "file_pattern",
                             "content_pattern", "expect"},
}


def validate_state(state: dict) -> list[str]:
    """Refuse a declaration that asserts less than it appears to.

    An unrecognised key under `expected:` is the worst possible defect here:
    it reads like an assertion, is rendered like one, and checks nothing, so
    the item reports OK forever on the strength of a line nobody evaluates.
    That is indistinguishable from a passing test that was never run, so it is
    a hard error rather than a warning.
    """
    problems: list[str] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(state.get("items") or []):
        where = item.get("id") or f"items[{index}]"
        if not item.get("id"):
            problems.append(f"{where}: no id")
        elif item["id"] in seen_ids:
            problems.append(f"{where}: duplicate id")
        else:
            seen_ids.add(item["id"])

        for key in set(item) - ITEM_KEYS:
            problems.append(f"{where}: unknown key {key!r}")

        phase = item.get("phase", "now")
        if phase not in PHASES:
            problems.append(f"{where}: phase {phase!r} is not one of {PHASES}")

        expected = item.get("expected") or {}
        for key in set(expected) - EXPECTED_KEYS:
            problems.append(
                f"{where}: expected.{key} is not an assertion this tool "
                f"evaluates — it would report OK while checking nothing "
                f"(known: {', '.join(sorted(EXPECTED_KEYS))})")
        if expected.get("issue") not in (None, "open", "closed"):
            problems.append(f"{where}: expected.issue must be open or closed")
        if expected.get("implementation") not in (None, "active", "none"):
            problems.append(f"{where}: expected.implementation must be active or none")
        for key, vocabulary in (("review", REVIEW_VALUES), ("merge", MERGE_VALUES)):
            declared = expected.get(key)
            if declared is None:
                continue
            for value in (declared if isinstance(declared, list) else [declared]):
                if value not in vocabulary:
                    problems.append(
                        f"{where}: expected.{key} {value!r} is not a value this "
                        f"tool can produce (known: {', '.join(sorted(vocabulary))})")

        issue_keys = {"issue", "implementation", "review", "merge"} & set(expected)
        if issue_keys and not item.get("issue"):
            problems.append(
                f"{where}: asserts {', '.join(sorted(issue_keys))} but names no issue")

        if not expected and not item.get("probes"):
            problems.append(f"{where}: asserts nothing at all")

        for probe in item.get("probes") or []:
            pid = probe.get("id", "?")
            kind = probe.get("kind")
            if kind not in PROBE_KEYS:
                problems.append(f"{where}: probe {pid} has unknown kind {kind!r}")
                continue
            missing = PROBE_KEYS[kind] - set(probe) - {"id"}
            if missing:
                problems.append(
                    f"{where}: probe {pid} is missing {', '.join(sorted(missing))}")
            if "expect" in probe:
                try:
                    compare(0, probe["expect"])
                except ValueError as exc:
                    problems.append(f"{where}: probe {pid}: {exc}")
            for field_name in ("pattern", "file_pattern", "content_pattern"):
                if field_name in probe:
                    try:
                        re.compile(probe[field_name])
                    except re.error as exc:
                        problems.append(
                            f"{where}: probe {pid}: {field_name} is not a regex: {exc}")

        if "max_silence" in item:
            try:
                parse_duration(item["max_silence"])
            except ValueError as exc:
                problems.append(f"{where}: {exc}")
    if "max_silence" in (state.get("defaults") or {}):
        try:
            parse_duration(state["defaults"]["max_silence"])
        except ValueError as exc:
            problems.append(f"defaults: {exc}")
    return problems


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


def _render_want(want) -> str:
    return " or ".join(want) if isinstance(want, list) else str(want)


def _satisfies(actual: str, want) -> bool:
    """Does the observed value answer the declaration?

    A declaration may list several acceptable answers, because some
    assertions legitimately have more than one: an item under active
    implementation alternates between `blocking-findings` and
    `unreviewed-head` every time its author pushes a fix, and reporting that
    as drift would train people to ignore the report.

    The count in `blocked-conversations:N` is evidence rather than identity,
    so the bare name matches any N. The rule is deliberately confined to that
    one value: applied across the vocabulary it would also let a declared
    `unknown` be satisfied by "we could not read the field".
    """
    for candidate in (want if isinstance(want, list) else [want]):
        candidate = str(candidate)
        if actual == candidate:
            return True
        if (candidate == "blocked-conversations"
                and actual.startswith("blocked-conversations:")):
            return True
    return False


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
    for review in nodes_of(pr.get("reviews"), f"PR #{pr.get('number')} reviews"):
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
    mergeable = (pr.get("mergeable") or "").upper()

    # GitHub computes mergeability asynchronously: a PR touched since the last
    # background pass answers UNKNOWN. That is not a merge state, it is the
    # absence of one, and returning it as a value put a third path into the
    # very failure this file exists to close -- a declared `merge: ready`
    # would have been reported as DRIFT against something never observed.
    if mergeable == "UNKNOWN" or status == "UNKNOWN" or not status:
        raise ObservationFailure(
            f"PR #{pr.get('number')}: GitHub has not computed mergeability yet "
            f"(mergeable={mergeable or 'absent'}, status={status or 'absent'})")

    if pr.get("isDraft"):
        return "draft"
    if mergeable == "CONFLICTING" or status == "DIRTY":
        return "conflicted"
    if status in ("CLEAN", "HAS_HOOKS"):
        return "ready"
    if status == "BEHIND":
        return "blocked-behind-base"
    if status == "UNSTABLE":
        return "blocked-checks"
    if status == "BLOCKED":
        # reviewDecision has three inhabitants, not two, and two of them call
        # for opposite actions: CHANGES_REQUESTED means someone looked and
        # wants work; REVIEW_REQUIRED means nobody has looked at all, which is
        # where every pre-review PR under branch protection sits.
        decision = (pr.get("reviewDecision") or "").upper()
        if decision == "CHANGES_REQUESTED":
            return "blocked-review"
        if decision == "REVIEW_REQUIRED":
            return "blocked-review-required"
        # Outdated threads are counted. GitHub's "require conversation
        # resolution" gate counts every UNRESOLVED thread; a thread going
        # outdated because its line was edited does not resolve it, and the
        # merge box keeps refusing. Excluding them drove the count to zero on
        # exactly the PRs this branch exists to explain.
        unresolved = sum(
            1 for t in nodes_of(pr.get("reviewThreads"),
                                f"PR #{pr.get('number')} review threads")
            if not t.get("isResolved"))
        if unresolved:
            return f"blocked-conversations:{unresolved}"
        return "blocked-unresolved-check"
    raise ObservationFailure(
        f"PR #{pr.get('number')}: unrecognised mergeStateStatus {status!r}")


# Worst-first, so a summary over several PRs is deterministic rather than
# decided by whichever GitHub happened to return first.
MERGE_RANK = ["conflicted", "blocked-review", "blocked-review-required",
              "blocked-checks", "blocked-behind-base", "blocked-unresolved-check",
              "blocked-conversations", "draft", "ready"]


def _merge_summary(open_prs: list[dict]) -> str:
    """The most blocking merge state across this issue's open PRs."""
    if not open_prs:
        return "none"
    states = [_merge_state(p["raw"]) for p in open_prs]

    def rank(value: str) -> int:
        head = value.split(":")[0]
        if head not in MERGE_RANK:
            # Unreachable while _merge_state raises on anything it does not
            # recognise, and sorted last rather than first so that if it ever
            # becomes reachable an unknown value cannot quietly win.
            return len(MERGE_RANK)
        return MERGE_RANK.index(head)

    return min(states, key=rank)


def _review_state(open_prs: list[dict]) -> str:
    """'clean' | 'blocking-findings' | 'none' | 'unreviewed-head' | 'unknown'.

    Derived on demand, like the merge state and for the same reason: computing
    it while building the observation let a blind spot in one assertion fail an
    item that only asked about another.
    """
    if not open_prs:
        return "none"
    if any(not _head_is_reviewed(p["raw"]) for p in open_prs):
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
    # One list for what could not be observed, one for what diverged. No axis
    # returns early: the rule that a blind spot outranks a divergence in the
    # STATE without erasing what the other axes showed had to be remembered
    # independently at three returns, and two of them forgot it. Collecting
    # both and deciding once makes it structural instead.
    blind: list[str] = []
    mismatches: list[str] = []

    issue_obs = None
    if item.get("issue"):
        try:
            issue_obs = observe_issue(gh, item["issue"])
        except ObservationFailure as exc:
            # Collected, not returned. Returning here also skipped the probe
            # loop, so an item declaring both an issue and a probe lost its
            # probes to a GitHub hiccup -- or, since validate_state does not
            # check that the reference resolves, to a typo.
            blind.append(f"could not observe {item['issue']}: {exc}")
        except ValueError as exc:
            blind.append(str(exc))
    if issue_obs:
        result.evidence["issue_state"] = issue_obs.state.lower()
        result.evidence["open_prs"] = [p["number"] for p in issue_obs.open_prs]
        try:
            result.evidence["review"] = _review_state(issue_obs.open_prs)
        except ObservationFailure as exc:
            # Recorded either way. An absent key reads as "not applicable" to
            # anyone looking at the snapshot, when the truth is that we looked
            # and could not tell.
            result.evidence["review"] = "unobserved"
            if "review" in expected:
                blind.append(f"could not observe review state: {exc}")
        result.evidence["last_activity"] = issue_obs.last_activity

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
        actual_review = result.evidence["review"]
        # Guarded like the merge axis. Without this the branch reported a
        # divergence on the very axis it had just recorded as unreadable —
        # "review expected clean, actual unobserved" is not an observation,
        # it is the absence of one wearing a finding's clothes.
        if actual_review != "unobserved" and not _satisfies(actual_review, want_review):
            mismatches.append(
                f"review expected {_render_want(want_review)}, actual {actual_review}")

    want_merge = expected.get("merge")
    if want_merge and issue_obs:
        try:
            actual_merge = _merge_summary(issue_obs.open_prs)
        except ObservationFailure as exc:
            blind.append(f"could not observe mergeability: {exc}")
            actual_merge = None
            # Recorded like the review axis, for the same reason: an absent
            # key reads as "not applicable" to anyone looking at the snapshot,
            # when the truth is that we looked and could not tell.
            result.evidence["merge"] = "unobserved"
        if actual_merge is not None:
            result.evidence["merge"] = actual_merge
        # The count is evidence, not identity: blocked-conversations:14 and :9
        # are the same situation and must not churn the report. The rule is
        # deliberately narrow to that one value -- applied across the whole
        # vocabulary it would also let a declared "unknown" be satisfied by
        # "we could not read the field", which is the one match that must
        # never go green.
            if not _satisfies(actual_merge, want_merge):
                mismatches.append(
                    f"merge expected {_render_want(want_merge)}, actual {actual_merge}")

    for probe in item.get("probes") or []:
        try:
            count, description = run_probe(gh, probe)
        except ObservationFailure as exc:
            blind.append(f"probe {probe.get('id', '?')}: {exc}")
            continue
        except (KeyError, re.error) as exc:
            blind.append(f"probe {probe.get('id', '?')} is malformed: {exc}")
            continue
        result.evidence.setdefault("probes", {})[probe.get("id", "?")] = count
        try:
            satisfied = compare(count, probe["expect"])
        except (KeyError, ValueError) as exc:
            # Reading `expect` outside the guard let a malformed probe raise
            # out of reconcile(), exit 1, and be published by the workflow as
            # an ordinary "not aligned" result -- a crash dressed as a finding.
            blind.append(
                f"probe {probe.get('id', '?')} has no usable expectation: {exc}")
            continue
        if not satisfied:
            mismatches.append(
                f"probe {probe.get('id', '?')}: {description} = {count}, "
                f"expected {probe['expect']}")

    # One decision, made once. A blind spot outranks a divergence in the
    # state -- reporting DRIFT on an axis nobody could read would be inventing
    # a finding -- but `reasons` is what every rendering shows, so what the
    # other axes did observe is reported alongside it.
    if blind:
        result.state = OBSERVATION_FAILED
        result.reasons.extend(blind)
        result.reasons.extend(mismatches)
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
        f"State last changed {snapshot['generated_at']} — **{snapshot['overall']}**.",
        "",
        "This page is regenerated only when the reconciled state changes, so the "
        "date above is that change, not the last time anything was checked. The "
        "run history is the record of what ran.",
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


def headline(snapshot: dict) -> str:
    return f"{snapshot['overall']}  " + "  ".join(
        f"{BADGE[st]}={snapshot['counts'][st]}" for st in SEVERITY)


def write_digest(path: pathlib.Path, snapshot: dict, body: str) -> None:
    """The digest, for a later workflow step to read.

    Its own file, because GITHUB_STEP_SUMMARY is per step: the reporting step
    opening it got an empty one, and the tracking issue carried an empty code
    block for five commits without anyone noticing. A named function so the
    write itself can be exercised rather than re-implemented by a test.
    """
    path.write_text(
        f"{headline(snapshot)}\n\n{body}\n" if body else f"{headline(snapshot)}\n")


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
                   unresolved=0, mergeable="MERGEABLE", outdated=0,
                   timeline_truncated=False, threads_truncated=False,
                   reviews_truncated=False):
        def node(n, d, closes):
            threads = ([{"isResolved": False, "isOutdated": False}] * unresolved
                       + [{"isResolved": False, "isOutdated": True}] * outdated)
            return {"source": {
                "number": n, "url": f"u{n}", "state": "OPEN", "isDraft": False,
                "reviewDecision": d, "updatedAt": updated, "headRefOid": "abc123",
                "mergeable": mergeable, "mergeStateStatus": merge_status,
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": threads_truncated},
                    "nodes": threads},
                "reviews": {"pageInfo": {"hasPreviousPage": reviews_truncated},
                            "nodes": [
                    {"state": d, "submittedAt": updated,
                     "commit": {"oid": "abc123" if head_seen else "older99"}}
                ]},
                "closingIssuesReferences": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": (
                        [{"number": 1, "repository": {"nameWithOwner": "o/r"}}]
                        if closes else
                        [{"number": 7, "repository": {"nameWithOwner": "o/other"}}])}}}
        nodes = [node(n, d, True) for n, d in prs]
        nodes += [node(n, "NONE", False) for n in mentions]
        return {"repository": {"issue": {
            "number": 1, "title": "t", "url": "u", "state": state,
            "updatedAt": updated,
            "timelineItems": {
                "pageInfo": {"hasPreviousPage": timeline_truncated},
                "nodes": nodes}}}}

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
            # Deliberately not gated on self._fail: a stub whose GraphQL is
            # down can still have readable files, which is the case the
            # probe-after-issue-failure fixture needs.
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

    # Every finding from the review of #56, as a case.

    # UNKNOWN mergeability is the absence of a merge state, not a value.
    # Reporting it as DRIFT against a declared `merge: ready` would name a
    # state nobody observed.
    r = reconcile_item(StubGH(fake_issue(prs=[(9, "APPROVED")],
                                         mergeable="UNKNOWN", merge_status="UNKNOWN")),
                       dict(item_active, expected={"issue": "open",
                                                   "implementation": "active",
                                                   "review": "clean",
                                                   "merge": "ready"}), {}, now)
    check(r.state == OBSERVATION_FAILED,
          f"uncomputed mergeability must not be a value: {r.state} {r.reasons}")

    # REVIEW_REQUIRED is "nobody has looked", the opposite instruction to
    # "a reviewer wants changes". Every pre-review PR under branch protection
    # sits there, and folding the two together sends the reader to the wrong
    # action.
    r = reconcile_item(StubGH(fake_issue(prs=[(9, "REVIEW_REQUIRED")],
                                         merge_status="BLOCKED", head_seen=False)),
                       dict(item_active, expected={"issue": "open",
                                                   "implementation": "active",
                                                   "review": "unreviewed-head",
                                                   "merge": "blocked-review-required"}),
                       {}, now)
    check(r.state == ALIGNED and r.evidence["merge"] == "blocked-review-required",
          f"unreviewed must not read as changes-requested: {r.state} {r.evidence}")

    # An outdated thread is still unresolved, and GitHub's gate still refuses.
    # Excluding them drove the count to zero on exactly the PRs this branch
    # exists to explain, producing "we cannot say why" when we can.
    r = reconcile_item(StubGH(fake_issue(prs=[(9, "APPROVED")], merge_status="BLOCKED",
                                         unresolved=0, outdated=3)),
                       dict(item_active, expected={"issue": "open",
                                                   "implementation": "active",
                                                   "review": "clean",
                                                   "merge": "blocked-conversations"}),
                       {}, now)
    check(r.evidence["merge"] == "blocked-conversations:3",
          f"outdated-but-unresolved threads must count: {r.evidence.get('merge')}")

    # A truncated connection is a refusal, not a short list.
    r = reconcile_item(StubGH(fake_issue(prs=[(9, "APPROVED")], merge_status="BLOCKED",
                                         unresolved=2, threads_truncated=True)),
                       dict(item_active, expected={"issue": "open",
                                                   "implementation": "active",
                                                   "review": "clean",
                                                   "merge": "blocked-conversations"}),
                       {}, now)
    check(r.state == OBSERVATION_FAILED,
          f"a truncated thread list must not be counted: {r.state}")
    r = reconcile_item(StubGH(fake_issue(prs=[(9, "APPROVED")], timeline_truncated=True)),
                       item_active, {}, now)
    check(r.state == OBSERVATION_FAILED,
          f"a truncated timeline must not be counted: {r.state}")

    # The count is evidence only for the one value that carries it. A declared
    # "unknown" must never be satisfied by "we could not read the field".
    check(_merge_summary([{"raw": {"number": 1, "mergeable": "MERGEABLE",
                                   "mergeStateStatus": "BLOCKED",
                                   "reviewDecision": "APPROVED",
                                   "reviewThreads": {"pageInfo": {},
                                                     "nodes": [{"isResolved": False}] * 9}}}])
          == "blocked-conversations:9", "summary lost the count")

    # A malformed probe is a configuration failure, not a published finding.
    bad_probe = {"id": "b", "title": "B", "phase": "now", "expected": {},
                 "probes": [{"id": "n", "kind": "file_line_match_count",
                             "repo": "o/r", "path": "a.txt", "pattern": "x"}]}
    r = reconcile_item(StubGH(files={"a.txt": "x\n"}), bad_probe, {}, now)
    check(r.state == OBSERVATION_FAILED,
          f"a probe with no expectation must not crash out: {r.state}")

    # validate_state refuses a declaration that asserts less than it looks.
    problems = validate_state({"items": [
        {"id": "a", "issue": "o/r#1", "expected": {"resolver_mode": "declarative"}},
        {"id": "a", "issue": "o/r#2", "expected": {"issue": "open"}},
        {"id": "c", "phase": "someday", "expected": {"issue": "open"}},
        {"id": "d"},
        {"id": "e", "probes": [{"id": "p", "kind": "file_line_match_count",
                                "repo": "o/r", "path": "p", "pattern": "(",
                                "expect": "roughly 3"}]},
    ]})
    joined = " | ".join(problems)
    for needle in ("resolver_mode", "duplicate id", "someday", "asserts nothing",
                   "names no issue", "not a regex", "unparsable expectation"):
        check(needle in joined, f"validate_state missed {needle!r}: {joined}")
    check(validate_state({"items": [
        {"id": "ok", "issue": "o/r#1", "expected": {"issue": "open"}}]}) == [],
        "validate_state rejected a valid item")

    # A declaration may list several acceptable answers: an item under active
    # implementation alternates between blocking-findings and unreviewed-head
    # every time its author pushes, and calling that drift trains people to
    # ignore the report.
    r = reconcile_item(StubGH(fake_issue(prs=[(9, "CHANGES_REQUESTED")], head_seen=False)),
                       dict(item_active, expected={
                           "issue": "open", "implementation": "active",
                           "review": ["blocking-findings", "unreviewed-head"]}),
                       {}, now)
    check(r.state == ALIGNED, f"a list of acceptable answers must align: {r.state} {r.reasons}")

    # But a list does not weaken the one match that must never go green.
    check(not _satisfies("unknown:none", "unknown"),
          "a declared unknown was satisfied by an unreadable field")
    check(_satisfies("blocked-conversations:14", "blocked-conversations"),
          "the count should be evidence, not identity")

    # An item that asserts nothing about merging must not be failed by a
    # transient UNKNOWN on some PR attached to it.
    r = reconcile_item(StubGH(fake_issue(prs=[(9, "CHANGES_REQUESTED")],
                                         mergeable="UNKNOWN", merge_status="UNKNOWN")),
                       dict(item_active, expected={"issue": "open",
                                                   "implementation": "none"}), {}, now)
    check(r.state == DRIFT and any("implementation" in x for x in r.reasons),
          f"an unasserted merge blind spot hid an observed DRIFT: {r.state} {r.reasons}")

    # Liveness is measured against the run history, not the snapshot. The
    # snapshot is committed only on a change, so a quiet roadmap -- the
    # designed steady state -- would have read as a growing outage forever.
    class Args:
        max_staleness = "26h"
        previous_run_at = None

    a = Args()
    check(gap_since_previous_run(a, now) is None,
          "no reference should mean no claim about liveness")
    a.previous_run_at = "2026-09-11T17:00:00Z"
    check(gap_since_previous_run(a, now) is None,
          "a run an hour ago is not an outage")
    a.previous_run_at = "2026-09-09T12:00:00Z"
    check(gap_since_previous_run(a, now) == 54,
          f"a two-day gap should be reported: {gap_since_previous_run(a, now)}")

    # The digest path: empty for five commits without anyone noticing,
    # because nothing exercised it.
    import tempfile as _tempfile
    snap = {"generated_at": "2026-09-11T21:00:00Z", "overall": DRIFT,
            "counts": {OBSERVATION_FAILED: 0, DRIFT: 1, UNEXPECTED_SILENCE: 0,
                       ALIGNED: 1},
            "items": [
                {"id": "a", "title": "A", "phase": "now", "state": DRIFT,
                 "issue": "o/r#1", "reasons": ["review expected clean, actual none"],
                 "evidence": {}, "epic": None, "note": None},
                {"id": "b", "title": "B", "phase": "done", "state": ALIGNED,
                 "issue": None, "reasons": [], "evidence": {}, "epic": None,
                 "note": None}]}
    text = digest(snap)
    check("A" in text and "review expected clean" in text,
          f"digest dropped the finding: {text!r}")
    check("B" not in text, f"digest included an aligned item: {text!r}")
    page = render_markdown(snap)
    check("State last changed" in page,
          "the page must not imply it was regenerated on every check")
    check("## Done" in page and "## Now" in page, "the page lost its phases")
    with _tempfile.TemporaryDirectory() as d:
        out = pathlib.Path(d) / "digest.txt"
        write_digest(out, snap, text)
        written = out.read_text()
    check(written.startswith(DRIFT) and "review expected clean" in written
          and "DRIFT=1" in written,
          f"the digest file lost its content: {written!r}")

    # The aligned case: an empty body must still produce a file with the
    # headline, because the reporting step reads it unconditionally.
    aligned_snap = dict(snap, overall=ALIGNED, items=[snap["items"][1]],
                        counts={OBSERVATION_FAILED: 0, DRIFT: 0,
                                UNEXPECTED_SILENCE: 0, ALIGNED: 1})
    with _tempfile.TemporaryDirectory() as d:
        out = pathlib.Path(d) / "digest.txt"
        write_digest(out, aligned_snap, digest(aligned_snap))
        written = out.read_text()
    check(written == headline(aligned_snap) + "\n",
          f"an aligned digest must be exactly the headline and a newline, "
          f"since the report step cats it under `set -euo pipefail`: {written!r}")

    # An item asserting both an issue and a probe, with the probe unreadable:
    # the state is OBSERVATION_FAILED, and the observed mismatch must still be
    # reported. Nothing exercised this combination for fifteen review rounds,
    # which is how the reasons came to be dropped.
    both_axes = {
        "id": "m", "title": "M", "phase": "now", "issue": "o/r#1",
        "expected": {"issue": "open"},
        "probes": [{"id": "z", "kind": "file_line_match_count", "repo": "o/r",
                    "path": "gone.txt", "pattern": "^x", "expect": "== 0"}],
    }
    r = reconcile_item(StubGH(fake_issue(state="CLOSED"), files={}), both_axes, {}, now)
    check(r.state == OBSERVATION_FAILED,
          f"an unreadable probe must outrank a divergence: {r.state}")
    check(any("issue expected open" in x for x in r.reasons),
          f"the observed mismatch was dropped: {r.reasons}")
    check(any("probe z" in x for x in r.reasons),
          f"the unreadable probe was not reported: {r.reasons}")

    # The other two axes the rule had to be remembered at, and was not.
    # An unreadable merge state alongside an observed issue divergence:
    r = reconcile_item(StubGH(fake_issue(state="CLOSED", prs=[(9, "APPROVED")],
                                         mergeable="UNKNOWN", merge_status="UNKNOWN")),
                       {"id": "mm", "title": "MM", "phase": "now", "issue": "o/r#1",
                        "expected": {"issue": "open", "merge": "ready"}}, {}, now)
    check(r.state == OBSERVATION_FAILED, f"merge blind spot must set the state: {r.state}")
    check(any("issue expected open" in x for x in r.reasons),
          f"the observed divergence was dropped by the merge axis: {r.reasons}")

    # An unreadable review state alongside the same:
    r = reconcile_item(StubGH(fake_issue(state="CLOSED", prs=[(9, "APPROVED")],
                                         reviews_truncated=True)),
                       {"id": "rr", "title": "RR", "phase": "now", "issue": "o/r#1",
                        "expected": {"issue": "open", "review": "clean"}}, {}, now)
    check(r.state == OBSERVATION_FAILED, f"review blind spot must set the state: {r.state}")
    check(any("issue expected open" in x for x in r.reasons),
          f"the observed divergence was dropped by the review axis: {r.reasons}")
    check(not any("review expected" in x for x in r.reasons),
          f"a divergence was reported on the axis that could not be read: {r.reasons}")

    # An item declaring merge AND probes must still have its probes run.
    r = reconcile_item(
        StubGH(fake_issue(prs=[(9, "APPROVED")], mergeable="UNKNOWN",
                          merge_status="UNKNOWN"),
               files={"a.txt": "zones/x\n"}),
        {"id": "mp", "title": "MP", "phase": "now", "issue": "o/r#1",
         "expected": {"merge": "ready"},
         "probes": [{"id": "z", "kind": "file_line_match_count", "repo": "o/r",
                     "path": "a.txt", "pattern": "^zones/", "expect": "== 0"}]},
        {}, now)
    check(any("probe z" in x for x in r.reasons),
          f"an unreadable merge state skipped the probes entirely: {r.reasons}")

    # An unreadable issue must not cost the item its probes.
    r = reconcile_item(
        StubGH(None, files={"a.txt": "zones/x\n"}, fail="gh exited 1: HTTP 502"),
        {"id": "ip", "title": "IP", "phase": "now", "issue": "o/r#1",
         "expected": {"issue": "open"},
         "probes": [{"id": "z", "kind": "file_line_match_count", "repo": "o/r",
                     "path": "a.txt", "pattern": "^zones/", "expect": "== 0"}]},
        {}, now)
    check(r.state == OBSERVATION_FAILED, f"expected OBSERVATION_FAILED, got {r.state}")
    check(any("could not observe o/r#1" in x for x in r.reasons),
          f"the issue failure was not reported: {r.reasons}")
    check(any("probe z" in x for x in r.reasons),
          f"an unreadable issue skipped the probes: {r.reasons}")

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

def gap_since_previous_run(args, now: dt.datetime) -> int | None:
    """Hours since the last successful run, when that exceeds the allowance.

    The reference is the workflow's own run history, supplied by the caller,
    and it must be the time of a run that actually RECONCILED -- not merely a
    run that concluded successfully. A pull-request run of that workflow skips
    the reconcile job, and a skipped job does not fail a run, so such a run
    succeeds having observed nothing; letting one through would reset this
    clock in the middle of an outage. The caller is responsible for that
    filter, which is why this says so rather than "the last successful run".

    Not the committed snapshot either: it is written only when the reconciled
    state changes, so "no change" -- the designed steady state -- would have
    read as a growing outage forever, climbing while nothing was wrong.

    What this can and cannot do is worth being exact about. It reports an
    outage that has ENDED, on the first run after it: a reconciler that is
    still down produces no run and therefore no report. Catching that needs a
    watchdog outside this workflow, which does not exist yet.
    """
    if not args.previous_run_at:
        return None
    previous = dt.datetime.fromisoformat(args.previous_run_at.replace("Z", "+00:00"))
    gap = (now - previous).total_seconds()
    if gap <= parse_duration(args.max_staleness):
        return None
    return int(gap // 3600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="roadmap/roadmap-state.yaml")
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--markdown", dest="md_out")
    parser.add_argument(
        "--digest", dest="digest_out",
        help="write the human-readable digest here. GITHUB_STEP_SUMMARY cannot "
             "serve this: the runtime gives each step its own file, so a later "
             "step reading it gets an empty one.")
    parser.add_argument("--previous", help="prior snapshot.json, to report only on change")
    parser.add_argument(
        "--previous-run-at",
        help="ISO timestamp of the last successful run of this reconciler, "
             "from the workflow's own run history. NOT the snapshot's "
             "generated_at: the snapshot is committed only when the state "
             "changes, so its timestamp records the last change and a quiet "
             "roadmap would look like an outage forever.")
    parser.add_argument(
        "--max-staleness", default="26h",
        help="how long between runs before the gap is itself reported")
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

    try:
        parse_duration(args.max_staleness)
        if args.previous_run_at:
            parsed = dt.datetime.fromisoformat(
                args.previous_run_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # Naive passes fromisoformat and then raises TypeError on the
                # subtraction, several steps away from the flag that caused it.
                raise ValueError(
                    f"--previous-run-at {args.previous_run_at!r} has no timezone")
    except ValueError as exc:
        # Swallowing this would disable the liveness check while leaving the
        # flag in place -- a guard that reports nothing and looks armed.
        print(f"reconcile: {exc}", file=sys.stderr)
        return 2

    problems = validate_state(state)
    if problems:
        for problem in problems:
            print(f"reconcile: {args.state}: {problem}", file=sys.stderr)
        return 2

    # Read the prior snapshot BEFORE anything is written. The workflow points
    # --json and --previous at the same path, so writing first made `before`
    # the file just produced, `changed` permanently false, and the
    # notification step dead -- a reporter that could never report.
    before = None
    if args.previous and pathlib.Path(args.previous).exists():
        try:
            before = json.loads(pathlib.Path(args.previous).read_text())
        except (OSError, json.JSONDecodeError):
            before = None  # unreadable is not evidence of no change

    now = dt.datetime.now(dt.timezone.utc)
    snapshot = reconcile(GitHub(), state, now)

    # Every item unobserved is not a roadmap that went wrong, it is a
    # reconciler that could not look — a token without access to the repos it
    # asserts about, a rate limit, an outage. Publishing an all-red page and
    # exiting 1 would file that under "results", which is the same mistake as
    # a watcher publishing silence.
    blind = (snapshot["counts"][OBSERVATION_FAILED] == len(snapshot["items"])
             and snapshot["items"])
    if blind:
        print("reconcile: every item was unobservable — this is an access or "
              "connectivity failure, not a roadmap state", file=sys.stderr)
        for item in snapshot["items"][:3]:
            for reason in item["reasons"]:
                print(f"reconcile:   {reason}", file=sys.stderr)
        return 2

    if args.json_out:
        pathlib.Path(args.json_out).write_text(json.dumps(snapshot, indent=2) + "\n")
    if args.md_out:
        pathlib.Path(args.md_out).write_text(render_markdown(snapshot))

    changed = before is None or reportable_state(before) != reportable_state(snapshot)

    body = digest(snapshot)
    print(headline(snapshot))
    if body:
        print()
        print(body)

    stale_hours = gap_since_previous_run(args, now)
    if stale_hours is not None:
        gap_line = (f"{BADGE[OBSERVATION_FAILED]} no successful reconcile for "
                    f"{stale_hours}h (allowed {args.max_staleness}) — the schedule "
                    f"may have fired and failed rather than missed a tick; either "
                    f"way the roadmap below was unobserved for that window")
        body = f"{gap_line}\n{body}" if body else gap_line
        print(gap_line)

    if args.digest_out:
        write_digest(pathlib.Path(args.digest_out), snapshot, body)

    if step_summary := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as fh:
            fh.write(f"### Roadmap: {headline(snapshot)}\n\n")
            fh.write(f"```\n{body or 'everything aligned'}\n```\n")
    if github_output := os.getenv("GITHUB_OUTPUT"):
        with open(github_output, "a") as fh:
            fh.write(f"overall={snapshot['overall']}\n")
            fh.write(f"changed={'true' if changed else 'false'}\n")
            fh.write(f"unobserved={snapshot['counts'][OBSERVATION_FAILED]}\n")
            fh.write(f"stale_hours={stale_hours if stale_hours is not None else 0}\n")

    return 0 if snapshot["overall"] == ALIGNED else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # Exit 2, never 1. An unhandled exception means the run did not
        # complete, and exit 1 is the code the workflow publishes as an
        # ordinary "something diverged" result — a crash would otherwise be
        # rendered as a roadmap finding.
        print(f"reconcile: unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
