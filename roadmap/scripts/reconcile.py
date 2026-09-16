#!/usr/bin/env python3
"""Compare EpicDefinition desired state with the observed GitHub graph.

Read-only by construction. The output is a deterministic `RoadmapDiff`: the same
manifest bytes and the same snapshot bytes always produce byte-identical JSON,
because nothing here consults a clock or a random source.

Hierarchy, dependency and binding drift stay in separate collections. They fail
for unrelated reasons and conflating them would make one broken binding look
like a graph-wide collapse.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

import github_graph
import validate
from github_graph import (
    FixtureGraphSource,
    IssueKey,
    LiveGraphSource,
    ObservationError,
    ObservedGraph,
    SnapshotIncomplete,
    observed_graph,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "epics"

EXIT_CONVERGED = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

DRIFT = "drift"
INFORMATIONAL = "informational"

EPIC_OWNER = "epic"


class ReconcileError(RuntimeError):
    """Usage, IO, validation or auth failure. Never a statement about drift."""


# ---------------------------------------------------------------- desired


@dataclass(frozen=True)
class DesiredGraph:
    """Authored state, derived only after the whole corpus validates."""

    name: str
    root: IssueKey | None
    # Work items only. The root lives in its own field: a work item may legally
    # be called "epic", and sharing one dict would let it silently displace the
    # root binding, which would then never be compared at all.
    bindings: dict[str, IssueKey] = field(default_factory=dict)
    unbound: tuple[str, ...] = ()
    hierarchy: tuple[tuple[str, IssueKey, IssueKey], ...] = ()
    dependencies: tuple[tuple[str, IssueKey, IssueKey], ...] = ()
    external_refs: tuple[tuple[str, IssueKey], ...] = ()

    def authored_bindings(self) -> list[tuple[str, IssueKey]]:
        """Every binding this manifest owns, root first."""

        bindings: list[tuple[str, IssueKey]] = []
        if self.root is not None:
            bindings.append((EPIC_OWNER, self.root))
        bindings.extend(sorted(self.bindings.items()))
        return bindings

    def owned_keys(self) -> list[IssueKey]:
        return [key for _, key in self.authored_bindings()]

    def authored_keys(self) -> list[IssueKey]:
        keys = set(self.owned_keys())
        keys.update(key for _, key in self.external_refs)
        return sorted(keys)


def desired_graph(document: dict[str, Any]) -> DesiredGraph:
    spec = document["spec"]
    root = validate.issue_key(spec["github"]["issue"])

    bindings: dict[str, IssueKey] = {}
    unbound: list[str] = []
    for item in spec["workItems"]:
        key = validate.issue_key(item.get("issue"))
        if key is None:
            unbound.append(item["id"])
        else:
            bindings[item["id"]] = key

    hierarchy: list[tuple[str, IssueKey, IssueKey]] = []
    dependencies: list[tuple[str, IssueKey, IssueKey]] = []
    external_refs: list[tuple[str, IssueKey]] = []

    for item in spec["workItems"]:
        item_id = item["id"]
        child = bindings.get(item_id)
        if child is None:
            # An unbound item derives no edge: there is no GitHub object for a
            # relation to exist between.
            continue

        parent_id = item.get("parent")
        parent = bindings.get(parent_id) if parent_id else root
        if parent is not None and parent != child:
            hierarchy.append((item_id, child, parent))

        for target in item.get("dependsOn", []):
            blocker = bindings.get(target)
            if blocker is not None and blocker != child:
                dependencies.append((item_id, child, blocker))

        for external in item.get("externalDependsOn", []):
            blocker = validate.issue_key(external)
            if blocker is None or blocker == child:
                continue
            external_refs.append((item_id, blocker))
            dependencies.append((item_id, child, blocker))

    return DesiredGraph(
        name=document["metadata"]["name"],
        root=root,
        bindings=bindings,
        unbound=tuple(sorted(unbound)),
        hierarchy=tuple(sorted(hierarchy)),
        dependencies=tuple(sorted(dependencies)),
        external_refs=tuple(sorted(set(external_refs))),
    )


# ------------------------------------------------------------------- diff


def _ref(key: IssueKey) -> dict[str, Any]:
    return {"repository": key[0], "number": key[1]}


def _sorted(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: json.dumps(entry, sort_keys=True))


def _resolve_endpoints(
    desired: DesiredGraph, observed: ObservedGraph
) -> tuple[list[dict[str, Any]], dict[IssueKey, IssueKey], set[IssueKey]]:
    """Settle identity first, then say which endpoints may still be compared.

    A redirect is binding drift, not relation drift: the issue exists and has a
    canonical identity, so relations are rewritten onto it and keep being
    compared. Only an endpoint we could not pin down -- missing or ambiguous --
    suppresses the relations that touch it.
    """

    entries: list[dict[str, Any]] = []
    resolved: dict[IssueKey, IssueKey] = {}
    suppressed: set[IssueKey] = set()

    authored: list[tuple[str, IssueKey]] = (
        desired.authored_bindings() + sorted(desired.external_refs)
    )

    for owner, key in authored:
        if key in observed.missing:
            entries.append(
                {
                    "type": "BindingIssueNotFound",
                    "severity": DRIFT,
                    "owner": owner,
                    "requested": _ref(key),
                }
            )
            suppressed.add(key)
            continue

        target = observed.resolve(key)
        if target is None:
            raise SnapshotIncomplete(f"no observation for {key[0]}#{key[1]}")

        resolved[key] = target
        if target != key:
            entries.append(
                {
                    "type": "BindingRedirected",
                    "severity": DRIFT,
                    "owner": owner,
                    "requested": _ref(key),
                    "resolved": _ref(target),
                }
            )

        parents = observed.parent_of(target)
        if len(parents) > 1:
            entries.append(
                {
                    "type": "BindingAmbiguous",
                    "severity": DRIFT,
                    "owner": owner,
                    "requested": _ref(key),
                    "resolved": _ref(target),
                    "observedParents": [_ref(parent) for parent in parents],
                }
            )
            suppressed.add(key)

    # Two authored bindings that resolve to one canonical issue are as
    # ambiguous as one issue observed under two parents: a single live object
    # would satisfy both work items and the duplicate would never surface,
    # because the validator can only see the identities as authored.
    collisions: dict[IssueKey, list[IssueKey]] = {}
    for key, target in sorted(resolved.items()):
        collisions.setdefault(target, []).append(key)
    owners_by_authored = {key: owner for owner, key in authored}
    for target, sources in sorted(collisions.items()):
        if len(sources) < 2:
            continue
        for key in sources:
            entries.append(
                {
                    "type": "BindingAmbiguous",
                    "severity": DRIFT,
                    "owner": owners_by_authored.get(key, EPIC_OWNER),
                    "requested": _ref(key),
                    "resolved": _ref(target),
                }
            )
            suppressed.add(key)

    for item_id in desired.unbound:
        entries.append(
            {
                "type": "BindingUnbound",
                "severity": INFORMATIONAL,
                "owner": item_id,
            }
        )

    return entries, resolved, suppressed


def _suppressed_identities(
    resolved: dict[IssueKey, IssueKey], suppressed: set[IssueKey]
) -> set[IssueKey]:
    """Every identity a suppressed endpoint can appear under in the observed graph.

    Declining to compare an endpoint has to hold in both directions. Otherwise an
    endpoint we refused to judge comes back as an "unexpected" relation on its
    neighbour, and one ambiguous binding cascades into drift on every issue that
    points at it.
    """

    identities: set[IssueKey] = set()
    for key in suppressed:
        identities.add(key)
        target = resolved.get(key)
        if target is not None:
            identities.add(target)
    return identities


def _hierarchy_entries(
    desired: DesiredGraph,
    observed: ObservedGraph,
    resolved: dict[IssueKey, IssueKey],
    suppressed: set[IssueKey],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    for owner, child, parent in desired.hierarchy:
        if child in suppressed or parent in suppressed:
            continue
        child_key = resolved.get(child)
        parent_key = resolved.get(parent)
        if child_key is None or parent_key is None:
            continue

        observed_parents = observed.parent_of(child_key)
        if not observed_parents:
            entries.append(
                {
                    "type": "HierarchyMissingParent",
                    "severity": DRIFT,
                    "owner": owner,
                    "child": _ref(child_key),
                    "expectedParent": _ref(parent_key),
                }
            )
        elif parent_key not in observed_parents:
            entries.append(
                {
                    "type": "HierarchyWrongParent",
                    "severity": DRIFT,
                    "owner": owner,
                    "child": _ref(child_key),
                    "expectedParent": _ref(parent_key),
                    "observedParent": _ref(observed_parents[0]),
                }
            )

    owned = {resolved[key] for key in desired.owned_keys() if key in resolved}
    owned |= _suppressed_identities(resolved, suppressed)
    for owner, key in desired.authored_bindings():
        if key in suppressed:
            continue
        parent_key = resolved.get(key)
        if parent_key is None:
            continue
        for child_key in observed.children.get(parent_key, ()):
            if child_key in owned:
                continue
            entries.append(
                {
                    "type": "HierarchyUnexpectedChild",
                    "severity": INFORMATIONAL,
                    "owner": owner,
                    "child": _ref(child_key),
                    "observedParent": _ref(parent_key),
                }
            )

    return entries


def _dependency_entries(
    desired: DesiredGraph,
    observed: ObservedGraph,
    resolved: dict[IssueKey, IssueKey],
    suppressed: set[IssueKey],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    comparable: set[tuple[IssueKey, IssueKey]] = set()

    for owner, blocked, blocker in desired.dependencies:
        if blocked in suppressed or blocker in suppressed:
            continue
        blocked_key = resolved.get(blocked)
        blocker_key = resolved.get(blocker)
        if blocked_key is None or blocker_key is None:
            continue

        comparable.add((blocked_key, blocker_key))
        if (blocked_key, blocker_key) not in observed.blocked_by:
            entries.append(
                {
                    "type": "DependencyMissing",
                    "severity": DRIFT,
                    "owner": owner,
                    "blocked": _ref(blocked_key),
                    "blocker": _ref(blocker_key),
                }
            )

    # Unexpected edges are only reportable on issues this manifest owns; the
    # blocked-by list of somebody else's issue is not ours to have an opinion on.
    owners_by_key = {
        resolved[key]: owner
        for owner, key in desired.authored_bindings()
        if key in resolved and key not in suppressed
    }
    withheld = _suppressed_identities(resolved, suppressed)
    for blocked_key, blocker_key in sorted(observed.blocked_by):
        owner = owners_by_key.get(blocked_key)
        if owner is None or (blocked_key, blocker_key) in comparable:
            continue
        if blocked_key in withheld or blocker_key in withheld:
            continue
        entries.append(
            {
                "type": "DependencyUnexpected",
                "severity": DRIFT,
                "owner": owner,
                "blocked": _ref(blocked_key),
                "blocker": _ref(blocker_key),
            }
        )

    return entries


def diff(
    desired: DesiredGraph,
    observed: ObservedGraph,
    *,
    manifest_path: str,
    manifest_sha256: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    binding, resolved, suppressed = _resolve_endpoints(desired, observed)
    hierarchy = _hierarchy_entries(desired, observed, resolved, suppressed)
    dependency = _dependency_entries(desired, observed, resolved, suppressed)

    binding = _sorted(binding)
    hierarchy = _sorted(hierarchy)
    dependency = _sorted(dependency)

    every = binding + hierarchy + dependency
    epic: dict[str, Any] = {
        "name": desired.name,
        "manifest": {"path": manifest_path, "sha256": manifest_sha256},
    }
    if desired.root is not None:
        epic["issue"] = _ref(desired.root)

    return {
        "apiVersion": "roadmap.mctl.ai/v1alpha1",
        "kind": "RoadmapDiff",
        "epic": epic,
        "source": dict(source),
        "summary": {
            "binding": len(binding),
            "hierarchy": len(hierarchy),
            "dependency": len(dependency),
            "drift": sum(1 for entry in every if entry["severity"] == DRIFT),
            "informational": sum(
                1 for entry in every if entry["severity"] == INFORMATIONAL
            ),
        },
        "binding": binding,
        "hierarchy": hierarchy,
        "dependency": dependency,
    }


def has_drift(document: dict[str, Any]) -> bool:
    return document["summary"]["drift"] > 0


# --------------------------------------------------------------- preflight


def load_corpus(corpus: Path, schema: dict[str, Any]) -> dict[Path, dict[str, Any]]:
    """Validate the whole canonical corpus before anything reaches the network.

    Selecting one manifest must not narrow corpus-wide ownership: a duplicate
    binding in a manifest nobody asked about still means this graph does not own
    what it claims to own.
    """

    paths = validate._manifest_paths([str(corpus)])
    if not paths:
        raise ReconcileError(f"no EpicDefinition manifests found under {corpus}")

    documents: list[tuple[Path, dict[str, Any]]] = []
    failures: dict[Path, list[str]] = {}

    for path in paths:
        try:
            with path.open(encoding="utf-8") as handle:
                document = yaml.safe_load(handle)
            if not isinstance(document, dict):
                raise ValueError("document root must be a mapping")
            errors = validate.validate_document(document, schema)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            failures[path] = [str(exc)]
            continue
        if errors:
            failures[path] = errors
        else:
            documents.append((path, document))

    for path, errors in validate.corpus_errors(documents).items():
        failures.setdefault(path, []).extend(errors)

    if failures:
        rendered = "; ".join(
            f"{path}: {message}"
            for path in sorted(failures)
            for message in sorted(set(failures[path]))
        )
        raise ReconcileError(f"corpus validation failed: {rendered}")

    return {path.resolve(): document for path, document in documents}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


REPO_ROOT = ROOT.parent


def _manifest_label(path: Path) -> str:
    """Render the manifest path repo-relative so the diff is machine-portable.

    An absolute path would put the checkout location into the output and two
    machines reconciling identical bytes would disagree.
    """

    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def reconcile(
    manifest_path: Path,
    document: dict[str, Any],
    source_adapter: Any,
) -> dict[str, Any]:
    desired = desired_graph(document)
    snapshot = source_adapter.snapshot(desired.authored_keys())
    github_graph.require_observations(snapshot, desired.authored_keys())
    return diff(
        desired,
        observed_graph(snapshot),
        manifest_path=_manifest_label(manifest_path),
        manifest_sha256=_sha256(manifest_path),
        source=snapshot["source"],
    )


# --------------------------------------------------------------------- CLI


def _build_source(args: argparse.Namespace) -> Any:
    if args.snapshot:
        return FixtureGraphSource.from_path(Path(args.snapshot))
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    return LiveGraphSource(token, api_base=args.api_base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifests",
        nargs="*",
        help="manifests to reconcile (default: every manifest in the corpus)",
    )
    parser.add_argument(
        "--corpus",
        default=str(DEFAULT_CORPUS),
        help="canonical corpus root, always validated in full (default: roadmap/epics)",
    )
    parser.add_argument("--schema", default=str(validate.DEFAULT_SCHEMA))
    parser.add_argument("--snapshot", help="replay a captured or synthetic snapshot")
    parser.add_argument(
        "--live", action="store_true", help="read live GitHub state (GET only)"
    )
    parser.add_argument("--capture", help="live mode; also write the snapshot here")
    parser.add_argument("--api-base", default=github_graph.DEFAULT_API_BASE)
    parser.add_argument("--output", help="write the diff here instead of stdout")
    args = parser.parse_args(argv)

    if args.capture:
        args.live = True
    if bool(args.snapshot) == bool(args.live):
        print(
            "exactly one of --snapshot or --live/--capture is required",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        schema = validate._load_schema(Path(args.schema))
        corpus = load_corpus(Path(args.corpus), schema)

        if args.manifests:
            selected = []
            for raw in args.manifests:
                path = Path(raw).resolve()
                if path not in corpus:
                    raise ReconcileError(
                        f"{raw} is not part of the corpus at {args.corpus}"
                    )
                selected.append(path)
        else:
            selected = sorted(corpus)

        source_adapter = _build_source(args)
        documents = []
        for path in selected:
            documents.append((path, reconcile(path, corpus[path], source_adapter)))
    except (ReconcileError, SnapshotIncomplete, ObservationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        _emit(args, corpus, selected, source_adapter, documents)
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    return (
        EXIT_DRIFT
        if any(has_drift(document) for _, document in documents)
        else EXIT_CONVERGED
    )


def _emit(
    args: argparse.Namespace,
    corpus: dict[Path, dict[str, Any]],
    selected: list[Path],
    source_adapter: Any,
    documents: list[tuple[Path, dict[str, Any]]],
) -> None:
    if args.capture and documents:
        # The adapter caches observations, so this re-serializes what was
        # already read rather than issuing a second pass over GitHub.
        keys: set[IssueKey] = set()
        for path in selected:
            keys.update(desired_graph(corpus[path]).authored_keys())
        snapshot = source_adapter.snapshot(sorted(keys))
        Path(args.capture).write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    payload = [document for _, document in documents]
    rendered = json.dumps(
        payload[0] if len(payload) == 1 else payload,
        indent=2,
        sort_keys=True,
    )
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    raise SystemExit(main())
