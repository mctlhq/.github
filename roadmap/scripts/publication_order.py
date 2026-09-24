#!/usr/bin/env python3
"""Two questions the publisher asks about `roadmap-state`, answered offline.

`newer CURRENT CANDIDATE` -- may CANDIDATE replace CURRENT? Only if its
observation is strictly newer. The workflow serializes its own runs, but a
serialized queue is ordered by when runs started, and a manual or dispatched
run can sit in it holding a capture that another run has already overtaken.
Pushing that capture would move `capturedAt` backwards, and every consumer
would then read a graph older than one it has already been shown. The push
itself refuses to force, which stops a lost race; this stops a won race with
an older answer. Exit 0: replace. Exit 1: overtaken, nothing is pushed -- the
newer publication already says more than this one could. Exit 3: CANDIDATE is
not a live publication at all, which is a failed run.

`fresh STATE --revision SHA (--max-age SECONDS | --observed-after TIME)` --
does this run have nothing to add? Only when the published source revision is
SHA (the publisher's inputs have not moved) and the observation is either at
most SECONDS old (the scheduled reconciliation) or started strictly after TIME
(a pushed or dispatched run, where TIME is when the run was created: whatever
asked for it happened before that, so a capture that started later already
saw it). Exit 0: fresh, skip. Exit 1: stale or unreadable, capture.

`decide STATE --event EVENT [--run-created TIME] [--repo DIR]` is what the
workflow runs: it works out which revision to judge freshness against and
picks the bound from the event. The publisher's inputs are `INPUT_PATHS`; when
they are byte-equal at HEAD and at the published source revision, a commit
elsewhere on main (a profile, another workflow) has changed nothing that is
published, and freshness is judged against the published revision instead of
HEAD. Same exit codes as `fresh`.

`budget --event E --now T --start T [--limit L --remaining R --reset T]
--cost C --failures N` is one step of the build job's wait for API budget. It
prints exactly one action -- `capture`, `skip`, `fail` or `sleep SECONDS` --
so the shell only calls `gh api` and sleeps. The deadline is --start plus
BUDGET_WAIT_SECONDS, so the window is defined once. Omitting the three budget
fields means the budget could not be read, and --failures counts consecutive
unreadable reads. See `budget_step`.

Standard library only: the publish job that runs `newer` holds the only write
token of the workflow and installs no package.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PUBLICATION_FILE = "publication.json"

# Everything a publication is derived from: the manifests and evaluator under
# roadmap/, and the workflow that runs them. Mirrors the push trigger's paths.
INPUT_PATHS = ("roadmap", ".github/workflows/roadmap-publish.yml")

# The scheduled reconciliation's bound on a publication's age.
SCHEDULE_MAX_AGE_SECONDS = 5400

EXIT_YES = 0
EXIT_NO = 1
EXIT_USAGE = 2
EXIT_INVALID = 3


class Unreadable(ValueError):
    """The directory holds no publication whose observation time can be read."""


def captured_at(directory: Path) -> datetime:
    path = directory / PUBLICATION_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Unreadable(f"{path}: {exc}") from exc
    observation = document.get("observation") if isinstance(document, dict) else None
    value = observation.get("capturedAt") if isinstance(observation, dict) else None
    # A synthetic observation has no capturedAt and is never fresh, so it can
    # neither be the newer publication nor make a skip look safe.
    if not isinstance(value, str) or not value.endswith("Z"):
        raise Unreadable(f"{path}: observation.capturedAt is missing or not UTC")
    try:
        moment = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise Unreadable(f"{path}: capturedAt {value!r}: {exc}") from exc
    return moment


def source_revision(directory: Path) -> str | None:
    path = directory / PUBLICATION_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    source = document.get("source") if isinstance(document, dict) else None
    revision = source.get("revision") if isinstance(source, dict) else None
    return revision if isinstance(revision, str) else None


def may_replace(current: Path, candidate: Path) -> tuple[bool, str]:
    """True when CANDIDATE is strictly newer than CURRENT, or CURRENT is empty."""

    new = captured_at(candidate)
    if not (current / PUBLICATION_FILE).exists():
        return True, "no publication yet"
    try:
        old = captured_at(current)
    except Unreadable as exc:
        # The branch holds something that is not a live publication. A live
        # capture is strictly better evidence than that, so it may replace it.
        return True, f"current publication is unreadable ({exc})"
    if new > old:
        return True, f"{_z(new)} is newer than {_z(old)}"
    return False, f"{_z(new)} is not newer than the published {_z(old)}"


def is_fresh(
    state: Path, revision: str, max_age_seconds: int, now: datetime
) -> tuple[bool, str]:
    published = source_revision(state)
    if published != revision:
        return False, f"published source {published} is not {revision}"
    try:
        moment = captured_at(state)
    except Unreadable as exc:
        return False, str(exc)
    age = (now - moment).total_seconds()
    # A capture from the future means a clock is wrong somewhere; that is not
    # evidence of freshness.
    if age < 0:
        return False, f"capturedAt {_z(moment)} is in the future"
    if age > max_age_seconds:
        return False, f"{int(age)}s old; the reconciliation bound is {max_age_seconds}s"
    return True, f"{int(age)}s old at {revision[:12]}"


def observed_after(state: Path, revision: str, after: datetime) -> tuple[bool, str]:
    published = source_revision(state)
    if published != revision:
        return False, f"published source {published} is not {revision}"
    try:
        moment = captured_at(state)
    except Unreadable as exc:
        return False, str(exc)
    # Strictly after: capturedAt is floored to the second, so a capture stamped
    # in the same second as TIME may have started before the change it is
    # being asked to show.
    if moment > after:
        return True, f"captured {_z(moment)}, after this run was requested at {_z(after)}"
    return False, f"captured {_z(moment)}, not after this run was requested at {_z(after)}"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def judged_revision(repo: Path, published: str | None) -> tuple[str, str]:
    """The revision freshness is judged against, and why.

    HEAD, unless the published revision is known here and every input path is
    byte-equal between it and HEAD -- then the published revision, because
    nothing that is published has changed since it.
    """

    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise Unreadable(f"{repo}: not a git checkout: {head.stderr.strip()}")
    head_revision = head.stdout.strip()
    if not published:
        return head_revision, "no published revision"
    if _git(repo, "cat-file", "-e", f"{published}^{{commit}}").returncode != 0:
        return head_revision, f"published {published[:12]} is not in this checkout"
    diff = _git(repo, "diff", "--quiet", published, head_revision, "--", *INPUT_PATHS)
    if diff.returncode == 0:
        return published, f"inputs at HEAD are those of the published {published[:12]}"
    if diff.returncode == 1:
        return head_revision, f"inputs changed since the published {published[:12]}"
    # Anything else is git failing, not an answer; capturing is the safe side.
    return head_revision, f"could not compare with {published[:12]}: {diff.stderr.strip()}"


def decide(
    state: Path,
    repo: Path,
    event: str,
    run_created: datetime | None,
    now: datetime,
) -> tuple[bool, str]:
    """True when this run can skip its capture."""

    revision, why = judged_revision(repo, source_revision(state))
    if event == "schedule":
        fresh, reason = is_fresh(state, revision, SCHEDULE_MAX_AGE_SECONDS, now)
    else:
        if run_created is None:
            raise ValueError(f"a {event} run needs --run-created")
        fresh, reason = observed_after(state, revision, run_created)
    return fresh, f"{why}; {reason}"


# How long a pushed or dispatched run may wait for budget, how often it may
# poll /rate_limit, and how many unreadable reads it tolerates.
BUDGET_WAIT_SECONDS = 75 * 60
BUDGET_POLL_FLOOR_SECONDS = 60
BUDGET_READ_ATTEMPTS = 3


def budget_step(
    *,
    event: str,
    now: int,
    deadline: int,
    cost: int,
    failures: int,
    limit: int | None,
    remaining: int | None,
    reset: int | None,
) -> tuple[str, int, str]:
    """One decision of the budget wait: (action, seconds, message).

    action is `capture` (enough budget), `skip` (publish nothing, exit 0 with a
    warning or notice in `message`), `fail` (no wait can help) or `sleep`
    (wait `seconds`, then read the budget again). Invariant, pinned by a test
    over every reset offset: a sleep never ends after `deadline`, so a waiting
    run always ends by its own decision, never by the job timeout.
    """

    readable = None not in (limit, remaining, reset)
    if not readable:
        if event == "schedule" or failures >= BUDGET_READ_ATTEMPTS or now >= deadline:
            return "skip", 0, (
                f"::warning::could not read the API budget ({failures} attempt(s)); "
                "not captured. roadmap-state keeps its older capturedAt."
            )
        return "sleep", max(1, min(BUDGET_POLL_FLOOR_SECONDS, deadline - now)), (
            "::notice::could not read the API budget; retrying"
        )
    assert limit is not None and remaining is not None and reset is not None
    if remaining >= cost:
        return "capture", 0, f"capture cost {cost} GETs; core budget {remaining}/{limit}"
    if limit < cost:
        return "fail", 0, (
            f"::error::a capture needs {cost} GETs but the whole hourly budget is "
            f"{limit}; no wait can fix this"
        )
    if event == "schedule":
        return "skip", 0, (
            f"::notice::{remaining}/{limit} GETs left, a capture needs {cost}; "
            "the next scheduled run captures instead"
        )
    if now >= deadline:
        return "skip", 0, (
            f"::warning::{remaining}/{limit} GETs left after waiting, a capture needs "
            f"{cost}; not captured. roadmap-state keeps its older capturedAt; the next "
            "scheduled run that finds budget captures."
        )
    wait = max(reset - now + 5, BUDGET_POLL_FLOOR_SECONDS)
    wait = max(1, min(wait, deadline - now))
    return "sleep", wait, (
        f"::notice::{remaining}/{limit} GETs left, a capture needs {cost}; "
        f"waiting {wait}s for the reset"
    )


def _parse_utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError(f"{value!r} is not a UTC timestamp")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    newer = sub.add_parser("newer", help="may CANDIDATE replace CURRENT?")
    newer.add_argument("current")
    newer.add_argument("candidate")
    fresh = sub.add_parser("fresh", help="is STATE fresh enough to skip a scheduled run?")
    fresh.add_argument("state")
    fresh.add_argument("--revision", required=True)
    bound = fresh.add_mutually_exclusive_group(required=True)
    bound.add_argument("--max-age", type=int, help="seconds")
    bound.add_argument("--observed-after", help="UTC timestamp, e.g. the run's created_at")
    choose = sub.add_parser("decide", help="may this workflow run skip its capture?")
    choose.add_argument("state")
    choose.add_argument("--event", required=True)
    choose.add_argument("--run-created", help="UTC created_at of this run (not needed for schedule)")
    choose.add_argument("--repo", default=".")
    step = sub.add_parser("budget", help="one step of the wait for API budget")
    step.add_argument("--event", required=True)
    step.add_argument("--now", type=int, required=True)
    step.add_argument("--start", type=int, required=True, help="epoch seconds the wait began")
    step.add_argument("--cost", type=int, required=True)
    step.add_argument("--failures", type=int, default=0)
    # Strings, not ints: a truncated or garbled /rate_limit answer is an
    # unreadable budget, not a usage error that fails the step.
    step.add_argument("--limit")
    step.add_argument("--remaining")
    step.add_argument("--reset")
    args = parser.parse_args(argv)

    if args.command == "budget":
        def _int(value: str | None) -> int | None:
            return int(value) if value is not None and value.isdigit() else None

        action, seconds, message = budget_step(
            event=args.event, now=args.now, deadline=args.start + BUDGET_WAIT_SECONDS,
            cost=args.cost,
            failures=args.failures, limit=_int(args.limit),
            remaining=_int(args.remaining), reset=_int(args.reset),
        )
        if message:
            print(message, file=sys.stderr)
        print(f"sleep {seconds}" if action == "sleep" else action)
        return EXIT_YES

    if args.command == "decide":
        try:
            created = _parse_utc(args.run_created) if args.run_created else None
            skip, why = decide(
                Path(args.state), Path(args.repo), args.event, created,
                datetime.now(timezone.utc),
            )
        except (Unreadable, ValueError) as exc:
            print(f"capture: {exc}", file=sys.stderr)
            return EXIT_NO
        print(("skip: " if skip else "capture: ") + why, file=sys.stderr)
        return EXIT_YES if skip else EXIT_NO

    if args.command == "newer":
        try:
            ok, why = may_replace(Path(args.current), Path(args.candidate))
        except Unreadable as exc:
            print(f"REFUSED: candidate is not a live publication: {exc}", file=sys.stderr)
            return EXIT_INVALID
        print(("replace: " if ok else "overtaken: ") + why, file=sys.stderr)
        return EXIT_YES if ok else EXIT_NO

    if args.observed_after is not None:
        try:
            after = _parse_utc(args.observed_after)
        except ValueError as exc:
            print(f"--observed-after: {exc}", file=sys.stderr)
            return EXIT_USAGE
        ok, why = observed_after(Path(args.state), args.revision, after)
    elif args.max_age <= 0:
        print("--max-age must be positive", file=sys.stderr)
        return EXIT_USAGE
    else:
        ok, why = is_fresh(
            Path(args.state), args.revision, args.max_age, datetime.now(timezone.utc)
        )
    print(("fresh: " if ok else "stale: ") + why, file=sys.stderr)
    return EXIT_YES if ok else EXIT_NO


if __name__ == "__main__":
    raise SystemExit(main())
