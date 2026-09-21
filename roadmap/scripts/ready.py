#!/usr/bin/env python3
"""Derive a dependency-aware RoadmapReadySet from an EpicDefinition.

`completion.py` answers whether a work item's own bound issue is delivered.
It deliberately does not look at that item's `dependsOn` / `externalDependsOn`
predecessors, so `completion.blocking` is "required and not complete", not a
ready queue. This module joins the authored dependency edges with
`completion.item_status` to answer a different question: which bound work
items are executable right now.

A work item's readiness is one of four states:

    complete  the item's own bound issue is delivered
    ready     the item is itself incomplete, and every authored predecessor
              (dependsOn and externalDependsOn) is complete
    blocked   the item is itself incomplete, and non-readiness is evidenced --
              either a predecessor was observed incomplete, or the item's own
              issue is closed as not_planned/duplicate
    unknown   the item's own readiness could not be proven -- because the item
              itself is unbound/unobserved/ambiguous/not-found, or because a
              predecessor is

`ready` is narrower than "own issue not complete": only an item whose own
issue is `open` can be executable. `closed_not_planned` and `closed_duplicate`
are evidence of undelivered work for a *dependent*, but for the item itself
they mean GitHub has already retired it -- handing one to a wave launcher
would launch work against a closed issue.

`blocked` outranks `unknown` when both apply to the same item: non-readiness
is already proven, the same certainty-first precedence `completion.compute`
uses when it prefers `incomplete` over `unknown`.

Pure: `compute()` takes no clock, no workflow runtime state and performs no
I/O. Only `assess()` reads a snapshot, through the source it is given, and
only `main()` touches the filesystem or network.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

import completion
import github_graph
import health
import reconcile
import validate
from github_graph import (
    FixtureGraphSource,
    IssueKey,
    ObservationError,
    ObservedGraph,
    SnapshotIncomplete,
    observed_graph,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_READY_SCHEMA = ROOT / "schemas" / "roadmap-ready-set.schema.json"

API_VERSION = "roadmap.mctl.ai/v1alpha1"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INVALID = 3
EXIT_OBSERVATION_FAILED = 4

READY = "ready"
BLOCKED = "blocked"
COMPLETE = "complete"
UNKNOWN = "unknown"
STATES: tuple[str, ...] = (READY, BLOCKED, COMPLETE, UNKNOWN)

# Reasons that are observed evidence of undelivered work -- the only ones that
# make a *dependent* item BLOCKED. Every other non-complete reason (unbound,
# issue_not_found, unobserved, state_not_observed, binding_ambiguous,
# closed_reason_unrecognized) is indeterminate: it says readiness cannot be
# proven, not that the predecessor is definitely still open.
BLOCKING_REASONS = frozenset({"open", "closed_not_planned", "closed_duplicate"})

# The only own reason an executable item can carry, and its complement: the
# reasons that mean GitHub has already retired the item. Both block a
# *dependent*, which is why BLOCKING_REASONS alone cannot decide readiness --
# but only `open` leaves the item itself executable.
#
# Spelled out rather than derived by subtraction from BLOCKING_REASONS: a
# future blocking reason must be an explicit decision about which side it
# falls on, not a silent default into "retired". `test_ready` asserts the two
# sets partition BLOCKING_REASONS exactly, so adding one to that set alone
# fails loudly.
OPEN = "open"
RETIRED_REASONS = frozenset({"closed_not_planned", "closed_duplicate"})

# A predecessor's (or an item's own) contribution to readiness.
_SATISFIED = "satisfied"
_BLOCKING = "blocking"
_INDETERMINATE = "indeterminate"


def _classify(status: str, reason: str) -> str:
    if status == completion.COMPLETE:
        return _SATISFIED
    if status == completion.INCOMPLETE and reason in BLOCKING_REASONS:
        return _BLOCKING
    return _INDETERMINATE


def _blocker_sort_key(blocker: dict[str, Any]) -> tuple[Any, ...]:
    if blocker["kind"] == "workItem":
        return ("workItem", blocker["id"])
    issue = blocker["issue"]
    return ("external", issue["repository"], issue["number"])


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    return {state: sum(1 for item in items if item["state"] == state) for state in STATES}


def compute(
    document: dict[str, Any],
    observed: ObservedGraph,
    unobserved: frozenset[IssueKey] = frozenset(),
) -> dict[str, Any]:
    """Compute the readiness projection for one validated EpicDefinition.

    Delegates every "is this delivered" decision to `completion.item_status`,
    the single source of the completion axis; this module adds no second
    interpretation of GitHub state. Only one hop over the authored graph is
    evaluated: `validate.semantic_errors` already rejects dependency cycles,
    and a `complete` predecessor's own predecessors say nothing about this
    item.

    Returns `{"ready": [...], "summary": {...}, "items": [...]}` -- the parts
    of a RoadmapReadySet that do not need epic identity or source provenance,
    so `assess()` can wrap it without this function ever touching either.
    """

    spec = document["spec"]
    work_items = spec["workItems"]
    ambiguous = completion.colliding_bindings(work_items, observed, unobserved)

    # Every item's own completion axis, computed once up front: a predecessor
    # reference only ever needs this item's own status, never its blockers.
    own: dict[str, tuple[IssueKey | None, str, str]] = {}
    for item in work_items:
        key = validate.issue_key(item.get("issue"))
        status, reason = completion.item_status(key, observed, unobserved, ambiguous)
        own[item["id"]] = (key, status, reason)

    items: list[dict[str, Any]] = []
    for item in sorted(work_items, key=lambda entry: entry["id"]):
        item_id = item["id"]
        key, status, reason = own[item_id]
        own_class = _classify(status, reason)

        depends_on: list[str] = list(item.get("dependsOn", []))
        external_depends_on: list[dict[str, Any]] = list(item.get("externalDependsOn", []))

        blockers: list[dict[str, Any]] = []
        predecessor_classes: set[str] = set()

        for target_id in depends_on:
            _, target_status, target_reason = own[target_id]
            target_class = _classify(target_status, target_reason)
            predecessor_classes.add(target_class)
            if target_class != _SATISFIED:
                blockers.append(
                    {
                        "kind": "workItem",
                        "id": target_id,
                        "status": target_status,
                        "reason": target_reason,
                    }
                )

        for external in external_depends_on:
            external_key = validate.issue_key(external)
            external_status, external_reason = completion.item_status(
                external_key, observed, unobserved, ambiguous
            )
            external_class = _classify(external_status, external_reason)
            predecessor_classes.add(external_class)
            if external_class != _SATISFIED:
                blockers.append(
                    {
                        "kind": "external",
                        "issue": {
                            "repository": external_key[0],
                            "number": external_key[1],
                        },
                        "status": external_status,
                        "reason": external_reason,
                    }
                )

        if own_class == _SATISFIED:
            state = COMPLETE
            blockers = []
        elif own_class == _INDETERMINATE:
            # The item itself is unbound / unobserved / ambiguous / not-found /
            # closed for an unrecognised reason. The reason lives on its own
            # `completion` block; no predecessor is blamed for this item's own
            # unprovable state.
            state = UNKNOWN
            blockers = []
        elif reason in RETIRED_REASONS:
            # The item's own issue is closed as not_planned / duplicate. That is
            # observed evidence, not an unprovable state, so it is not `unknown`
            # -- but it is the item itself that is retired, so no predecessor is
            # to blame and `blockers` would otherwise be empty or list only
            # predecessors that are perfectly fine. It names itself instead.
            state = BLOCKED
            blockers.append(
                {
                    "kind": "workItem",
                    "id": item_id,
                    "status": status,
                    "reason": reason,
                }
            )
        elif _BLOCKING in predecessor_classes:
            # Non-readiness is already proven, even if another predecessor is
            # also indeterminate -- the same certainty-first precedence
            # completion.compute() uses for incomplete over unknown.
            state = BLOCKED
        elif _INDETERMINATE in predecessor_classes:
            state = UNKNOWN
        else:
            state = READY

        entry: dict[str, Any] = {
            "id": item_id,
            "phase": item["phase"],
            "required": bool(item["required"]),
            "state": state,
            "completion": {"status": status, "reason": reason},
            "dependsOn": depends_on,
            "externalDependsOn": [
                {"repository": ref["repository"], "number": ref["number"]}
                for ref in external_depends_on
            ],
            "blockers": sorted(blockers, key=_blocker_sort_key),
        }
        if key is not None:
            entry["issue"] = {"repository": key[0], "number": key[1]}
        items.append(entry)

    ready_ids = sorted(item["id"] for item in items if item["state"] == READY)
    summary = {
        "items": _counts(items),
        "required": _counts([item for item in items if item["required"]]),
    }
    return {"ready": ready_ids, "summary": summary, "items": items}


def consistency_errors(document: dict[str, Any]) -> list[str]:
    """Semantic checks a RoadmapReadySet must pass beyond its JSON Schema.

    A consumer that did not compute a ready set itself should run this on it,
    the same way `completion.consistency_errors` is run on a completion block.
    """

    errors: list[str] = []
    items = document.get("items", [])
    ids = {item.get("id") for item in items}

    for item in items:
        item_id = item.get("id")
        state = item.get("state")
        blockers = item.get("blockers", [])
        own = item.get("completion", {})
        own_status = own.get("status")
        own_reason = own.get("reason")

        if state == READY:
            if blockers:
                errors.append(f"{item_id}: state ready carries blockers {blockers!r}")
            if own_reason != OPEN:
                errors.append(
                    f"{item_id}: state ready has own reason {own_reason!r}, not {OPEN!r} "
                    "-- only an item whose own issue is open is executable"
                )
        elif state == COMPLETE:
            if blockers:
                errors.append(f"{item_id}: state complete carries blockers {blockers!r}")
            if own_status != completion.COMPLETE:
                errors.append(f"{item_id}: state complete has own status {own_status!r}")
        elif state == BLOCKED:
            if not any(blocker.get("reason") in BLOCKING_REASONS for blocker in blockers):
                errors.append(
                    f"{item_id}: state blocked has no blocker whose reason is in "
                    f"{sorted(BLOCKING_REASONS)}"
                )
        elif state == UNKNOWN:
            own_indeterminate = own_status == completion.UNKNOWN or own_reason in (
                "unbound",
                "issue_not_found",
            )
            blocker_indeterminate = any(
                blocker.get("reason") not in BLOCKING_REASONS for blocker in blockers
            )
            if not own_indeterminate and not blocker_indeterminate:
                errors.append(
                    f"{item_id}: state unknown has neither an indeterminate own reason "
                    "nor an indeterminate blocker"
                )
        else:
            errors.append(f"{item_id}: unrecognised state {state!r}")

        for blocker in blockers:
            if blocker.get("kind") != "workItem":
                continue
            if blocker.get("id") not in ids:
                errors.append(
                    f"{item_id}: blocker names work item {blocker.get('id')!r}, "
                    "not part of this manifest"
                )
            elif blocker.get("id") == item_id and blocker.get("reason") not in RETIRED_REASONS:
                # An item is its own blocker in exactly one case: its own issue
                # was retired. Any other self-reference is a dependency cycle
                # that validate.py should already have rejected.
                errors.append(
                    f"{item_id}: names itself as a blocker with reason "
                    f"{blocker.get('reason')!r}, not one of {sorted(RETIRED_REASONS)}"
                )

    expected_ready = sorted(item["id"] for item in items if item.get("state") == READY)
    if document.get("ready") != expected_ready:
        errors.append(f"ready {document.get('ready')!r} != sorted ready ids {expected_ready!r}")

    expected_summary = {
        "items": _counts(items),
        "required": _counts([item for item in items if item.get("required")]),
    }
    if document.get("summary") != expected_summary:
        errors.append(f"summary {document.get('summary')!r} != derived {expected_summary!r}")

    return errors


def render(documents: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """One manifest yields a bare RoadmapReadySet; several yield a List, never a bare array."""

    ordered = sorted(documents, key=lambda item: item["epic"]["manifest"]["path"])
    if len(ordered) == 1:
        return ordered[0]
    return {"apiVersion": API_VERSION, "kind": "RoadmapReadySetList", "items": ordered}


def _load_ready_schema(path: Path = DEFAULT_READY_SCHEMA) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return schema


def assess(
    path: Path,
    loaded: reconcile.LoadedManifest,
    source_adapter: Any,
    *,
    corpus: Path | None = None,
    ready_schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], frozenset[IssueKey]]:
    """Observe one validated manifest through `source_adapter` and compute its ready set.

    Raises `ObservationError`, `SnapshotIncomplete`, `ValueError` or `OSError`
    when no snapshot could be obtained, read or validated at all -- a ready
    set with no `source` provenance would be a claim about a graph nobody
    looked at, so the caller must not emit a document for that case.

    A snapshot that WAS obtained but did not observe every authored identity
    still returns a document: the affected items are `unknown`, never
    silently absent. The second element of the return tuple is the set of
    authored keys the snapshot never observed, so the caller can still exit
    non-zero without discarding the ready set it did manage to compute.
    """

    desired = reconcile.desired_graph(loaded.document)
    keys = desired.authored_keys()

    if isinstance(source_adapter, FixtureGraphSource):
        snapshot = source_adapter.snapshot(keys, require_complete=False)
    else:
        snapshot = source_adapter.snapshot(keys)

    errors = github_graph.snapshot_errors(snapshot)
    if errors:
        raise ValueError("snapshot is invalid: " + "; ".join(errors))

    observed_keys = {
        github_graph._ref_key(item["requested"]) for item in snapshot.get("issues", [])
    }
    unobserved = frozenset(key for key in keys if key not in observed_keys)
    graph = observed_graph(snapshot)

    epic = health._epic(path, loaded, corpus)
    projection = compute(loaded.document, graph, unobserved)
    result: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "kind": "RoadmapReadySet",
        "epic": epic,
        "source": dict(snapshot["source"]),
        "ready": projection["ready"],
        "summary": projection["summary"],
        "items": projection["items"],
    }

    schema = ready_schema if ready_schema is not None else _load_ready_schema()
    schema_failures = validate.schema_errors(result, schema)
    if schema_failures:
        raise ValueError("ready set failed schema validation: " + "; ".join(schema_failures))
    semantic_failures = consistency_errors(result)
    if semantic_failures:
        raise ValueError("ready set failed consistency check: " + "; ".join(semantic_failures))

    return result, unobserved


# --------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "manifests",
        nargs="*",
        help="manifests to assess (default: every manifest in the corpus)",
    )
    parser.add_argument(
        "--corpus",
        default=str(reconcile.DEFAULT_CORPUS),
        help="canonical corpus root, always validated in full (default: roadmap/epics)",
    )
    parser.add_argument("--schema", default=str(validate.DEFAULT_SCHEMA))
    parser.add_argument("--ready-schema", default=str(DEFAULT_READY_SCHEMA))
    parser.add_argument("--snapshot", help="replay a captured or synthetic snapshot")
    parser.add_argument("--live", action="store_true", help="read live GitHub state (GET only)")
    parser.add_argument("--capture", help="live mode; also write the snapshot here")
    parser.add_argument("--api-base", default=github_graph.DEFAULT_API_BASE)
    parser.add_argument("--output", help="write the ready set here instead of stdout")
    args = parser.parse_args(argv)

    if args.capture:
        args.live = True
    if bool(args.snapshot) == bool(args.live):
        print(
            "exactly one of --snapshot or --live/--capture is required",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        schema = validate._load_schema(Path(args.schema))
        ready_schema = _load_ready_schema(Path(args.ready_schema))
        validation = reconcile.validate_corpus(Path(args.corpus), schema)
    except (reconcile.ReconcileError, OSError, ValueError, json.JSONDecodeError, SchemaError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if validation.failures:
        rendered = "; ".join(
            f"{path}: {message}"
            for path in sorted(validation.failures)
            for message in validation.failures[path]
        )
        print(f"ERROR: corpus validation failed: {rendered}", file=sys.stderr)
        return EXIT_INVALID

    known = set(validation.manifests)
    if args.manifests:
        selected = []
        for raw in args.manifests:
            path = Path(raw).resolve()
            if path not in known:
                print(f"ERROR: {raw} is not part of the corpus at {args.corpus}", file=sys.stderr)
                return EXIT_USAGE
            selected.append(path)
    else:
        selected = sorted(known)

    try:
        source_adapter = reconcile._build_source(args)
    except (ObservationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_OBSERVATION_FAILED

    documents: list[dict[str, Any]] = []
    any_unobserved = False
    corpus_root = Path(args.corpus)
    for path in selected:
        try:
            result, unobserved = assess(
                path,
                validation.manifests[path],
                source_adapter,
                corpus=corpus_root,
                ready_schema=ready_schema,
            )
        except (ObservationError, SnapshotIncomplete, ValueError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_OBSERVATION_FAILED
        documents.append(result)
        any_unobserved = any_unobserved or bool(unobserved)

    rendered = json.dumps(render(documents), indent=2, sort_keys=True)
    try:
        if args.output:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE

    return EXIT_OBSERVATION_FAILED if any_unobserved else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
