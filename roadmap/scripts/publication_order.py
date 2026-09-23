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

Standard library only: the publish job that runs `newer` holds the only write
token of the workflow and installs no package.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PUBLICATION_FILE = "publication.json"

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
    args = parser.parse_args(argv)

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
