"""Deterministic mutators for GitHubGraphSnapshot fixtures.

A detector that only knows how to report drift is as broken as a guard that
only knows how to pass, so the test suite proves both directions: it takes the
synthetic converged graph, breaks exactly one thing, and asserts that exactly
that one thing is reported.

Every mutator is pure. It deep-copies its input and returns the copy, so a test
can assert the original fixture is untouched and that restoring a mutation
returns an object equal to where it started.
"""

from __future__ import annotations

import copy
from typing import Any

Ref = dict[str, Any]


def ref(text: str) -> Ref:
    """Parse "owner/repo#123" into an issue ref."""

    repository, _, number = text.partition("#")
    return {"repository": repository, "number": int(number)}


def _same(left: Ref, right: Ref) -> bool:
    return (
        left["repository"].lower() == right["repository"].lower()
        and left["number"] == right["number"]
    )


def _find(snapshot: dict[str, Any], target: Ref) -> dict[str, Any]:
    for observation in snapshot["issues"]:
        if _same(observation["requested"], target):
            return observation
    raise KeyError(f"{target['repository']}#{target['number']} is not in the snapshot")


def drop_parent_edge(snapshot: dict[str, Any], child: str) -> dict[str, Any]:
    """Remove one hierarchy edge from both directions it is observable from."""

    result = copy.deepcopy(snapshot)
    target = ref(child)
    _find(result, target)["parent"] = None
    for observation in result["issues"]:
        observation["subIssues"] = [
            item for item in observation.get("subIssues", []) if not _same(item, target)
        ]
    return result


def repoint_parent(
    snapshot: dict[str, Any], child: str, new_parent: str
) -> dict[str, Any]:
    result = drop_parent_edge(snapshot, child)
    target = ref(child)
    parent = ref(new_parent)
    _find(result, target)["parent"] = copy.deepcopy(parent)
    parent_observation = _find(result, parent)
    parent_observation.setdefault("subIssues", []).append(copy.deepcopy(target))
    return result


def add_second_parent(
    snapshot: dict[str, Any], child: str, extra_parent: str
) -> dict[str, Any]:
    """Make one issue observable under two parents at once."""

    result = copy.deepcopy(snapshot)
    parent_observation = _find(result, ref(extra_parent))
    parent_observation.setdefault("subIssues", []).append(ref(child))
    return result


def drop_dependency(
    snapshot: dict[str, Any], blocked: str, blocker: str
) -> dict[str, Any]:
    result = copy.deepcopy(snapshot)
    observation = _find(result, ref(blocked))
    target = ref(blocker)
    observation["blockedBy"] = [
        item for item in observation.get("blockedBy", []) if not _same(item, target)
    ]
    return result


def add_dependency(
    snapshot: dict[str, Any], blocked: str, blocker: str
) -> dict[str, Any]:
    result = copy.deepcopy(snapshot)
    _find(result, ref(blocked)).setdefault("blockedBy", []).append(ref(blocker))
    return result


def mark_missing(snapshot: dict[str, Any], issue: str) -> dict[str, Any]:
    """Model an issue that cannot be resolved at all.

    Everything the graph knew about it goes too: an issue nobody can see is not
    quietly still listed as somebody's sub-issue.
    """

    result = copy.deepcopy(snapshot)
    target = ref(issue)
    observation = _find(result, target)
    for key in ("resolved", "state", "updatedAt", "parent", "subIssues", "blockedBy"):
        observation.pop(key, None)
    observation["found"] = False

    for other in result["issues"]:
        if other is observation:
            continue
        for relation in ("subIssues", "blockedBy"):
            if relation in other:
                other[relation] = [
                    item for item in other[relation] if not _same(item, target)
                ]
        if other.get("parent") is not None and _same(other["parent"], target):
            other["parent"] = None
    return result


def redirect(snapshot: dict[str, Any], requested: str, resolved: str) -> dict[str, Any]:
    """Model a transferred issue: same object, new canonical identity.

    GitHub answers with the new identity everywhere, so every relation naming
    the old one is rewritten. Only the authored request still says the old name.
    """

    result = copy.deepcopy(snapshot)
    old = ref(requested)
    new = ref(resolved)
    _find(result, old)["resolved"] = copy.deepcopy(new)

    for observation in result["issues"]:
        if observation.get("parent") is not None and _same(observation["parent"], old):
            observation["parent"] = copy.deepcopy(new)
        for relation in ("subIssues", "blockedBy"):
            observation[relation] = [
                copy.deepcopy(new) if _same(item, old) else item
                for item in observation.get(relation, [])
            ]
    return result
