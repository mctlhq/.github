#!/usr/bin/env python3
"""Per-epic semantic guards over an EpicDefinition's work-item graph.

JSON Schema and validate.py prove that a graph is well formed (known ids, no
cycles, unique bindings). They cannot say that a *particular* item must exist,
must stay required, or must sit upstream of another. A guard here does: it is a
small, declarative rule set registered by epic name, evaluated offline over the
manifest, with no GitHub reads or writes.

v1alpha1 has no conditional dependencies, so a rule like "if writes are ever
enabled, human approval must already be proven" is expressed as structure the
guard can check: the write-enabling item dependsOn the approval item, and no
required item may depend on that optional approval item.

Exit codes: 0 every guard clean, 1 a guard violation, 2 could not evaluate
(unreadable manifest, or a registered guard whose epic is absent from the
corpus). "Could not observe" is never reported as "clean".
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# Epic glovo-c2-friends-pilot (mctlhq/.github#365). Owner scope 2026-11-18:
# Owner-only -> 10 read-only -> Approval + Canary -> 20 read+write.
#
# The graph is split into two stages and the guard is a CLOSED WORLD: every
# work item must be classified below, so a renamed or newly added item cannot
# slip past a name-based match (the earlier rule matched the substring "write").
#   READ stage   the owner-only and 10-user read-only pilot; required for
#                completion (plus optional read-side evidence). It must never
#                depend, directly or transitively, on a WRITE item or on the
#                approval externals.
#   WRITE stage  everything that exists only for the 20 stage: the approval
#                proof (#55), the write canary (#57) and the 20 GO (#52). All
#                optional, so allRequired completion never includes the write
#                circuit. Optional items may depend on required ones; a required
#                item may never depend on an optional one (guarded below).
# Every WRITE item other than the approval proof must depend on it.
GLOVO_C2 = {
    "epic": "glovo-c2-friends-pilot",
    # id -> (required, bound issue as "repo#number")
    "items": {
        "c2-write-gate-decision": (True, "mctlhq/mctl-glovo#49"),
        "c2-write-disabled-proof": (True, "mctlhq/mctl-glovo#41"),
        "c2-fresh-link-identity": (True, "mctlhq/mctl-glovo#42"),
        "c2-security-boundary": (True, "mctlhq/mctl-glovo#44"),
        "c2-cutover-package": (True, "mctlhq/mctl-gitops#2777"),
        "c2-pilot-operations": (True, "mctlhq/mctl-glovo#46"),
        "c2-e2e-resilience": (True, "mctlhq/mctl-glovo#43"),
        "c2-order-intelligence": (True, "mctlhq/mctl-glovo#45"),
        "c2-go-owner": (True, "mctlhq/mctl-glovo#40"),
        "c2-go-2": (True, "mctlhq/mctl-glovo#50"),
        "c2-go-10": (True, "mctlhq/mctl-glovo#51"),
        "c2-cohort-feedback": (False, "mctlhq/mctl-glovo#47"),
        "c2-partner-dossier": (False, "mctlhq/mctl-glovo#48"),
        "c2-write-approval-proof": (False, "mctlhq/mctl-glovo#55"),
        "c2-write-canary": (False, "mctlhq/mctl-glovo#57"),
        "c2-go-20": (False, "mctlhq/mctl-glovo#52"),
    },
    "write_stage": {"c2-write-approval-proof", "c2-write-canary", "c2-go-20"},
    # (item, must depend on) edges that must be authored directly.
    "direct": [
        ("c2-write-disabled-proof", "c2-write-gate-decision"),
        ("c2-write-approval-proof", "c2-write-disabled-proof"),
        # The pre-GO's own upstream: deleting one is not silently tolerated.
        ("c2-go-owner", "c2-write-gate-decision"),
        ("c2-go-owner", "c2-write-disabled-proof"),
        ("c2-go-owner", "c2-fresh-link-identity"),
        ("c2-go-owner", "c2-cutover-package"),
        ("c2-go-owner", "c2-pilot-operations"),
        # The write canary follows the approval proof and the read-only 10 stage.
        ("c2-write-canary", "c2-write-approval-proof"),
        ("c2-write-canary", "c2-go-10"),
        # The 20 stage: all prerequisites are binding when it is run.
        ("c2-go-20", "c2-go-10"),
        ("c2-go-20", "c2-cohort-feedback"),
        ("c2-go-20", "c2-security-boundary"),
        ("c2-go-20", "c2-write-approval-proof"),
        ("c2-go-20", "c2-write-canary"),
    ],
    # (item, must be downstream of) paths of any length.
    "transitive": [
        ("c2-write-approval-proof", "c2-write-gate-decision"),
        ("c2-go-owner", "c2-write-gate-decision"),
        ("c2-e2e-resilience", "c2-go-owner"),
        ("c2-e2e-resilience", "c2-write-disabled-proof"),
        ("c2-go-2", "c2-write-disabled-proof"),
        ("c2-go-10", "c2-write-disabled-proof"),
        ("c2-go-10", "c2-e2e-resilience"),
        ("c2-write-canary", "c2-e2e-resilience"),
        # Evidence gates of the read-only pilot reach the 20 stage too.
        ("c2-go-20", "c2-e2e-resilience"),
        ("c2-go-20", "c2-pilot-operations"),
        ("c2-go-20", "c2-order-intelligence"),
        ("c2-go-20", "c2-write-disabled-proof"),
    ],
    "approval_item": "c2-write-approval-proof",
    "disabled_item": "c2-write-disabled-proof",
    # Items of the read-only pilot admission: no write item upstream, ever.
    "read_only_pilot": ["c2-go-owner", "c2-go-10"],
    # Rule W1 (mctl-glovo#49 / #55) must stay recorded in the manifest as a
    # named success criterion: writes are opened only by satisfying the
    # approval item, never by a flag alone. The guard checks its presence.
    "rule_w1": ("W1", "c2-write-approval-proof", "never by a flag alone"),
    # Approval externals: owned by the approval item only, never by a READ-stage
    # item (they would make the read-only pilot wait on the write stage).
    "approval_externals": {
        ("mctlhq/mctl-api", 1006),
        ("mctlhq/mctl-api", 1011),
        ("mctlhq/mctl-api", 1012),
    },
}

GUARDS = [GLOVO_C2]


def _items(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in document.get("spec", {}).get("workItems", []):
        if isinstance(item, dict) and "id" in item:
            result[item["id"]] = item
    return result


def _upstream(items: dict[str, dict[str, Any]], start: str) -> set[str]:
    """Every item `start` depends on, directly or transitively (cycle-safe)."""

    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        for dependency in items.get(current, {}).get("dependsOn", []):
            if dependency not in seen:
                seen.add(dependency)
                stack.append(dependency)
    return seen


def _required(item: dict[str, Any]) -> bool:
    return item.get("required") is True


def _label(issue: Any) -> str | None:
    key = validate.issue_key(issue)
    if key is None:
        return None
    return f"{key[0]}#{key[1]}"


def guard_errors(document: dict[str, Any], guard: dict[str, Any]) -> list[str]:
    """Violations of one guard over a schema-valid EpicDefinition."""

    errors: list[str] = []
    items = _items(document)
    name = guard["epic"]

    def fail(message: str) -> None:
        errors.append(f"epic {name}: {message}")

    # Graph well-formedness first: a cycle or unknown id also makes the
    # reachability checks below meaningless, so report it under this guard too.
    for message in validate.semantic_errors(document):
        fail(f"graph invalid: {message}")

    for item_id, (required, issue) in guard["items"].items():
        item = items.get(item_id)
        if item is None:
            fail(f"required guard item {item_id} is missing")
            continue
        if _required(item) != required:
            expected = "required" if required else "optional"
            fail(f"item {item_id} must be {expected}")
        if issue is not None:
            bound = _label(item.get("issue"))
            if bound is None or bound.lower() != issue.lower():
                fail(f"item {item_id} must be bound to {issue}, found {bound}")

    for source, target in guard["direct"]:
        if source in items and target not in items[source].get("dependsOn", []):
            fail(f"item {source} must dependsOn {target} directly")

    for source, target in guard["transitive"]:
        if source in items and target not in _upstream(items, source):
            fail(f"item {source} must depend on {target} (directly or transitively)")

    approval = guard["approval_item"]
    disabled = guard["disabled_item"]
    write_stage = guard["write_stage"]

    # Closed world: an item the guard does not know is a violation, so a
    # rename or a new write-capable item cannot bypass the write rules.
    for item_id in sorted(items):
        if item_id not in guard["items"]:
            fail(
                f"item {item_id} is not classified in the guard "
                "(add it to items and to the read or write stage)"
            )

    for item_id in sorted(write_stage):
        if item_id in items and item_id != approval and approval not in _upstream(items, item_id):
            fail(f"write-stage item {item_id} must depend on {approval}")

    # The read-only stage must stay free of the write stage. A required item
    # that depends on an optional one lets an optional item block completion;
    # a read-stage item that reaches the write circuit lets the pilot wait on
    # #1011/#1012, the canary or the 20 GO.
    for item_id, item in items.items():
        if item_id in write_stage:
            continue
        for dependency in sorted(_upstream(items, item_id) & write_stage):
            fail(f"read-only stage item {item_id} must not depend on write-stage item {dependency}")
        for external in item.get("externalDependsOn", []):
            key = validate.issue_key(external)
            if key is not None and (key[0], key[1]) in guard["approval_externals"]:
                fail(
                    f"read-only stage item {item_id} must not depend on approval external "
                    f"{validate.issue_label(external)}"
                )
    for item_id in guard["read_only_pilot"]:
        if item_id in items:
            for dependency in sorted(_upstream(items, item_id) & write_stage):
                fail(f"read-only pilot {item_id} must not depend on {dependency}")
    for item_id, item in items.items():
        if not _required(item):
            continue
        for dependency in item.get("dependsOn", []):
            if dependency in items and not _required(items[dependency]):
                fail(f"required item {item_id} depends on optional {dependency}")
        if approval in _upstream(items, item_id):
            fail(f"required item {item_id} must not depend on {approval}")

    # Rule W1 must stay a named criterion of the epic.
    criteria = [c for c in document.get("spec", {}).get("successCriteria", []) if isinstance(c, str)]
    if not any(all(token in criterion for token in guard["rule_w1"]) for criterion in criteria):
        fail(
            "successCriteria must record rule W1 (writes are enabled only by "
            f"satisfying {approval}, never by a flag alone)"
        )

    # The disabled-path proof is the read-only stage's own item: it may not
    # inherit the write stage through any upstream either.
    if disabled in items and approval in _upstream(items, disabled):
        fail(f"{disabled} must not depend on {approval}")

    # The approval item keeps the externals that make it the real gate.
    if approval in items:
        present = set()
        for external in items[approval].get("externalDependsOn", []):
            key = validate.issue_key(external)
            if key is not None:
                present.add((key[0], key[1]))
        for repository, number in sorted(guard["approval_externals"]):
            if (repository, number) not in present:
                fail(f"item {approval} must list external {repository}#{number}")

    return sorted(set(errors))


def corpus_guard_errors(
    documents: Iterable[tuple[Path, dict[str, Any]]],
) -> tuple[dict[Path, list[str]], list[str]]:
    """(violations by path, guards whose epic was not found)."""

    by_name: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path, document in documents:
        if document.get("kind") == "EpicDefinition":
            by_name[document.get("metadata", {}).get("name")] = (path, document)

    violations: dict[Path, list[str]] = {}
    missing: list[str] = []
    for guard in GUARDS:
        found = by_name.get(guard["epic"])
        if found is None:
            missing.append(guard["epic"])
            continue
        path, document = found
        errors = guard_errors(document, guard)
        if errors:
            violations.setdefault(path, []).extend(errors)
    return violations, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=[str(ROOT / "epics")])
    args = parser.parse_args(argv)

    documents: list[tuple[Path, dict[str, Any]]] = []
    for path in validate._manifest_paths(args.paths):
        try:
            document = validate._load_manifest(path)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            print(f"epic_guards: ERROR: {path}: {exc}", file=sys.stderr)
            return 2
        if isinstance(document, dict):
            documents.append((path, document))

    violations, missing = corpus_guard_errors(documents)
    if missing:
        for epic in missing:
            print(
                f"epic_guards: ERROR: guarded epic {epic} not found in the corpus",
                file=sys.stderr,
            )
        return 2

    for guard in GUARDS:
        print(f"guard {guard['epic']}")
    for path, errors in sorted(violations.items()):
        print(f"FAIL {path}")
        for error in errors:
            print(f"  - {error}")
    if violations:
        return 1
    print("PASS epic guards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
