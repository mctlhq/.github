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
of the manifest, and every write target an owned one; the run -- all selected
manifests together, not each one -- must fit inside `--max-operations`; and a
live run must already hold an issue id for every operation it plans.

All of that is phase one, and it happens for every selected manifest before the
first mutation is transmitted. Phase two writes, and appends each manifest's
`RoadmapApplyResult` as it completes, so a failure part-way through still emits
a record of what preceded it: a write that landed and was never recorded is the
one outcome this tool may not produce.
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
    max_operations: int | None = DEFAULT_MAX_OPERATIONS,
    results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Run every operation in a plan, in plan order.

    `max_operations=None` means the cap was already enforced by the caller. A
    multi-manifest run has to sum plan sizes before the first write to cap the
    run rather than each manifest, so `_run` checks it there and passes None
    here; a direct caller of this function still gets the default cap.

    `results`, when given, is the list each operation's record is appended to
    the moment it completes, rather than a list assembled only on return. A
    caller that holds the same reference still sees every operation that
    completed before one of them raised -- a write that landed and was never
    recorded is the one outcome this tool may not produce, and a list built
    entirely inside a comprehension that never finishes cannot honor that.
    """

    if document["refusals"]:
        rendered = ", ".join(
            f"{refusal['reason']} ({refusal['owner']})"
            for refusal in document["refusals"]
        )
        raise ApplyRefused(f"the plan refuses this manifest: {rendered}")

    operations = document["operations"]
    if max_operations is not None and len(operations) > max_operations:
        raise ApplyRefused(
            f"{len(operations)} operations exceeds --max-operations "
            f"({max_operations}); refusing rather than applying part of a plan"
        )
    for operation in operations:
        plan_module.assert_authored(operation, authored)

    if results is None:
        results = []
    for operation in operations:
        results.append(
            run_operation(
                operation,
                source_factory=source_factory,
                mutator=mutator,
                authored=authored,
                execute=execute,
            )
        )
    return results


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
    manifests_selected: int,
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
            # How many manifests the run SELECTED, recorded on every result so
            # that a truncated run is legible from the artifact alone. Without
            # it, a corpus run that applied manifest 1 and then raised on
            # manifest 2 emits exactly one bare RoadmapApplyResult -- byte
            # identical to a clean single-manifest run, with the process exit
            # code as the only signal, and that is not in the file the operator
            # archives. Comparing this against the number of results present
            # answers "did the run finish?" without it.
            "manifestsSelected": manifests_selected,
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

    # `_run` appends into this list as it goes, rather than returning it at the
    # end, so that a run which raises part-way through still hands back what it
    # already did. A write that landed on GitHub and was never recorded is the
    # one outcome this tool may not produce: the audit record is the only
    # evidence the operator has, and "REFUSED, no output" reads as zero writes.
    documents: list[tuple[Path, dict[str, Any]]] = []
    try:
        _run(args, documents)
    except (ApplyRefused, plan_module.PlanRefused, MutationRefused) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        _emit(documents, args)
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
        _emit(documents, args)
        return EXIT_ERROR
    except BaseException:
        # Ctrl-C from an operator watching a long `--live --execute` run, a CI
        # cancellation delivering SIGINT, `SystemExit` -- none of them is a
        # licence to drop the record of writes that already landed. The two
        # tuples above do not catch them, and `documents` is exactly non-empty
        # in the case that matters: part-way through the corpus.
        #
        # A handler rather than a `finally`, because the success path below has
        # to be able to turn a failed emission into EXIT_ERROR, which a
        # `finally` cannot do without swallowing the interrupt. `raise`
        # preserves the status Python gives the interrupt, and re-emitting is
        # harmless -- `_emit` is silent on an empty list.
        _emit(documents, args)
        raise

    if not _emit(documents, args):
        return EXIT_ERROR
    return exit_code([document for _, document in documents])


def _emit(
    documents: list[tuple[Path, dict[str, Any]]], args: argparse.Namespace
) -> bool:
    """Serialize the result, if there is one. Returns False on an IO failure.

    Called on every path out of `main`, including the refusal and error paths,
    where it may be emitting a partial record of a run that stopped early. An
    empty document list is not an error -- a guard that fired before the first
    manifest was planned genuinely has nothing to report -- so it is silent.
    """

    if not documents:
        return True
    rendered = json.dumps(render(documents), indent=2, sort_keys=True)
    try:
        if args.output:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return False
    return True


@dataclass(frozen=True)
class _Prepared:
    """One manifest, planned and guarded, with nothing written yet."""

    path: Path
    document: dict[str, Any]
    mutator: github_apply.Mutator
    source_factory: Any
    owned: frozenset[IssueKey]
    authored: frozenset[IssueKey]
    git_revision: str


def _related_endpoint(operation: dict[str, Any]) -> IssueKey:
    """The identity a write names by numeric id, rather than in its path.

    Every one of the four permitted writes carries exactly one such end, and it
    is the only end a live run needs an id for. `MoveSubIssue` decomposes into
    two writes that both name the child, so one key covers it.
    """

    if operation["type"] in (plan_module.ADD_SUB_ISSUE, plan_module.MOVE_SUB_ISSUE):
        return _key(operation["child"])
    return _key(operation["blocker"])


def _operation_targets(operation: dict[str, Any]) -> list[IssueKey]:
    """The identity (or identities) a write is made *against*, by path.

    This is `check_request`'s `request.target`, computed here so the same
    boundary can be asserted before any write is attempted, not only at
    transmission. `AddSubIssue`/`AddDependency`/`RemoveDependency` each target
    one key; `MoveSubIssue` decomposes into two writes -- a remove against
    `observedParent` and an add against `parent` -- so it names both.
    """

    if operation["type"] == plan_module.ADD_SUB_ISSUE:
        return [_key(operation["parent"])]
    if operation["type"] == plan_module.MOVE_SUB_ISSUE:
        return [_key(operation["observedParent"]), _key(operation["parent"])]
    return [_key(operation["blocked"])]


def assert_owned_targets(
    operation: dict[str, Any], owned: frozenset[IssueKey]
) -> None:
    """Refuse any operation whose write target is not in the owned set.

    `plan_module.assert_authored` holds every endpoint of an operation to the
    wider authored boundary. `check_request` holds the narrower one at
    transmission: the issue a write is made against must be owned, not merely
    authored -- `externalDependsOn` is authority to depend on a third party's
    issue, never to edit it. Asserting it again here, in phase 1, means a
    `MoveSubIssue` out of a foreign `observedParent` refuses the run before the
    first write of any selected manifest, the same as every other guard.
    """

    foreign = [key for key in _operation_targets(operation) if key not in owned]
    if foreign:
        rendered = ", ".join(f"{key[0]}#{key[1]}" for key in foreign)
        raise ApplyRefused(
            f"{operation['type']} writes against an identity outside the "
            f"owned set: {rendered}"
        )


def _prepare(
    args: argparse.Namespace,
    path: Path,
    loaded: Any,
    plan_schema: dict[str, Any],
    snapshot: dict[str, Any] | None,
    token: str,
    resolve_id: Any,
) -> _Prepared:
    """Guard and plan one manifest. Performs no write and opens no write path."""

    # Guard 2: these bytes, at this revision.
    git = resolve_git_context(path, loaded.sha256)
    # Guard 3: approval is bound to content, so a force-push after approval
    # invalidates it automatically.
    if args.approved_sha256 and args.approved_sha256 != loaded.sha256:
        raise ApplyRefused(
            "ApprovalHashMismatch: the approved digest "
            f"{args.approved_sha256} is not the manifest digest {loaded.sha256}"
        )

    desired = reconcile.desired_graph(loaded.document)
    # Two boundaries, not one. `owned` is what may be written *against*;
    # `authored` additionally holds the issues the manifest only names in
    # `externalDependsOn`, which may be the related end of a write but never
    # its target. See `github_apply.check_request`.
    owned = frozenset(desired.owned_keys())
    authored = frozenset(desired.authored_keys())
    if snapshot is not None:
        mutator: github_apply.Mutator = github_apply.FakeMutator(
            snapshot, owned, authored
        )

        # One dict, read and written by the same run, so a re-read after a
        # write observes the write -- the offline mirror of live behaviour.
        def source_factory(_snapshot=snapshot) -> Any:
            return github_graph.FixtureGraphSource(_snapshot)
    else:
        mutator = github_apply.LiveMutator(
            token,
            owned,
            api_base=args.api_base,
            resolve_id=resolve_id,
            authored=authored,
        )

        def source_factory(_token=token, _base=args.api_base) -> Any:
            return github_graph.LiveGraphSource(_token, api_base=_base)

    document = plan_module.plan_manifest(
        path, loaded, source_factory(), corpus=Path(args.corpus)
    )
    errors = plan_module.schema_errors(document, plan_schema)
    if errors:
        raise ApplyError(
            "plan violates roadmap-apply-plan.schema.json: " + "; ".join(errors)
        )
    if document["refusals"]:
        rendered = ", ".join(
            f"{refusal['reason']} ({refusal['owner']})"
            for refusal in document["refusals"]
        )
        raise ApplyRefused(f"the plan refuses this manifest: {rendered}")
    for operation in document["operations"]:
        plan_module.assert_authored(operation, authored)
        assert_owned_targets(operation, owned)

    return _Prepared(
        path=path,
        document=document,
        mutator=mutator,
        source_factory=source_factory,
        owned=owned,
        authored=authored,
        git_revision=git.revision,
    )


def _check_issue_ids(prepared: list[_Prepared], resolve_id: Any) -> None:
    """Every id a live run will need, resolved before the first write.

    The resolver raises mid-write otherwise, and a `MutationRefused` escaping
    from operation 5 of 9 is precisely the shape that used to leave four real
    mutations on GitHub with no audit record. Asking for the ids up front turns
    an incomplete `--issue-ids` file back into a guard.
    """

    if resolve_id is None:
        return
    missing: list[str] = []
    for item in prepared:
        for operation in item.document["operations"]:
            key = _related_endpoint(operation)
            try:
                resolve_id(key)
            except MutationRefused:
                label = f"{key[0]}#{key[1]}"
                if label not in missing:
                    missing.append(label)
    if missing:
        raise ApplyRefused(
            "--issue-ids is missing an id for " + ", ".join(sorted(missing))
        )


def _run(
    args: argparse.Namespace,
    documents: list[tuple[Path, dict[str, Any]]],
) -> None:
    """Plan everything, guard everything, and only then write anything.

    The two phases are the point. Every guard -- corpus validation, git context,
    approval digest, plan schema, plan refusals, the authored assertion, the
    run-wide operation cap and the live id map -- is evaluated for *all*
    selected manifests before the first mutation is transmitted, so a guard that
    fires cannot fire after a sibling manifest has already been applied. Results
    are appended to `documents` as each manifest completes, so a failure inside
    the write phase still leaves an audit record of what preceded it.
    """

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
    resolve_id = None
    if args.snapshot:
        snapshot = github_graph.load_snapshot(Path(args.snapshot))
    else:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if not token:
            # Auth, not refusal: nothing about the manifest or the graph was
            # judged here, so this is the same exit 2 the reader gives.
            raise ApplyError("live mode requires a GitHub token")
        resolve_id = _issue_id_resolver(args.issue_ids)

    # -- phase 1: plan and guard, no writes ------------------------------
    prepared = [
        _prepare(args, path, corpus[path], plan_schema, snapshot, token, resolve_id)
        for path in selected
    ]

    # The cap is on the run, not on each manifest: with the whole corpus
    # selected, a per-manifest cap of 25 is a real ceiling of 25 x N.
    planned = sum(len(item.document["operations"]) for item in prepared)
    if planned > args.max_operations:
        raise ApplyRefused(
            f"{planned} operations across {len(prepared)} manifest(s) exceeds "
            f"--max-operations ({args.max_operations}); refusing rather than "
            "applying part of a run"
        )
    if args.execute:
        _check_issue_ids(prepared, resolve_id)

    # -- phase 2: write --------------------------------------------------
    # Whether phase 2 came back normally, read by the capture `finally` below
    # to tell "the capture failed" from "the capture failed while something
    # else was already unwinding".
    phase_two_returned = False
    try:
        for item in prepared:
            operations: list[dict[str, Any]] = []
            completed = False
            try:
                apply_plan(
                    item.document,
                    source_factory=item.source_factory,
                    mutator=item.mutator,
                    authored=item.authored,
                    execute=args.execute,
                    # Already enforced run-wide above.
                    max_operations=None,
                    results=operations,
                )
                completed = True
            finally:
                # `operations` already holds a record for every operation that
                # completed, even the ones before whichever operation raised --
                # `apply_plan` appends into it as it goes. So the manifest's
                # result is built and appended here too, not only after a
                # normal return: a write that landed on GitHub for operation N
                # must not go unrecorded just because operation N+1 raised.
                # A manifest that raised before recording anything (a guard
                # tripped by `apply_plan` itself, or the very first operation)
                # still has nothing to report, same as before this fix.
                if completed or operations:
                    # Non-throwing on purpose. `result` raises `ApplyError` on
                    # its leak and schema checks, and this call sits in a
                    # `finally`: an `ApplyError` raised while a
                    # `MutationRefused` is in flight would REPLACE it, so the
                    # refusal never reaches stderr, the exit code flips from
                    # refused to error, and the append never runs -- losing the
                    # partial record this block exists to preserve. Report and
                    # let the original exception continue unwinding.
                    try:
                        documents.append(
                            (
                                item.path,
                                result(
                                    item.document,
                                    operations,
                                    mode=mode,
                                    actor=args.actor,
                                    git_revision=item.git_revision,
                                    manifests_selected=len(prepared),
                                    proposal=proposal,
                                    schema=result_schema,
                                ),
                            )
                        )
                    except ApplyError as exc:
                        if completed:
                            # Nothing is unwinding -- this IS the failure, and
                            # a result that does not validate must not be
                            # reported as a clean run.
                            raise
                        print(
                            f"ERROR: the result for {item.path} could not be "
                            f"built: {exc}",
                            file=sys.stderr,
                        )
        phase_two_returned = True
    finally:
        # The snapshot is mutated in place by every applied write, so it is
        # worth capturing even from a run that stopped early -- that is when
        # knowing the post-run state matters most.
        #
        # Non-throwing for the same reason as the result append above: an
        # `OSError` from `write_text` raised in a `finally` would mask whatever
        # phase 2 was unwinding, and a missing capture file is the lesser loss.
        if args.capture and snapshot is not None:
            try:
                Path(args.capture).write_text(
                    json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                if phase_two_returned:
                    raise
                print(f"ERROR: the capture could not be written: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
