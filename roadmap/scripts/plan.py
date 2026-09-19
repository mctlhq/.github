#!/usr/bin/env python3
"""Turn a RoadmapDiff into the set of GitHub mutations a manifest authorizes.

`plan()` is pure: no clock, no randomness, no network, no absolute path. The
same manifest bytes and the same snapshot bytes always produce byte-identical
JSON, exactly like `reconcile.diff()` -- which is what makes a plan reviewable
before anything is written, and what makes `planId` mean something.

Two rules carry the whole safety argument of the write boundary:

* Every operation endpoint must be an identity the manifest itself authored.
  A target is therefore a subset of `DesiredGraph.authored_keys()` by
  construction, not by review, and no model output can widen it.
* Any binding-family drift refuses the WHOLE plan. A redirect, a deletion or
  an ambiguity means the authored identity no longer names the object we think
  it names; converging relations onto it would write over somebody else's
  graph. Only a human editing the manifest can settle that.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema.exceptions import SchemaError

import github_graph
import reconcile
import validate
from github_graph import IssueKey, ObservationError, SnapshotIncomplete

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN_SCHEMA = ROOT / "schemas" / "roadmap-apply-plan.schema.json"

EXIT_EMPTY = 0
EXIT_OPERATIONS = 1
EXIT_ERROR = 2
EXIT_REFUSED = 3

ADD_SUB_ISSUE = "AddSubIssue"
MOVE_SUB_ISSUE = "MoveSubIssue"
ADD_DEPENDENCY = "AddDependency"
REMOVE_DEPENDENCY = "RemoveDependency"

# Diff entry type -> plan outcome. Anything not in one of these three tables is
# an entry type this version does not understand, and an unknown entry is an
# error rather than something to ignore: silently dropping it would let a newer
# reconciler's drift pass as a converged graph.
OPERATION_FOR = {
    "HierarchyMissingParent": ADD_SUB_ISSUE,
    "HierarchyWrongParent": MOVE_SUB_ISSUE,
    "DependencyMissing": ADD_DEPENDENCY,
    "DependencyUnexpected": REMOVE_DEPENDENCY,
}
NOTE_TYPES = ("BindingUnbound", "HierarchyUnexpectedChild")
REFUSAL_TYPES = ("BindingIssueNotFound", "BindingRedirected", "BindingAmbiguous")

OP_ID_LENGTH = 16


class PlanRefused(RuntimeError):
    """A plan could not be built at all.

    Distinct from the `refusals` collection inside a plan. A refusal entry is a
    planned, reported outcome -- the manifest's identities are unsettled, so the
    plan is empty and stays empty. This exception is raised when the inputs
    themselves cannot be trusted to produce a plan: an operation naming an issue
    the manifest never authored, or a diff computed from other manifest bytes.
    """


def _key(ref: dict[str, Any]) -> IssueKey:
    key = validate.issue_key(ref)
    if key is None:
        raise PlanRefused(f"not an issue ref: {ref!r}")
    return key


def _ref(key: IssueKey) -> dict[str, Any]:
    return {"repository": key[0], "number": key[1]}


def _label(key: IssueKey) -> str:
    return f"{key[0]}#{key[1]}"


def _op_id(manifest_sha256: str, kind: str, endpoints: list[tuple[str, IssueKey]]) -> str:
    """Derive a stable id from the manifest digest and the operation's endpoints.

    Only those inputs. The id must not move when an unrelated work item is added
    to the snapshot or when the run happens twice, because apply uses it as an
    idempotency key and audit uses it to name what was written.
    """

    payload = "\n".join(
        [manifest_sha256, kind]
        + [f"{role}={_label(key)}" for role, key in endpoints]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:OP_ID_LENGTH]


def _plan_id(manifest_sha256: str, operations: list[dict[str, Any]]) -> str:
    payload = "\n".join([manifest_sha256] + [op["opId"] for op in operations])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _operation(manifest_sha256: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Map exactly one actionable diff entry onto exactly one operation."""

    kind = OPERATION_FOR[entry["type"]]
    owner = entry["owner"]

    if kind == ADD_SUB_ISSUE:
        child = _key(entry["child"])
        parent = _key(entry["expectedParent"])
        endpoints = [("child", child), ("parent", parent)]
        operation = {
            "opId": _op_id(manifest_sha256, kind, endpoints),
            "type": kind,
            "owner": owner,
            "child": _ref(child),
            "parent": _ref(parent),
            "precondition": {"type": "ParentAbsent", "child": _ref(child)},
        }
    elif kind == MOVE_SUB_ISSUE:
        child = _key(entry["child"])
        parent = _key(entry["expectedParent"])
        observed = _key(entry["observedParent"])
        endpoints = [("child", child), ("parent", parent), ("observedParent", observed)]
        operation = {
            "opId": _op_id(manifest_sha256, kind, endpoints),
            "type": kind,
            "owner": owner,
            "child": _ref(child),
            "parent": _ref(parent),
            "observedParent": _ref(observed),
            "precondition": {
                "type": "ParentIs",
                "child": _ref(child),
                "parent": _ref(observed),
            },
        }
    else:
        blocked = _key(entry["blocked"])
        blocker = _key(entry["blocker"])
        endpoints = [("blocked", blocked), ("blocker", blocker)]
        present = kind == REMOVE_DEPENDENCY
        operation = {
            "opId": _op_id(manifest_sha256, kind, endpoints),
            "type": kind,
            "owner": owner,
            "blocked": _ref(blocked),
            "blocker": _ref(blocker),
            "precondition": {
                "type": "DependencyPresent" if present else "DependencyAbsent",
                "blocked": _ref(blocked),
                "blocker": _ref(blocker),
            },
        }
    return operation


