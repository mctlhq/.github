#!/usr/bin/env python3
"""Diff-aware guard: refuse new required work on a completed epic.

A validator that reads only the final tree cannot tell "added after completion"
from "always there", so this compares the head manifests against a base. It is
offline and read-only: it runs fixed-argv git commands and makes no network or
GitHub API calls.

Exit codes: 0 evaluated and clean, 1 violation, 2 could not be evaluated safely
(unresolvable/unreadable/malformed base, or an invalid head corpus). "Could not
observe" is never reported as "nothing changed".
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate  # noqa: E402

EPICS_SUBDIR = "roadmap/epics"
MANIFEST_SUFFIXES = (".yaml", ".yml")


class GuardError(Exception):
    """The guard could not evaluate safely (exit 2). Message carries the prefix."""


class BaseError(GuardError):
    pass


@dataclass(frozen=True)
class EpicState:
    path: str
    name: str
    lifecycle: str
    required: dict[str, bool]


@dataclass(frozen=True)
class Violation:
    code: str
    epic: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: epic {self.epic!r}: {self.detail}"


def epic_state(path: str, document: Any, error: type[GuardError] = BaseError,
               prefix: str = "base-malformed") -> EpicState:
    def bad(reason: str) -> GuardError:
        return error(f"{prefix}: {path}: {reason}")

    if not isinstance(document, dict):
        raise bad("document root must be a mapping")
    metadata = document.get("metadata")
    spec = document.get("spec")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if not isinstance(name, str) or not name:
        raise bad("missing metadata.name")
    if not isinstance(spec, dict):
        raise bad("missing spec")
    lifecycle = spec.get("lifecycle")
    if not isinstance(lifecycle, str) or not lifecycle:
        raise bad("missing spec.lifecycle")
    items = spec.get("workItems")
    if not isinstance(items, list):
        raise bad("missing spec.workItems list")
    required: dict[str, bool] = {}
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("required"), bool)
        ):
            raise bad("work item without string id and boolean required")
        required[item["id"]] = item["required"]
    return EpicState(path, name, lifecycle, required)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, check=False
    )


def load_base_from_git(repo: Path, ref: str, subdir: str = EPICS_SUBDIR) -> list[EpicState]:
    resolved = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if resolved.returncode != 0:
        hint = ""
        shallow = _git(repo, "rev-parse", "--is-shallow-repository")
        if shallow.stdout.strip() == b"true":
            hint = " (shallow checkout: fetch full history, e.g. fetch-depth: 0)"
        raise BaseError(f"base-unresolvable: {ref!r} is not a commit here{hint}")
    commit = resolved.stdout.decode().strip()

    listing = _git(repo, "ls-tree", "-rz", "--name-only", commit, "--", subdir)
    if listing.returncode != 0:
        raise BaseError(
            f"base-unreadable: cannot list {subdir} at {ref}: "
            f"{listing.stderr.decode(errors='replace').strip()}"
        )
    paths = sorted(
        raw.decode("utf-8", errors="replace")
        for raw in listing.stdout.split(b"\0")
        if raw and raw.decode("utf-8", errors="replace").endswith(MANIFEST_SUFFIXES)
    )
    states: list[EpicState] = []
    for path in paths:
        blob = _git(repo, "show", f"{commit}:{path}")
        if blob.returncode != 0:
            raise BaseError(f"base-unreadable: {path}")
        try:
            document = yaml.safe_load(blob.stdout.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            raise BaseError(f"base-malformed: {path}: {exc}") from exc
        states.append(epic_state(path, document))
    return states


def load_base_from_dir(directory: Path) -> list[EpicState]:
    states: list[EpicState] = []
    for path in sorted(
        p for p in directory.iterdir() if p.is_file() and p.suffix in MANIFEST_SUFFIXES
    ):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise BaseError(f"base-unreadable: {path}") from exc
        except yaml.YAMLError as exc:
            raise BaseError(f"base-malformed: {path}: {exc}") from exc
        states.append(epic_state(f"{EPICS_SUBDIR}/{path.name}", document))
    return states


def load_head(
    directory: Path, schema_path: Path
) -> list[tuple[str, dict[str, Any]]]:
    """Load and fully validate the head corpus; any failure is head-invalid."""

    try:
        schema = validate._load_schema(schema_path)
    except Exception as exc:  # noqa: BLE001 - any schema failure is unevaluable
        raise GuardError(f"head-invalid: schema: {exc}") from exc
    paths = validate._manifest_paths([str(directory)])
    if not paths:
        raise GuardError(f"head-invalid: no manifests under {directory}")
    documents: list[tuple[Path, dict[str, Any]]] = []
    problems: list[str] = []
    for path in paths:
        try:
            document = validate._load_manifest(path)
            failures = validate.validate_document(document, schema)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            problems.append(f"{path}: {exc}")
            continue
        if failures:
            problems.extend(f"{path}: {failure}" for failure in failures)
        else:
            documents.append((path, document))
    for path, failures in validate.corpus_errors(documents).items():
        problems.extend(f"{path}: {failure}" for failure in failures)
    if problems:
        raise GuardError("head-invalid: " + "; ".join(problems))
    return [(f"{EPICS_SUBDIR}/{path.name}", document) for path, document in documents]


def check(base: list[EpicState], head: list[EpicState]) -> list[Violation]:
    """Pure comparison. Pairs by metadata.name, falling back to the same path."""

    by_name = {state.name: state for state in base}
    by_path = {state.path: state for state in base}
    violations: list[Violation] = []
    for epic in head:
        if epic.lifecycle != "completed":
            continue
        previous = by_name.get(epic.name) or by_path.get(epic.path)
        head_required = sorted(i for i, req in epic.required.items() if req)
        if previous is None:
            if head_required:
                violations.append(
                    Violation(
                        "BornCompletedUnverified",
                        epic.name,
                        "new manifest is completed but holds required work "
                        f"({', '.join(head_required)}); closure cannot be observed "
                        "offline, land it active/paused first",
                    )
                )
            continue
        base_required = {i for i, req in previous.required.items() if req}
        for item in head_required:
            if item in base_required:
                continue
            if item in previous.required:
                violations.append(
                    Violation(
                        "RequiredFlagRaised",
                        epic.name,
                        f"work item {item!r} became required on a completed epic",
                    )
                )
            else:
                violations.append(
                    Violation(
                        "RequiredItemAdded",
                        epic.name,
                        f"required work item {item!r} added to a completed epic",
                    )
                )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--base-ref", help="git ref/commit holding the base manifests")
    source.add_argument("--base-dir", help="directory holding base manifests (tests)")
    parser.add_argument("--head-dir", default=str(validate.DEFAULT_EPICS))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--schema", default=str(validate.DEFAULT_SCHEMA))
    args = parser.parse_args(argv)

    try:
        if args.base_ref:
            base = load_base_from_git(Path(args.repo), args.base_ref)
        else:
            base = load_base_from_dir(Path(args.base_dir))
        head_docs = load_head(Path(args.head_dir), Path(args.schema))
        head = [epic_state(path, doc, GuardError, "head-invalid") for path, doc in head_docs]
    except GuardError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    violations = check(base, head)
    for violation in violations:
        print(f"FAIL {violation}")
    if violations:
        return 1
    print(f"lifecycle guard: ok ({len(head)} head manifests, {len(base)} base manifests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
