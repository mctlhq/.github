#!/usr/bin/env python3
"""Validate declarative mctl EpicDefinition manifests.

The JSON Schema owns structural validation. This module adds graph invariants that
JSON Schema cannot express cleanly: unique local ids, valid references, acyclic
hierarchy/dependency graphs, and unique GitHub issue bindings.

It intentionally performs no GitHub reads or writes. Live reconciliation is a
separate phase of the roadmap control-plane initiative.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "schemas" / "epic-definition.schema.json"
DEFAULT_EPICS = ROOT / "epics"


def _load_schema(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return schema


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise ValueError("document root must be a mapping")
    return document


def _json_path(parts: Iterable[Any]) -> str:
    rendered = "$"
    for part in parts:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += f".{part}"
    return rendered


def schema_errors(document: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    failures = sorted(
        validator.iter_errors(document),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    return [f"{_json_path(error.absolute_path)}: {error.message}" for error in failures]


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def _cycle(nodes: Iterable[str], edges: dict[str, list[str]]) -> list[str] | None:
    """Return one deterministic cycle without using Python recursion."""

    node_set = set(nodes)
    state: dict[str, int] = {node: 0 for node in node_set}

    for start in sorted(node_set):
        if state[start] != 0:
            continue

        stack: list[tuple[str, list[str], int]] = []
        path: list[str] = []
        path_index: dict[str, int] = {}

        state[start] = 1
        path_index[start] = 0
        path.append(start)
        stack.append(
            (
                start,
                [target for target in sorted(edges.get(start, [])) if target in node_set],
                0,
            )
        )

        while stack:
            node, neighbours, index = stack[-1]

            if index >= len(neighbours):
                stack.pop()
                path.pop()
                path_index.pop(node, None)
                state[node] = 2
                continue

            target = neighbours[index]
            stack[-1] = (node, neighbours, index + 1)

            if state[target] == 0:
                state[target] = 1
                path_index[target] = len(path)
                path.append(target)
                stack.append(
                    (
                        target,
                        [
                            child
                            for child in sorted(edges.get(target, []))
                            if child in node_set
                        ],
                        0,
                    )
                )
            elif state[target] == 1:
                cycle_start = path_index[target]
                return path[cycle_start:] + [target]

    return None


def _issue_key(ref: Any) -> tuple[str, int] | None:
    if (
        isinstance(ref, dict)
        and isinstance(ref.get("repository"), str)
        and isinstance(ref.get("number"), int)
    ):
        return (ref["repository"], ref["number"])
    return None


def _document_bindings(document: dict[str, Any]) -> list[tuple[tuple[str, int], str]]:
    """Return every authored GitHub issue binding in one manifest."""

    bindings: list[tuple[tuple[str, int], str]] = []
    spec = document.get("spec", {})

    root_key = _issue_key(spec.get("github", {}).get("issue"))
    if root_key is not None:
        bindings.append((root_key, "epic"))

    for item in spec.get("workItems", []):
        if not isinstance(item, dict):
            continue
        key = _issue_key(item.get("issue"))
        if key is not None:
            bindings.append((key, item.get("id", "<unknown>")))

    return bindings


def semantic_errors(document: dict[str, Any]) -> list[str]:
    """Validate invariants over a schema-valid EpicDefinition document."""

    errors: list[str] = []
    spec = document.get("spec", {})
    phases = spec.get("phases", [])
    work_items = spec.get("workItems", [])

    phase_ids = [
        phase["id"] for phase in phases if isinstance(phase, dict) and "id" in phase
    ]
    for phase_id in _duplicates(phase_ids):
        errors.append(f"duplicate phase id: {phase_id}")
    phase_set = set(phase_ids)

    item_ids = [
        item["id"] for item in work_items if isinstance(item, dict) and "id" in item
    ]
    for item_id in _duplicates(item_ids):
        errors.append(f"duplicate work item id: {item_id}")
    item_set = set(item_ids)

    parent_edges: dict[str, list[str]] = {item_id: [] for item_id in item_ids}
    dependency_edges: dict[str, list[str]] = {item_id: [] for item_id in item_ids}

    issue_bindings: dict[tuple[str, int], str] = {}
    root_key = _issue_key(spec.get("github", {}).get("issue"))
    if root_key is not None:
        issue_bindings[root_key] = "epic"

    external_refs: list[tuple[str, tuple[str, int]]] = []

    for item in work_items:
        if not isinstance(item, dict) or "id" not in item:
            continue
        item_id = item["id"]

        phase_id = item.get("phase")
        if phase_id not in phase_set:
            errors.append(f"work item {item_id}: unknown phase {phase_id!r}")

        parent = item.get("parent")
        if parent is not None:
            if parent == item_id:
                errors.append(f"work item {item_id}: parent cannot reference itself")
            elif parent not in item_set:
                errors.append(f"work item {item_id}: unknown parent {parent!r}")
            else:
                parent_edges[item_id].append(parent)

        for dependency in item.get("dependsOn", []):
            if dependency == item_id:
                errors.append(
                    f"work item {item_id}: dependsOn cannot reference itself"
                )
            elif dependency not in item_set:
                errors.append(
                    f"work item {item_id}: unknown dependsOn target {dependency!r}"
                )
            else:
                dependency_edges[item_id].append(dependency)

        issue = item.get("issue")
        key = _issue_key(issue)
        if issue is None:
            if not item.get("title"):
                errors.append(f"work item {item_id}: unbound item requires title")
            if not item.get("owner"):
                errors.append(f"work item {item_id}: unbound item requires owner")
        elif key is not None:
            existing = issue_bindings.get(key)
            if existing is not None:
                errors.append(
                    f"GitHub issue {key[0]}#{key[1]} is bound more than once: "
                    f"{existing}, {item_id}"
                )
            else:
                issue_bindings[key] = item_id

        for external in item.get("externalDependsOn", []):
            external_key = _issue_key(external)
            if external_key is not None:
                external_refs.append((item_id, external_key))

    for item_id, external_key in external_refs:
        local_owner = issue_bindings.get(external_key)
        if local_owner is not None:
            errors.append(
                f"work item {item_id}: externalDependsOn "
                f"{external_key[0]}#{external_key[1]} is locally bound by {local_owner}; "
                "use dependsOn instead"
            )

    parent_cycle = _cycle(item_ids, parent_edges)
    if parent_cycle:
        errors.append("parent graph contains cycle: " + " -> ".join(parent_cycle))

    dependency_cycle = _cycle(item_ids, dependency_edges)
    if dependency_cycle:
        errors.append("dependency graph contains cycle: " + " -> ".join(dependency_cycle))

    return sorted(errors)


def validate_document(
    document: dict[str, Any], schema: dict[str, Any]
) -> list[str]:
    structural = schema_errors(document, schema)
    if structural:
        return structural
    return semantic_errors(document)


def corpus_errors(
    documents: list[tuple[Path, dict[str, Any]]]
) -> dict[Path, list[str]]:
    """Validate invariants that span all EpicDefinition manifests."""

    failures: dict[Path, list[str]] = {path: [] for path, _ in documents}
    names: dict[str, Path] = {}
    bindings: dict[tuple[str, int], tuple[Path, str]] = {}

    for path, document in sorted(documents, key=lambda item: str(item[0])):
        name = document["metadata"]["name"]
        existing_name_path = names.get(name)
        if existing_name_path is not None:
            message = (
                f"epic metadata.name {name!r} is duplicated across "
                f"{existing_name_path} and {path}"
            )
            failures[existing_name_path].append(message)
            failures[path].append(message)
        else:
            names[name] = path

        for key, local_owner in _document_bindings(document):
            existing = bindings.get(key)
            if existing is not None:
                existing_path, existing_owner = existing
                message = (
                    f"GitHub issue {key[0]}#{key[1]} is bound across manifests: "
                    f"{existing_path} ({existing_owner}) and {path} ({local_owner})"
                )
                failures[existing_path].append(message)
                failures[path].append(message)
            else:
                bindings[key] = (path, local_owner)

    return {
        path: sorted(set(messages))
        for path, messages in failures.items()
        if messages
    }


def _manifest_paths(arguments: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in arguments:
        path = Path(raw)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.yaml")))
            paths.extend(sorted(path.glob("*.yml")))
        else:
            paths.append(path)
    return sorted(set(paths))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        default=[str(DEFAULT_EPICS)],
        help="EpicDefinition YAML file(s) or directories (default: roadmap/epics)",
    )
    parser.add_argument(
        "--schema",
        default=str(DEFAULT_SCHEMA),
        help="JSON Schema path",
    )
    args = parser.parse_args(argv)

    try:
        schema = _load_schema(Path(args.schema))
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        print(f"schema: ERROR: {exc}", file=sys.stderr)
        return 2

    manifests = _manifest_paths(args.paths)
    if not manifests:
        print("no EpicDefinition manifests found", file=sys.stderr)
        return 2

    failures_by_path: dict[Path, list[str]] = {}
    valid_documents: list[tuple[Path, dict[str, Any]]] = []

    for path in manifests:
        try:
            document = _load_manifest(path)
            failures = validate_document(document, schema)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            failures = [str(exc)]
            document = None

        if failures:
            failures_by_path[path] = failures
        elif document is not None:
            valid_documents.append((path, document))

    for path, failures in corpus_errors(valid_documents).items():
        failures_by_path.setdefault(path, []).extend(failures)

    failed = False
    for path in manifests:
        failures = sorted(set(failures_by_path.get(path, [])))
        if failures:
            failed = True
            print(f"FAIL {path}")
            for failure in failures:
                print(f"  - {failure}")
        else:
            print(f"PASS {path}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
