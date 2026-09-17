#!/usr/bin/env python3
"""Compute epic completion from an EpicDefinition and an observed GitHub graph.

The v1alpha1 contract has one completion mode, `allRequired`: an epic is complete
when every work item with `required: true` has reached its terminal completion
condition in the observed graph. Optional items (`required: false`) are reported
but never hold completion back.

Completion is a separate axis from health. An epic can be healthy and incomplete
(nothing has drifted, work is simply unfinished) or complete and drifted.

The same invariant as health applies here:

    A work item is incomplete only when its state was actually observed.
    An item nobody could observe is `unknown`, never `incomplete` and never
    `complete`.

Pure: no I/O and no clock.
"""

from __future__ import annotations

from typing import Any

import validate
from github_graph import IssueKey, ObservedGraph

MODE_ALL_REQUIRED = "allRequired"

COMPLETE = "complete"
INCOMPLETE = "incomplete"
UNKNOWN = "unknown"

# GitHub closes issues with a reason. Only `completed` -- or no recorded reason,
# which is how issues closed before reasons existed appear -- counts as delivered.
# Closed as not planned or duplicate did not complete the item. Any other reason
# (`reopened` on a closed issue, or a value GitHub adds later) is not evidence
# either way, so it is unknown: fail closed rather than count it as done.
_DELIVERED = {None, "completed"}
_NOT_DELIVERED = {"not_planned", "duplicate"}


def item_status(
    key: IssueKey | None,
    observed: ObservedGraph,
    unobserved: frozenset[IssueKey],
    ambiguous: frozenset[IssueKey] = frozenset(),
) -> tuple[str, str]:
    """Return (status, reason) for one work item."""

    if key is None:
        return INCOMPLETE, "unbound"
    if key in unobserved:
        return UNKNOWN, "unobserved"
    if key in ambiguous:
        # Two work items resolve to one live issue (a transfer can do this). That
        # issue's state cannot be credited to either: the reconciler reports the
        # same pair as BindingAmbiguous and suppresses it.
        return UNKNOWN, "binding_ambiguous"
    if key in observed.missing:
        # Observed, and GitHub says the issue does not exist: that is evidence.
        return INCOMPLETE, "issue_not_found"

    target = observed.resolve(key)
    if target is None:
        return UNKNOWN, "unobserved"
    state = observed.states.get(target)
    if state is None:
        return UNKNOWN, "state_not_observed"

    value, reason = state
    if value == "open":
        return INCOMPLETE, "open"
    if reason in _NOT_DELIVERED:
        return INCOMPLETE, f"closed_{reason}"
    if reason in _DELIVERED:
        return COMPLETE, "closed"
    return UNKNOWN, "closed_reason_unrecognized"


def _colliding_bindings(
    work_items: list[dict[str, Any]],
    observed: ObservedGraph,
    unobserved: frozenset[IssueKey],
) -> frozenset[IssueKey]:
    """Authored keys whose binding the reconciler reports as BindingAmbiguous.

    Both of its causes count: two work items resolving onto one live issue, and a
    live issue observed under more than one parent.
    """

    ambiguous: set[IssueKey] = set()
    by_target: dict[IssueKey, set[IssueKey]] = {}
    for work_item in work_items:
        key = validate.issue_key(work_item.get("issue"))
        if key is None or key in unobserved:
            continue
        target = observed.resolve(key)
        if target is None:
            continue
        by_target.setdefault(target, set()).add(key)
        if len(observed.parent_of(target)) > 1:
            ambiguous.add(key)
    ambiguous.update(key for keys in by_target.values() if len(keys) > 1 for key in keys)
    return frozenset(ambiguous)


def consistency_errors(block: dict[str, Any]) -> list[str]:
    """Semantic checks a completion block must pass beyond its JSON Schema.

    JSON Schema can say `blocking` is empty or not, but not that it is exactly the
    required items that are not complete, nor that the counts and status agree
    with the items. A consumer should run this on any RoadmapHealth it did not
    compute itself.
    """

    errors: list[str] = []
    items = block.get("items", [])
    required = [item for item in items if item.get("required")]

    expected_blocking = sorted(item["id"] for item in required if item["status"] != COMPLETE)
    if sorted(block.get("blocking", [])) != expected_blocking:
        errors.append(
            f"blocking {sorted(block.get('blocking', []))} != required items not complete {expected_blocking}"
        )

    counts = {
        "total": len(required),
        **{status: sum(1 for item in required if item["status"] == status)
           for status in (COMPLETE, INCOMPLETE, UNKNOWN)},
    }
    if block.get("required") != counts:
        errors.append(f"required counts {block.get('required')} != derived {counts}")

    if counts[INCOMPLETE]:
        expected_status = INCOMPLETE
    elif counts[UNKNOWN]:
        expected_status = UNKNOWN
    else:
        expected_status = COMPLETE
    if block.get("status") != expected_status:
        errors.append(f"status {block.get('status')!r} != derived {expected_status!r}")
    return errors


def compute(
    document: dict[str, Any],
    observed: ObservedGraph,
    unobserved: frozenset[IssueKey] = frozenset(),
) -> dict[str, Any]:
    """Compute the completion block for one validated EpicDefinition.

    Epic status, for required items only:
    - `incomplete` if any required item was observed incomplete -- certain even
      when other items are unknown;
    - otherwise `unknown` if any required item could not be observed;
    - otherwise `complete`.
    """

    spec = document["spec"]
    mode = spec["completion"]["mode"]
    if mode != MODE_ALL_REQUIRED:
        raise ValueError(f"unsupported completion mode: {mode!r}")

    ambiguous = _colliding_bindings(spec["workItems"], observed, unobserved)

    items: list[dict[str, Any]] = []
    for work_item in sorted(spec["workItems"], key=lambda item: item["id"]):
        key = validate.issue_key(work_item.get("issue"))
        status, reason = item_status(key, observed, unobserved, ambiguous)
        entry: dict[str, Any] = {
            "id": work_item["id"],
            "required": bool(work_item["required"]),
            "status": status,
            "reason": reason,
        }
        if key is not None:
            entry["issue"] = {"repository": key[0], "number": key[1]}
        items.append(entry)

    required = [item for item in items if item["required"]]
    counts = {
        status: sum(1 for item in required if item["status"] == status)
        for status in (COMPLETE, INCOMPLETE, UNKNOWN)
    }
    if counts[INCOMPLETE]:
        epic_status = INCOMPLETE
    elif counts[UNKNOWN]:
        epic_status = UNKNOWN
    else:
        epic_status = COMPLETE

    return {
        "mode": mode,
        "status": epic_status,
        "required": {"total": len(required), **counts},
        "blocking": [item["id"] for item in required if item["status"] != COMPLETE],
        "items": items,
    }