def _note(entry: dict[str, Any]) -> dict[str, Any]:
    if entry["type"] == "BindingUnbound":
        return {"type": "BindingUnbound", "owner": entry["owner"]}
    return {
        "type": "HierarchyUnexpectedChild",
        "owner": entry["owner"],
        "child": dict(entry["child"]),
        "observedParent": dict(entry["observedParent"]),
    }


def _refusal(entry: dict[str, Any]) -> dict[str, Any]:
    refusal: dict[str, Any] = {
        "reason": entry["type"],
        "owner": entry["owner"],
        "requested": dict(entry["requested"]),
    }
    # BindingIssueNotFound has nothing to resolve to -- the issue is gone.
    if "resolved" in entry:
        refusal["resolved"] = dict(entry["resolved"])
    return refusal


def operation_endpoints(operation: dict[str, Any]) -> list[IssueKey]:
    """Every GitHub identity an operation touches, ordered and deduplicated.

    Shared with `apply.py`, which re-runs the owned-target assertion against
    this same list immediately before each write.
    """

    if operation["type"] in (ADD_SUB_ISSUE, MOVE_SUB_ISSUE):
        refs = [operation["child"], operation["parent"]]
        if operation["type"] == MOVE_SUB_ISSUE:
            refs.append(operation["observedParent"])
    else:
        refs = [operation["blocked"], operation["blocker"]]

    keys: list[IssueKey] = []
    for ref in refs:
        key = _key(ref)
        if key not in keys:
            keys.append(key)
    return keys


def assert_authored(
    operation: dict[str, Any], authored: frozenset[IssueKey]
) -> None:
    """Refuse any operation touching an identity the manifest never authored.

    This is the whole containment argument: mutation targets come from validated
    manifest bytes or they do not happen. An unexpected relation to an issue
    outside the manifest is not quietly dropped either -- it refuses the run, so
    a human decides, rather than the engine reaching outside what it was given.
    """

    foreign = [key for key in operation_endpoints(operation) if key not in authored]
    if foreign:
        rendered = ", ".join(_label(key) for key in foreign)
        raise PlanRefused(
            f"{operation['type']} targets an identity this manifest does not "
            f"author: {rendered}"
        )


