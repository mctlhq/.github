#!/usr/bin/env python3
"""Observed GitHub issue graph for roadmap reconciliation.

This module loads and validates a `GitHubGraphSnapshot` and normalizes it into a
provider-neutral graph. It performs no network I/O: snapshots are replayed from
disk. Reading live GitHub state is a separate, separately reviewed change,
because that is where credentials and untrusted network responses come in.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from validate import _json_path, issue_key

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_SCHEMA = ROOT / "schemas" / "github-graph-snapshot.schema.json"

IssueKey = tuple[str, int]

# fromisoformat() alone also accepts ISO 8601 forms RFC 3339 does not: a space
# instead of "T", omitted seconds, a date with no time. Shape first, then parse.
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


class SnapshotIncomplete(RuntimeError):
    """The snapshot holds no observation for an issue the manifest binds.

    Absent is not the same as absent-from-GitHub. Reporting "not found" for an
    issue nobody looked at would invent evidence, so this is an error rather
    than a diff entry.
    """


def load_schema(path: Path = DEFAULT_SNAPSHOT_SCHEMA) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return schema


def _timestamp_errors(snapshot: dict[str, Any]) -> list[str]:
    """Check the `format: date-time` fields the JSON Schema only annotates.

    `format` is an annotation, not an assertion, unless a format checker is
    wired in -- and wiring one in would mean a new runtime dependency for
    RFC 3339. A live capture whose `capturedAt` is free text would be evidence
    that claims a time nobody can read, so it is checked here instead.
    """

    errors: list[str] = []

    def check(path: str, value: Any) -> None:
        if not isinstance(value, str):
            return
        if not _RFC3339.match(value):
            errors.append(f"{path}: {value!r} is not an RFC 3339 timestamp")
            return
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{path}: {value!r} is not an RFC 3339 timestamp")
            return
        if parsed.tzinfo is None:
            errors.append(f"{path}: {value!r} has no timezone offset")

    source = snapshot.get("source")
    if isinstance(source, dict):
        check("$.source.capturedAt", source.get("capturedAt"))
    for index, observation in enumerate(snapshot.get("issues", []) or []):
        if isinstance(observation, dict):
            check(f"$.issues[{index}].updatedAt", observation.get("updatedAt"))
    return errors


def _duplicate_request_errors(snapshot: dict[str, Any]) -> list[str]:
    """One canonical issue may be observed at most once.

    Two observations of the same request make normalization order-dependent:
    whichever lands last decides the resolved identity, and a found/missing
    pair leaves the issue in two states at once. That is an unusable input, not
    a graph to diff.
    """

    seen: set[tuple[str, int]] = set()
    duplicates: set[tuple[str, int]] = set()
    for observation in snapshot.get("issues", []) or []:
        if not isinstance(observation, dict):
            continue
        key = issue_key(observation.get("requested"))
        if key is None:
            continue
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return [
        f"$.issues: {repository}#{number} is observed more than once"
        for repository, number in sorted(duplicates)
    ]


def snapshot_errors(
    snapshot: dict[str, Any], schema: dict[str, Any] | None = None
) -> list[str]:
    validator = Draft202012Validator(schema if schema is not None else load_schema())
    failures = sorted(
        validator.iter_errors(snapshot),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    errors = [
        f"{_json_path(error.absolute_path)}: {error.message}" for error in failures
    ]
    if errors:
        return errors
    return _timestamp_errors(snapshot) + _duplicate_request_errors(snapshot)


def load_snapshot(path: Path, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        snapshot = json.load(handle)
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot root must be an object")
    errors = snapshot_errors(snapshot, schema)
    if errors:
        raise ValueError("; ".join(errors))
    return snapshot


@dataclass(frozen=True)
class ObservedGraph:
    """Provider-neutral normalized view of one snapshot.

    Keys are canonical `(repository, number)` pairs. `resolution` maps the
    identity that was asked for onto the identity GitHub answered with, which
    differ exactly when an issue was transferred.
    """

    resolution: dict[IssueKey, IssueKey]
    missing: frozenset[IssueKey]
    observed: frozenset[IssueKey]
    parents: dict[IssueKey, tuple[IssueKey, ...]]
    children: dict[IssueKey, tuple[IssueKey, ...]]
    blocked_by: frozenset[tuple[IssueKey, IssueKey]]

    def resolve(self, key: IssueKey) -> IssueKey | None:
        return self.resolution.get(key)

    def parent_of(self, key: IssueKey) -> tuple[IssueKey, ...]:
        return self.parents.get(key, ())


def _ref_key(ref: Any) -> IssueKey:
    key = issue_key(ref)
    if key is None:
        raise ValueError(f"not an issue ref: {ref!r}")
    return key


def observed_graph(snapshot: dict[str, Any]) -> ObservedGraph:
    """Normalize a schema-valid snapshot into a comparable graph."""

    resolution: dict[IssueKey, IssueKey] = {}
    missing: set[IssueKey] = set()
    observed: set[IssueKey] = set()
    parent_edges: set[tuple[IssueKey, IssueKey]] = set()
    blocked_by: set[tuple[IssueKey, IssueKey]] = set()

    for observation in snapshot.get("issues", []):
        requested = _ref_key(observation["requested"])
        if not observation.get("found", False):
            missing.add(requested)
            continue

        resolved = _ref_key(observation["resolved"])
        resolution[requested] = resolved
        observed.add(resolved)

        parent = observation.get("parent")
        if parent is not None:
            parent_key = _ref_key(parent)
            if parent_key != resolved:
                parent_edges.add((resolved, parent_key))

        for child in observation.get("subIssues", []):
            child_key = _ref_key(child)
            if child_key != resolved:
                parent_edges.add((child_key, resolved))

        for blocker in observation.get("blockedBy", []):
            blocker_key = _ref_key(blocker)
            if blocker_key != resolved:
                blocked_by.add((resolved, blocker_key))

    parents: dict[IssueKey, set[IssueKey]] = {}
    children: dict[IssueKey, set[IssueKey]] = {}
    for child_key, parent_key in parent_edges:
        parents.setdefault(child_key, set()).add(parent_key)
        children.setdefault(parent_key, set()).add(child_key)

    return ObservedGraph(
        resolution=resolution,
        missing=frozenset(missing),
        observed=frozenset(observed),
        parents={key: tuple(sorted(value)) for key, value in sorted(parents.items())},
        children={key: tuple(sorted(value)) for key, value in sorted(children.items())},
        blocked_by=frozenset(blocked_by),
    )


def require_observations(snapshot: dict[str, Any], keys: Iterable[IssueKey]) -> None:
    """Fail loudly when the snapshot never looked at an issue we must compare."""

    seen = {_ref_key(item["requested"]) for item in snapshot.get("issues", [])}
    absent = sorted(set(keys) - seen)
    if absent:
        rendered = ", ".join(f"{repository}#{number}" for repository, number in absent)
        raise SnapshotIncomplete(f"snapshot holds no observation for: {rendered}")


class FixtureGraphSource:
    """Replays a captured or synthetic snapshot. Performs no I/O beyond the file."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot

    @classmethod
    def from_path(
        cls, path: Path, schema: dict[str, Any] | None = None
    ) -> "FixtureGraphSource":
        return cls(load_snapshot(path, schema))

    def snapshot(self, keys: Sequence[IssueKey]) -> dict[str, Any]:
        # A source built from a dict never went through load_snapshot(), so it
        # is validated here, before require_observations() indexes into it.
        errors = snapshot_errors(self._snapshot)
        if errors:
            raise ValueError("snapshot is invalid: " + "; ".join(errors))
        require_observations(self._snapshot, keys)
        return self._snapshot
