#!/usr/bin/env python3
"""Execute a RoadmapApplyPlan against GitHub, and record what happened.

The pipeline is fixed and has no branch a model can influence:

    validated corpus (at git revision R)
      -> reconcile (GET only)   -> RoadmapDiff
      -> plan.py (pure)         -> RoadmapApplyPlan
      -> per operation: re-read exactly that operation's endpoints
           already satisfied -> alreadySatisfied, no write
           matches the plan  -> one allow-listed write -> applied
           anything else     -> skipped (PreconditionChanged), no write
      -> RoadmapApplyResult + audit

There is deliberately no `--plan` flag and no free-form target argument. The
plan is always recomputed here from validated manifest bytes, so a plan file
edited on the way in is not a thing that exists, and the only way for anything
-- a person or a model -- to change what gets written is to change manifest text
in a reviewed pull request.

Guards, in order: `--execute` is required for any write; the manifest's git
revision must be resolvable, its checkout clean, and its bytes identical to the
bytes committed at that revision; `--approved-sha256`, when given, must equal
the digest of those bytes; every operation endpoint must be an authored identity
of the manifest; and the run must fit inside `--max-operations`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema.exceptions import SchemaError

import github_apply
import github_graph
import plan as plan_module
import reconcile
import validate
from github_apply import MutationFailed, MutationRefused
from github_graph import IssueKey, ObservationError, ObservedGraph, SnapshotIncomplete

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULT_SCHEMA = ROOT / "schemas" / "roadmap-apply-result.schema.json"

EXIT_OK = 0
EXIT_SKIPPED = 1
EXIT_ERROR = 2
EXIT_REFUSED = 3
EXIT_FAILED = 4

DEFAULT_MAX_OPERATIONS = 25

MODE_PLAN_ONLY = "plan-only"
MODE_EXECUTE = "execute"

APPLIED = "applied"
ALREADY_SATISFIED = "alreadySatisfied"
SKIPPED = "skipped"
FAILED = "failed"

NOT_EXECUTED = "NotExecuted"
PRECONDITION_CHANGED = "PreconditionChanged"
WRITE_FAILED = "WriteFailed"
MOVE_INCOMPLETE = "MoveIncompleteChildOrphaned"

_REVISION = re.compile(r"^[0-9a-f]{40}$")

# Keys that could only ever hold provider prose. The result schema already
# refuses them structurally; this set is the belt to that pair of braces, and it
# also catches a document assembled in memory by a caller of this module.
FORBIDDEN_KEYS = frozenset(
    {
        "assignee",
        "assignees",
        "author",
        "body",
        "comment",
        "comments",
        "description",
        "label",
        "labels",
        "message",
        "milestone",
        "prose",
        "state",
        "stateReason",
        "text",
        "title",
        "user",
    }
)
MAX_STRING_LENGTH = 2048


class ApplyError(RuntimeError):
    """Usage, IO or auth failure. Never a statement about the graph."""


class ApplyRefused(RuntimeError):
    """A guard said no. Zero writes have happened, and none will."""


# ------------------------------------------------------------------ git


def _git(cwd: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            check=False,
        )
    except OSError as exc:  # git missing entirely
        raise ApplyError(f"git could not be run: {exc}") from exc
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise ApplyError(f"git {' '.join(args)}: {message}")
    return completed.stdout


@dataclass(frozen=True)
class GitContext:
    """The commit the manifest bytes were read at, once it is proven."""

    revision: str


def resolve_git_context(manifest: Path, manifest_sha256: str) -> GitContext:
    """Prove "these bytes, at this revision" before anything may be written.

    The audit record's whole value is that claim. A dirty checkout cannot make
    it -- the file on disk is not the file at any commit -- and neither can a
    manifest whose bytes differ from the bytes committed at HEAD, which is what
    a stale index or a local edit looks like.
    """

    resolved = manifest.resolve()
    directory = resolved.parent
    top = Path(_git(directory, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    revision = _git(directory, "rev-parse", "HEAD").decode().strip()
    if not _REVISION.match(revision):
        raise ApplyRefused(f"git HEAD is not a full 40-character revision: {revision!r}")

    if _git(top, "status", "--porcelain").decode("utf-8", "replace").strip():
        raise ApplyRefused(
            "the checkout is dirty: apply only runs on committed, reviewed bytes"
        )

    try:
        relative = resolved.relative_to(top)
    except ValueError as exc:
        raise ApplyError(f"{manifest} is outside the git repository at {top}") from exc

    committed = _git(top, "show", f"{revision}:{relative.as_posix()}")
    if hashlib.sha256(committed).hexdigest() != manifest_sha256:
        raise ApplyRefused(
            f"{relative.as_posix()} differs from the bytes committed at {revision}"
        )
    return GitContext(revision=revision)


# -------------------------------------------------------------- execution


def _ref(key: IssueKey) -> dict[str, Any]:
    return {"repository": key[0], "number": key[1]}


def _key(ref: dict[str, Any]) -> IssueKey:
    return github_graph._ref_key(ref)


def observe(source: Any, keys: list[IssueKey]) -> ObservedGraph:
    """Re-read exactly these endpoints and normalize them.

    Called immediately before every decision about an operation, so a graph that
    changed after the plan was computed is visible at write time rather than
    written over.
    """

    snapshot = source.snapshot(keys)
    errors = github_graph.snapshot_errors(snapshot)
    if errors:
        raise ApplyError("re-read snapshot is invalid: " + "; ".join(errors))
    github_graph.require_observations(snapshot, keys)
    return github_graph.observed_graph(snapshot)


def _identity_settled(observed: ObservedGraph, keys: list[IssueKey]) -> bool:
    """Every endpoint must still answer under the identity the plan named.

    A deleted issue resolves to nothing and a transferred one resolves to a new
    canonical identity. Either way the plan's target no longer names the object
    the plan was reviewed against, and the operation is not ours to perform.
    """

    return all(observed.resolve(key) == key for key in keys)


def evaluate(operation: dict[str, Any], observed: ObservedGraph) -> str:
    """Compare live state with the plan: satisfied, ready, or changed."""

    endpoints = plan_module.operation_endpoints(operation)
    if not _identity_settled(observed, endpoints):
        return "changed"

    kind = operation["type"]
    if kind in (plan_module.ADD_SUB_ISSUE, plan_module.MOVE_SUB_ISSUE):
        child = _key(operation["child"])
        parent = _key(operation["parent"])
        parents = observed.parent_of(child)
        if parents == (parent,):
            return "satisfied"
        if kind == plan_module.ADD_SUB_ISSUE:
            return "ready" if parents == () else "changed"
        expected = (_key(operation["observedParent"]),)
        return "ready" if parents == expected else "changed"

    edge = (_key(operation["blocked"]), _key(operation["blocker"]))
    present = edge in observed.blocked_by
    if kind == plan_module.ADD_DEPENDENCY:
        return "satisfied" if present else "ready"
    return "ready" if present else "satisfied"


def _write(operation: dict[str, Any], mutator: github_apply.Mutator) -> str | None:
    """Perform one operation. Returns None on success, or a failure reason."""

    kind = operation["type"]
    try:
        if kind == plan_module.ADD_SUB_ISSUE:
            mutator.add_sub_issue(_key(operation["parent"]), _key(operation["child"]))
        elif kind == plan_module.ADD_DEPENDENCY:
            mutator.add_dependency(
                _key(operation["blocked"]), _key(operation["blocker"])
            )
        elif kind == plan_module.REMOVE_DEPENDENCY:
            mutator.remove_dependency(
                _key(operation["blocked"]), _key(operation["blocker"])
            )
        else:
            # GitHub has no single call that reparents an issue, so a move is a
            # remove followed by an add. If the add half fails the child is left
            # orphaned; that intermediate state is recorded rather than hidden,
            # the run exits non-zero, and the next replay finishes the move --
            # its precondition will then be "no parent", which is exactly the
            # AddSubIssue case.
            child = _key(operation["child"])
            mutator.remove_sub_issue(_key(operation["observedParent"]), child)
            try:
                mutator.add_sub_issue(_key(operation["parent"]), child)
            except MutationFailed:
                return MOVE_INCOMPLETE
    except MutationFailed:
        return WRITE_FAILED
    return None


def run_operation(
    operation: dict[str, Any],
    *,
    source_factory: Any,
    mutator: github_apply.Mutator,
    authored: frozenset[IssueKey],
    execute: bool,
) -> dict[str, Any]:
    """Re-read, decide, and only then -- at most once -- write.

    `source_factory` builds a reader per operation rather than sharing one: the
    live source caches an issue for the life of the process, and a cached read
    taken before an earlier write is not a re-read.
    """

    endpoints = plan_module.operation_endpoints(operation)
    result: dict[str, Any] = {
        "opId": operation["opId"],
        "type": operation["type"],
        "targets": [_ref(key) for key in endpoints],
    }

    state = evaluate(operation, observe(source_factory(), endpoints))
    if state == "satisfied":
        # Replay and retry converge here: the graph already says what the
        # manifest says, so the correct number of writes is zero.
        result["outcome"] = ALREADY_SATISFIED
        return result
    if state == "changed":
        result["outcome"] = SKIPPED
        result["reason"] = PRECONDITION_CHANGED
        return result
    if not execute:
        result["outcome"] = SKIPPED
        result["reason"] = NOT_EXECUTED
        return result

    # Defense in depth. The plan asserted this when it was built; asserting it
    # again here means the assertion holds against the operation actually about
    # to be transmitted, not against the one that was planned.
    plan_module.assert_authored(operation, authored)

    reason = _write(operation, mutator)
    if reason is None:
        result["outcome"] = APPLIED
    else:
        result["outcome"] = FAILED
        result["reason"] = reason
    return result


def apply_plan(
    document: dict[str, Any],
    *,
    source_factory: Any,
    mutator: github_apply.Mutator,
    authored: frozenset[IssueKey],
    execute: bool = False,
    max_operations: int = DEFAULT_MAX_OPERATIONS,
) -> list[dict[str, Any]]:
    """Run every operation in a plan, in plan order."""

    if document["refusals"]:
        rendered = ", ".join(
            f"{refusal['reason']} ({refusal['owner']})"
            for refusal in document["refusals"]
        )
        raise ApplyRefused(f"the plan refuses this manifest: {rendered}")

    operations = document["operations"]
    if len(operations) > max_operations:
        raise ApplyRefused(
            f"{len(operations)} operations exceeds --max-operations "
            f"({max_operations}); refusing rather than applying part of a plan"
        )
    for operation in operations:
        plan_module.assert_authored(operation, authored)

    return [
        run_operation(
            operation,
            source_factory=source_factory,
            mutator=mutator,
            authored=authored,
            execute=execute,
        )
        for operation in operations
    ]


# ----------------------------------------------------------------- result


def _forbidden_content_errors(document: Any, path: str = "$") -> list[str]:
    """Assert, before emission, that no provider prose reached the record.

    The schema already makes prose unrepresentable. This is the independent
    check on top of it: audit output that quietly grew an issue title would be
    a leak nobody notices, so it fails loudly here instead.
    """

    errors: list[str] = []
    if isinstance(document, dict):
        keys = set(document)
        for key in sorted(keys):
            if key in FORBIDDEN_KEYS:
                errors.append(f"{path}.{key}: provider content has no place here")
        if {"repository", "number"} <= keys and keys != {"repository", "number"}:
            extra = ", ".join(sorted(keys - {"repository", "number"}))
            errors.append(f"{path}: an issue ref carries only repository and number, got {extra}")
        for key in sorted(keys):
            errors.extend(_forbidden_content_errors(document[key], f"{path}.{key}"))
    elif isinstance(document, list):
        for index, item in enumerate(document):
            errors.extend(_forbidden_content_errors(item, f"{path}[{index}]"))
    elif isinstance(document, str):
        if "\n" in document or "\r" in document:
            errors.append(f"{path}: multi-line text is never identity or a code")
        elif len(document) > MAX_STRING_LENGTH:
            errors.append(f"{path}: {len(document)} characters is not identity or a code")
    return errors


def load_schema(path: Path = DEFAULT_RESULT_SCHEMA) -> dict[str, Any]:
    return validate._load_schema(path)


def schema_errors(
    document: dict[str, Any], schema: dict[str, Any] | None = None
) -> list[str]:
    return validate.schema_errors(
        document, schema if schema is not None else load_schema()
    )


def result(
    plan_document: dict[str, Any],
    operations: list[dict[str, Any]],
    *,
    mode: str,
    actor: str,
    git_revision: str,
    proposal: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and self-check a RoadmapApplyResult."""

    touched: list[IssueKey] = []
    for operation in operations:
        if operation["outcome"] not in (APPLIED, FAILED):
            continue
        for ref in operation["targets"]:
            key = _key(ref)
            if key not in touched:
                touched.append(key)

    manifest = plan_document["epic"]["manifest"]
    document = {
        "apiVersion": "roadmap.mctl.ai/v1alpha1",
        "kind": "RoadmapApplyResult",
        "epic": json.loads(json.dumps(plan_document["epic"])),
        "mode": mode,
        "summary": {
            "applied": sum(1 for item in operations if item["outcome"] == APPLIED),
            "alreadySatisfied": sum(
                1 for item in operations if item["outcome"] == ALREADY_SATISFIED
            ),
            "skipped": sum(1 for item in operations if item["outcome"] == SKIPPED),
            "failed": sum(1 for item in operations if item["outcome"] == FAILED),
        },
        "operations": operations,
        "audit": {
            "actor": actor,
            # Explicit null, never an omitted key: absent and unattributed must
            # not look alike in an audit record.
            "proposal": proposal,
            "manifest": {
                "path": manifest["path"],
                "sha256": manifest["sha256"],
                "gitRevision": git_revision,
            },
            "planId": plan_document["planId"],
            "targets": [_ref(key) for key in sorted(touched)],
        },
    }

    leaks = _forbidden_content_errors(document)
    if leaks:
        raise ApplyError("result carries provider content: " + "; ".join(leaks))
    errors = schema_errors(document, schema)
    if errors:
        raise ApplyError(
            "result violates roadmap-apply-result.schema.json: " + "; ".join(errors)
        )
    return document


