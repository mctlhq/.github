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

# GitHub closes issues with a reason. Only these count as delivered work; an issue
# closed as not planned or as a duplicate did not complete the item it stands for.
_NOT_DELIVERED = {"not_planned", "duplicate"}


def item_status(
    key: IssueKey | None,
    observed: ObservedGraph,
    unobserved: frozenset[IssueKey],
) -> tuple[str, str]:
    """Return (status, reason) for one work item."""

    if key is None:
        return INCOMPLETE, "unbound"
    if key in unobserved:
        return UNKNOWN, "unobserved"
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
    return COMPLETE, "closed"


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

    items: list[dict[str, Any]] = []
    for work_item in sorted(spec["workItems"], key=lambda item: item["id"]):
        key = validate.issue_key(work_item.get("issue"))
        status, reason = item_status(key, observed, unobserved)
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
