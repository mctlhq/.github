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


def add_unexpected_child(
    snapshot: dict[str, Any], parent: str, child: str
) -> dict[str, Any]:
    """Attach an issue the manifest does not own under one it does."""

    result = copy.deepcopy(snapshot)
    _find(result, ref(parent)).setdefault("subIssues", []).append(ref(child))
    return result


def set_state(
    snapshot: dict[str, Any], issue: str, state: str, reason: str | None
) -> dict[str, Any]:
    """Set one observed issue's state and stateReason. Pure, deep-copying.

    The deep-copy twin of `test_completion._set_state`, kept here so
    `test_ready.py` (and any future suite) does not have to copy it again.
    """

    result = copy.deepcopy(snapshot)
    target = ref(issue)
    for observation in result["issues"]:
        if _same(observation["requested"], target):
            observation["state"] = state
            observation.pop("stateReason", None)
            if reason is not None:
                observation["stateReason"] = reason
    return result


def synthetic_snapshot(
    document: dict[str, Any],
    states: dict[str, tuple[str, str | None]] | None = None,
) -> dict[str, Any]:
    """Build a converged GitHubGraphSnapshot for any EpicDefinition document.

    Every authored identity -- owned bindings plus `externalDependsOn`
    targets -- is observed exactly as `reconcile.desired_graph()` derived it:
    parent edges and dependency edges converged, so `reconcile.py` reports
    zero drift against the manifest this was built from. `states` maps an
    "owner/repo#n" string to `(state, reason)`; a key not given defaults to
    `("open", None)`.

    This is how a manifest with no committed capture -- `lifecycle-ownership`,
    `unified-identity`, or any future epic -- gets a graph to test readiness
    against, without hand-editing a capture. `source.mode` is always
    `synthetic-fixture`, so a test graph can never masquerade as live
    evidence; a real live capture can replace it later without changing the
    contract this snapshot satisfies.
    """

    # Deferred: keeps this module importable with only `roadmap/tests` on
    # sys.path (as `.github/workflows/roadmap-validate.yml` does today for the
    # other mutators), and only requires `roadmap/scripts` on sys.path for
    # callers that actually use this function.
    import reconcile

    desired = reconcile.desired_graph(document)
    states = states or {}

    def _state_for(key: tuple[str, int]) -> tuple[str, str | None]:
        return states.get(f"{key[0]}#{key[1]}", ("open", None))

    parent_of: dict[tuple[str, int], tuple[str, int]] = {
        child: parent for _, child, parent in desired.hierarchy
    }
    children_of: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for child, parent in parent_of.items():
        children_of.setdefault(parent, []).append(child)

    blocked_by_of: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for _, blocked, blocker in desired.dependencies:
        blocked_by_of.setdefault(blocked, []).append(blocker)

    issues: list[dict[str, Any]] = []
    for key in desired.authored_keys():
        state, reason = _state_for(key)
        identity = {"repository": key[0], "number": key[1]}
        observation: dict[str, Any] = {
            "requested": dict(identity),
            "resolved": dict(identity),
            "found": True,
            "state": state,
            "parent": (
                {"repository": parent_of[key][0], "number": parent_of[key][1]}
                if key in parent_of
                else None
            ),
            "subIssues": [
                {"repository": child[0], "number": child[1]}
                for child in sorted(children_of.get(key, []))
            ],
            "blockedBy": [
                {"repository": blocker[0], "number": blocker[1]}
                for blocker in sorted(blocked_by_of.get(key, []))
            ],
        }
        if reason is not None:
            observation["stateReason"] = reason
        issues.append(observation)

    return {
        "apiVersion": "roadmap.mctl.ai/v1alpha1",
        "kind": "GitHubGraphSnapshot",
        "source": {"mode": "synthetic-fixture"},
        "issues": issues,
    }