def render(documents: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    ordered = [
        document for _, document in sorted(documents, key=lambda item: str(item[0]))
    ]
    if len(ordered) == 1:
        return ordered[0]
    return {
        "apiVersion": "roadmap.mctl.ai/v1alpha1",
        "kind": "RoadmapApplyResultList",
        "items": ordered,
    }


def exit_code(documents: list[dict[str, Any]]) -> int:
    if any(document["summary"]["failed"] for document in documents):
        return EXIT_FAILED
    if any(document["summary"]["skipped"] for document in documents):
        return EXIT_SKIPPED
    return EXIT_OK


# -------------------------------------------------------------------- CLI


def _issue_id_resolver(path: str | None):
    """Build the id lookup live writes need, from an operator-supplied file.

    GitHub's sub-issue and dependency endpoints identify the related issue by
    numeric id. `github-graph-snapshot.schema.json` records identity as
    {repository, number}, and the write client is forbidden to read, so for a
    live run the mapping is supplied from outside -- by an operator here, and by
    the apply activity in the mctl-agents follow-up. Offline runs never need it.
    """

    if path is None:
        return None
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ApplyError(f"{path}: expected an object of 'owner/repo#number': id")

    mapping: dict[IssueKey, int] = {}
    for label, value in raw.items():
        repository, _, number = str(label).partition("#")
        if not number.isdigit() or not isinstance(value, int):
            raise ApplyError(f"{path}: {label!r} is not 'owner/repo#number': <id>")
        mapping[(validate.canonical_repository(repository), int(number))] = value

    def resolve(key: IssueKey) -> int:
        try:
            return mapping[(validate.canonical_repository(key[0]), key[1])]
        except KeyError as exc:
            raise MutationRefused(
                f"no GitHub issue id supplied for {key[0]}#{key[1]}"
            ) from exc

    return resolve


def _proposal(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.proposal_url and not args.proposal_id:
        raise ApplyError("--proposal-url requires --proposal-id")
    if not args.proposal_id:
        return None
    proposal: dict[str, Any] = {"id": args.proposal_id}
    if args.proposal_url:
        proposal["url"] = args.proposal_url
    return proposal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "manifests",
        nargs="*",
        help="manifests to apply (default: every manifest in the corpus)",
    )
    parser.add_argument("--corpus", default=str(reconcile.DEFAULT_CORPUS))
    parser.add_argument("--schema", default=str(validate.DEFAULT_SCHEMA))
    parser.add_argument("--plan-schema", default=str(plan_module.DEFAULT_PLAN_SCHEMA))
    parser.add_argument("--result-schema", default=str(DEFAULT_RESULT_SCHEMA))
    parser.add_argument("--snapshot", help="replay a captured or synthetic snapshot")
    parser.add_argument("--live", action="store_true", help="read and write live GitHub")
    parser.add_argument("--api-base", default=github_graph.DEFAULT_API_BASE)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually write; without it the run re-reads and reports only",
    )
    parser.add_argument(
        "--actor",
        required=True,
        help="identity the writes are made as, recorded in audit.actor",
    )
    parser.add_argument("--proposal-id", help="RoadmapProposal this run is authorized by")
    parser.add_argument("--proposal-url")
    parser.add_argument(
        "--approved-sha256",
        help="approval hash; must equal the manifest digest or the run refuses",
    )
    parser.add_argument(
        "--max-operations",
        type=int,
        default=DEFAULT_MAX_OPERATIONS,
        help=f"per-run blast radius cap (default: {DEFAULT_MAX_OPERATIONS})",
    )
    parser.add_argument(
        "--issue-ids",
        help="JSON map of 'owner/repo#number' to GitHub issue id, for live writes",
    )
    parser.add_argument(
        "--capture",
        help="offline only; write the post-run snapshot here so it can be reconciled",
    )
    parser.add_argument("--output", help="write the result here instead of stdout")
    args = parser.parse_args(argv)

    if bool(args.snapshot) == bool(args.live):
        print("exactly one of --snapshot or --live is required", file=sys.stderr)
        return EXIT_ERROR
    if args.capture and args.live:
        print("--capture describes an offline snapshot, not the live graph", file=sys.stderr)
        return EXIT_ERROR
    if args.max_operations < 0:
        print("--max-operations may not be negative", file=sys.stderr)
        return EXIT_ERROR

    try:
        documents = _run(args)
    except ApplyRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except plan_module.PlanRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except MutationRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (
        ApplyError,
        reconcile.ReconcileError,
        ObservationError,
        SnapshotIncomplete,
        OSError,
        ValueError,
        json.JSONDecodeError,
        SchemaError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    rendered = json.dumps(render(documents), indent=2, sort_keys=True)
    try:
        if args.output:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    return exit_code([document for _, document in documents])


def _run(args: argparse.Namespace) -> list[tuple[Path, dict[str, Any]]]:
    schema = validate._load_schema(Path(args.schema))
    plan_schema = validate._load_schema(Path(args.plan_schema))
    result_schema = validate._load_schema(Path(args.result_schema))
    # Whole-corpus validation before any source is built, and therefore before
    # any network call: the same preflight the reconciler has.
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

    proposal = _proposal(args)
    mode = MODE_EXECUTE if args.execute else MODE_PLAN_ONLY

    snapshot: dict[str, Any] | None = None
    token = ""
    if args.snapshot:
        snapshot = github_graph.load_snapshot(Path(args.snapshot))
    else:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if not token:
            # Auth, not refusal: nothing about the manifest or the graph was
            # judged here, so this is the same exit 2 the reader gives.
            raise ApplyError("live mode requires a GitHub token")

    documents: list[tuple[Path, dict[str, Any]]] = []
    for path in selected:
        loaded = corpus[path]
        # Guard 2: these bytes, at this revision.
        git = resolve_git_context(path, loaded.sha256)
        # Guard 3: approval is bound to content, so a force-push after approval
        # invalidates it automatically.
        if args.approved_sha256 and args.approved_sha256 != loaded.sha256:
            raise ApplyRefused(
                "ApprovalHashMismatch: the approved digest "
                f"{args.approved_sha256} is not the manifest digest {loaded.sha256}"
            )

        authored = frozenset(reconcile.desired_graph(loaded.document).authored_keys())
        if snapshot is not None:
            mutator: github_apply.Mutator = github_apply.FakeMutator(snapshot, authored)
            # One dict, read and written by the same run, so a re-read after a
            # write observes the write -- the offline mirror of live behaviour.
            def source_factory(_snapshot=snapshot) -> Any:
                return github_graph.FixtureGraphSource(_snapshot)
        else:
            mutator = github_apply.LiveMutator(
                token,
                authored,
                api_base=args.api_base,
                resolve_id=_issue_id_resolver(args.issue_ids),
            )

            def source_factory(_token=token, _base=args.api_base) -> Any:
                return github_graph.LiveGraphSource(_token, api_base=_base)

        source_adapter = source_factory()
        document = plan_module.plan_manifest(
            path, loaded, source_adapter, corpus=Path(args.corpus)
        )
        errors = plan_module.schema_errors(document, plan_schema)
        if errors:
            raise ApplyError(
                "plan violates roadmap-apply-plan.schema.json: " + "; ".join(errors)
            )

        operations = apply_plan(
            document,
            source_factory=source_factory,
            mutator=mutator,
            authored=authored,
            execute=args.execute,
            max_operations=args.max_operations,
        )
        documents.append(
            (
                path,
                result(
                    document,
                    operations,
                    mode=mode,
                    actor=args.actor,
                    git_revision=git.revision,
                    proposal=proposal,
                    schema=result_schema,
                ),
            )
        )

    if args.capture and snapshot is not None:
        Path(args.capture).write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return documents


if __name__ == "__main__":
    raise SystemExit(main())