def plan(
    diff: dict[str, Any],
    *,
    document: dict[str, Any],
    manifest_path: str | None = None,
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Build a RoadmapApplyPlan from one RoadmapDiff.

    `document` is the validated manifest the diff was produced from; its
    `authored_keys()` bound every operation. `manifest_path` and
    `manifest_sha256` default to what the diff already carries, and are accepted
    explicitly so a caller holding a `LoadedManifest` can prove the diff and the
    plan describe the same bytes.
    """

    epic = diff["epic"]
    path = manifest_path or epic["manifest"]["path"]
    digest = manifest_sha256 or epic["manifest"]["sha256"]
    if digest != epic["manifest"]["sha256"]:
        raise PlanRefused(
            "the diff was produced from different manifest bytes "
            f"({epic['manifest']['sha256']} != {digest})"
        )

    entries = list(diff["binding"]) + list(diff["hierarchy"]) + list(diff["dependency"])
    unknown = sorted(
        {
            entry["type"]
            for entry in entries
            if entry["type"] not in OPERATION_FOR
            and entry["type"] not in NOTE_TYPES
            and entry["type"] not in REFUSAL_TYPES
        }
    )
    if unknown:
        raise PlanRefused(
            "the diff carries entry types this planner does not understand: "
            + ", ".join(unknown)
        )

    refusals = reconcile._sorted(
        [_refusal(entry) for entry in entries if entry["type"] in REFUSAL_TYPES]
    )
    notes = reconcile._sorted(
        [_note(entry) for entry in entries if entry["type"] in NOTE_TYPES]
    )

    if refusals:
        # Zero operations, and the refusal is evaluated BEFORE the authored-target
        # assertion: an unsettled identity must be reported as a refusal a human
        # can read, not raised as if the inputs were malformed.
        operations: list[dict[str, Any]] = []
    else:
        authored = frozenset(reconcile.desired_graph(document).authored_keys())
        operations = reconcile._sorted(
            [
                _operation(digest, entry)
                for entry in entries
                if entry["type"] in OPERATION_FOR
            ]
        )
        for operation in operations:
            assert_authored(operation, authored)

    plan_epic: dict[str, Any] = {
        "name": epic["name"],
        "manifest": {"path": path, "sha256": digest},
    }
    if "issue" in epic:
        plan_epic["issue"] = dict(epic["issue"])

    return {
        "apiVersion": "roadmap.mctl.ai/v1alpha1",
        "kind": "RoadmapApplyPlan",
        "epic": plan_epic,
        "source": dict(diff["source"]),
        "planId": _plan_id(digest, operations),
        "summary": {
            "operations": len(operations),
            "notes": len(notes),
            "refusals": len(refusals),
        },
        "operations": operations,
        "notes": notes,
        "refusals": refusals,
    }


def is_refused(document: dict[str, Any]) -> bool:
    return document["summary"]["refusals"] > 0


def has_operations(document: dict[str, Any]) -> bool:
    return document["summary"]["operations"] > 0


def load_schema(path: Path = DEFAULT_PLAN_SCHEMA) -> dict[str, Any]:
    return validate._load_schema(path)


def schema_errors(
    document: dict[str, Any], schema: dict[str, Any] | None = None
) -> list[str]:
    return validate.schema_errors(
        document, schema if schema is not None else load_schema()
    )


def render(documents: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    """One manifest yields a bare plan, several a list envelope.

    Same rule as `reconcile.render`: a bare JSON array carries no `kind` and
    matches no published contract.
    """

    ordered = [
        document for _, document in sorted(documents, key=lambda item: str(item[0]))
    ]
    if len(ordered) == 1:
        return ordered[0]
    return {
        "apiVersion": "roadmap.mctl.ai/v1alpha1",
        "kind": "RoadmapApplyPlanList",
        "items": ordered,
    }


def plan_manifest(
    manifest_path: Path,
    loaded: reconcile.LoadedManifest,
    source_adapter: Any,
    *,
    corpus: Path | None = None,
) -> dict[str, Any]:
    """Reconcile one validated manifest and plan the result."""

    diff = reconcile.reconcile(
        manifest_path,
        loaded.document,
        source_adapter,
        manifest_sha256=loaded.sha256,
        corpus=corpus,
    )
    return plan(diff, document=loaded.document, manifest_sha256=loaded.sha256)


# --------------------------------------------------------------------- CLI


def _emit(
    args: argparse.Namespace,
    corpus: dict[Path, reconcile.LoadedManifest],
    selected: list[Path],
    source_adapter: Any,
    documents: list[tuple[Path, dict[str, Any]]],
) -> None:
    if args.capture and documents:
        # The adapter caches observations, so this re-serializes what was
        # already read rather than issuing a second pass over GitHub.
        keys: set[IssueKey] = set()
        for path in selected:
            keys.update(reconcile.desired_graph(corpus[path].document).authored_keys())
        snapshot = source_adapter.snapshot(sorted(keys))
        Path(args.capture).write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    rendered = json.dumps(render(documents), indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "manifests",
        nargs="*",
        help="manifests to plan (default: every manifest in the corpus)",
    )
    parser.add_argument(
        "--corpus",
        default=str(reconcile.DEFAULT_CORPUS),
        help="canonical corpus root, always validated in full (default: roadmap/epics)",
    )
    parser.add_argument("--schema", default=str(validate.DEFAULT_SCHEMA))
    parser.add_argument(
        "--plan-schema",
        default=str(DEFAULT_PLAN_SCHEMA),
        help="contract every emitted plan is validated against",
    )
    parser.add_argument("--snapshot", help="replay a captured or synthetic snapshot")
    parser.add_argument(
        "--live", action="store_true", help="read live GitHub state (GET only)"
    )
    parser.add_argument("--capture", help="live mode; also write the snapshot here")
    parser.add_argument("--api-base", default=github_graph.DEFAULT_API_BASE)
    parser.add_argument("--output", help="write the plan here instead of stdout")
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
        plan_schema = validate._load_schema(Path(args.plan_schema))
        # Whole-corpus validation, before any source is built and therefore
        # before any network call: ownership a plan relies on is a corpus-wide
        # property, not a per-file one.
        corpus = reconcile.load_corpus(Path(args.corpus), schema)

        if args.manifests:
            selected = []
            for raw in args.manifests:
                path = Path(raw).resolve()
                if path not in corpus:
                    raise reconcile.ReconcileError(
                        f"{raw} is not part of the corpus at {args.corpus}"
                    )
                selected.append(path)
        else:
            selected = sorted(corpus)

        source_adapter = reconcile._build_source(args)
        documents: list[tuple[Path, dict[str, Any]]] = []
        for path in selected:
            document = plan_manifest(
                path, corpus[path], source_adapter, corpus=Path(args.corpus)
            )
            errors = schema_errors(document, plan_schema)
            if errors:
                raise PlanRefused(
                    "plan violates roadmap-apply-plan.schema.json: "
                    + "; ".join(errors)
                )
            documents.append((path, document))
    except PlanRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (
        reconcile.ReconcileError,
        SnapshotIncomplete,
        ObservationError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        SchemaError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        _emit(args, corpus, selected, source_adapter, documents)
    # --capture asks the source for its snapshot a second time, and that call is
    # held to the same contract as the first.
    except (OSError, ValueError, ObservationError, SnapshotIncomplete) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if any(is_refused(document) for _, document in documents):
        for _, document in documents:
            for refusal in document["refusals"]:
                print(
                    f"REFUSED: {refusal['reason']} for {refusal['owner']}",
                    file=sys.stderr,
                )
        return EXIT_REFUSED
    if any(has_operations(document) for _, document in documents):
        return EXIT_OPERATIONS
    return EXIT_EMPTY


if __name__ == "__main__":
    raise SystemExit(main())
