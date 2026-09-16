#!/usr/bin/env python3
"""Derive a RoadmapHealth state for each epic from a reconciliation run.

Health is computed, never authored. It answers two questions a consumer should not
have to re-derive from a diff: is this epic healthy, and if not, why.

States, in precedence order (the most severe applicable state wins):

    observation_failed > invalid > drift > healthy

The invariant this layer exists to keep:

    Observed absence != unobservable state.

    A missing relationship may be projected as absent only when the
    authoritative source was successfully observed.

So `healthy` is reachable only after a successful observation, a valid desired
state and zero drift, and any failed, partial or unreadable observation outranks
whatever drift the observed remainder shows. Evaluation itself is pure and
performs no I/O; only `assess()` reads a snapshot, through the source it is given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml
from jsonschema.exceptions import SchemaError

import github_graph
import reconcile
import validate
from github_graph import (
    FixtureGraphSource,
    IssueKey,
    ObservationError,
    SnapshotIncomplete,
    observed_graph,
)

API_VERSION = "roadmap.mctl.ai/v1alpha1"

EXIT_HEALTHY = 0
EXIT_DRIFT = 1
EXIT_USAGE = 2
EXIT_INVALID = 3
EXIT_OBSERVATION_FAILED = 4


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DRIFT = "drift"
    INVALID = "invalid"
    OBSERVATION_FAILED = "observation_failed"


# Most severe first. A state later in this tuple can never hide one earlier in it.
PRECEDENCE: tuple[HealthState, ...] = (
    HealthState.OBSERVATION_FAILED,
    HealthState.INVALID,
    HealthState.DRIFT,
    HealthState.HEALTHY,
)

EXIT_CODES = {
    HealthState.HEALTHY: EXIT_HEALTHY,
    HealthState.DRIFT: EXIT_DRIFT,
    HealthState.INVALID: EXIT_INVALID,
    HealthState.OBSERVATION_FAILED: EXIT_OBSERVATION_FAILED,
}

LEVEL_ERROR = "error"
LEVEL_DRIFT = "drift"
LEVEL_INFO = "info"
_LEVEL_ORDER = {LEVEL_ERROR: 0, LEVEL_DRIFT: 1, LEVEL_INFO: 2}

# Diagnostic codes owned by this layer. Drift and informational diagnostics reuse
# the RoadmapDiff entry type as their code.
OBSERVATION_FAILED = "ObservationFailed"
OBSERVATION_MISSING = "ObservationMissing"
SNAPSHOT_INVALID = "SnapshotInvalid"
MANIFEST_INVALID = "ManifestInvalid"
CORPUS_INVALID = "CorpusInvalid"
OBSERVATION_ABSENT = "ObservationAbsent"


@dataclass(frozen=True)
class Diagnostic:
    """One reason contributing to a health state.

    `subject` carries structured evidence -- an issue ref, a diff entry's fields --
    so a consumer can act on it without parsing `message`.
    """

    code: str
    level: str
    message: str
    subject: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        rendered: dict[str, Any] = {
            "code": self.code,
            "level": self.level,
            "message": self.message,
        }
        if self.subject is not None:
            rendered["subject"] = self.subject
        return rendered

    def sort_key(self) -> tuple[Any, ...]:
        return (
            _LEVEL_ORDER[self.level],
            self.code,
            json.dumps(self.subject, sort_keys=True),
            self.message,
        )


def _label(ref: dict[str, Any]) -> str:
    return f"{ref['repository']}#{ref['number']}"


def _diff_diagnostics(diff: dict[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for family in ("binding", "hierarchy", "dependency"):
        for entry in diff[family]:
            subject = {
                key: value
                for key, value in entry.items()
                if key not in ("type", "severity")
            }
            refs = ", ".join(
                f"{key}={_label(value)}"
                for key, value in sorted(subject.items())
                if isinstance(value, dict) and "repository" in value
            )
            owner = subject.get("owner")
            message = entry["type"]
            if owner:
                message += f" (owner {owner})"
            if refs:
                message += f": {refs}"
            level = LEVEL_DRIFT if entry["severity"] == reconcile.DRIFT else LEVEL_INFO
            diagnostics.append(
                Diagnostic(code=entry["type"], level=level, message=message, subject=subject)
            )
    return diagnostics


def evaluate(
    *,
    diff: dict[str, Any] | None = None,
    validation_errors: Sequence[Diagnostic] = (),
    observation_errors: Sequence[Diagnostic] = (),
) -> tuple[HealthState, tuple[Diagnostic, ...]]:
    """Compute a health state and its diagnostics. Pure: no I/O, no clock.

    Every input is kept as a diagnostic even when a more severe one decides the
    state, so an `observation_failed` epic still shows the drift that was seen on
    what could be observed.
    """

    diagnostics: list[Diagnostic] = [*observation_errors, *validation_errors]
    if diff is not None:
        diagnostics.extend(_diff_diagnostics(diff))

    if observation_errors:
        state = HealthState.OBSERVATION_FAILED
    elif validation_errors:
        state = HealthState.INVALID
    elif diff is None:
        # Nothing failed, yet nothing was observed either. Healthy would be a
        # claim about a graph nobody looked at.
        diagnostics.append(
            Diagnostic(
                code=OBSERVATION_ABSENT,
                level=LEVEL_ERROR,
                message="no observation was made, so health cannot be established",
            )
        )
        state = HealthState.OBSERVATION_FAILED
    elif any(item.level == LEVEL_DRIFT for item in diagnostics):
        state = HealthState.DRIFT
    else:
        state = HealthState.HEALTHY

    return state, tuple(sorted(diagnostics, key=Diagnostic.sort_key))


def worst(states: Iterable[HealthState]) -> HealthState:
    present = set(states)
    for state in PRECEDENCE:
        if state in present:
            return state
    return HealthState.HEALTHY


# ------------------------------------------------------------------ documents


def document(
    *,
    epic: dict[str, Any],
    state: HealthState,
    diagnostics: Sequence[Diagnostic],
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rendered: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "kind": "RoadmapHealth",
        "epic": epic,
        "state": state.value,
        "summary": {
            level: sum(1 for item in diagnostics if item.level == level)
            for level in (LEVEL_ERROR, LEVEL_DRIFT, LEVEL_INFO)
        },
        "diagnostics": [item.to_dict() for item in diagnostics],
    }
    if source is not None:
        rendered["source"] = dict(source)
    return rendered


def render(documents: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(documents, key=lambda item: item["epic"]["manifest"]["path"])
    if len(ordered) == 1:
        return ordered[0]
    return {"apiVersion": API_VERSION, "kind": "RoadmapHealthList", "items": ordered}


def _invalid_epic(path: Path, corpus: Path | None) -> dict[str, Any]:
    """Epic identity for a manifest that failed validation, read best-effort."""

    epic: dict[str, Any] = {"manifest": {"path": reconcile._manifest_label(path, corpus)}}
    try:
        raw = path.read_bytes()
    except OSError:
        return epic
    epic["manifest"]["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        parsed = yaml.safe_load(raw.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError):
        return epic
    # The manifest failed validation, so nothing about its shape can be assumed.
    metadata = parsed.get("metadata") if isinstance(parsed, dict) else None
    if isinstance(metadata, dict):
        name = metadata.get("name")
        if isinstance(name, str) and name:
            epic["name"] = name
    return epic


def invalid(
    path: Path,
    corpus: Path | None,
    failures: dict[Path, tuple[str, ...]],
) -> dict[str, Any]:
    """Health of one manifest when the corpus did not validate.

    Nothing is observed for any epic in an invalid corpus: ownership that did not
    validate cannot be compared against a live graph.
    """

    own = failures.get(path, ())
    if own:
        errors = [
            Diagnostic(
                code=MANIFEST_INVALID,
                level=LEVEL_ERROR,
                message=message,
                subject={"manifest": reconcile._manifest_label(path, corpus)},
            )
            for message in own
        ]
    else:
        others = sorted(reconcile._manifest_label(other, corpus) for other in failures)
        errors = [
            Diagnostic(
                code=CORPUS_INVALID,
                level=LEVEL_ERROR,
                message="the corpus this manifest belongs to did not validate",
                subject={"failingManifests": others},
            )
        ]
    state, diagnostics = evaluate(validation_errors=errors)
    return document(epic=_invalid_epic(path, corpus), state=state, diagnostics=diagnostics)


def _epic(path: Path, loaded: reconcile.LoadedManifest, corpus: Path | None) -> dict[str, Any]:
    """Identity of a validated epic -- the same shape on every path."""

    desired = reconcile.desired_graph(loaded.document)
    epic: dict[str, Any] = {
        "name": desired.name,
        "manifest": {
            "path": reconcile._manifest_label(path, corpus),
            "sha256": loaded.sha256,
        },
    }
    if desired.root is not None:
        epic["issue"] = {"repository": desired.root[0], "number": desired.root[1]}
    return epic


def _source_failure(exc: BaseException) -> Diagnostic:
    """Classify why a snapshot could not be obtained, by what failed -- not by adapter.

    A payload that was read but is not a valid snapshot (malformed JSON, schema
    violation) is SnapshotInvalid. Failing to read at all (missing file, transport,
    auth) is ObservationFailed. Both are observation failures in state; the code
    tells a consumer which remediation applies.
    """

    if isinstance(exc, ValueError):  # includes json.JSONDecodeError
        return Diagnostic(code=SNAPSHOT_INVALID, level=LEVEL_ERROR, message=str(exc))
    return Diagnostic(code=OBSERVATION_FAILED, level=LEVEL_ERROR, message=str(exc))


def assess(
    path: Path,
    loaded: reconcile.LoadedManifest,
    source_adapter: Any,
    *,
    corpus: Path | None = None,
) -> dict[str, Any]:
    """Observe one validated manifest through `source_adapter` and evaluate it."""

    desired = reconcile.desired_graph(loaded.document)
    keys = desired.authored_keys()
    epic = _epic(path, loaded, corpus)

    def failed(diagnostic: Diagnostic):
        state, diagnostics = evaluate(observation_errors=[diagnostic])
        return document(epic=epic, state=state, diagnostics=diagnostics)

    try:
        if isinstance(source_adapter, FixtureGraphSource):
            snapshot = source_adapter.snapshot(keys, require_complete=False)
        else:
            snapshot = source_adapter.snapshot(keys)
    except (ObservationError, SnapshotIncomplete, ValueError, OSError) as exc:
        return failed(_source_failure(exc))

    errors = github_graph.snapshot_errors(snapshot)
    if errors:
        return failed(
            Diagnostic(code=SNAPSHOT_INVALID, level=LEVEL_ERROR, message="; ".join(errors))
        )

    observed = {
        github_graph._ref_key(item["requested"]) for item in snapshot.get("issues", [])
    }
    unobserved = frozenset(key for key in keys if key not in observed)
    observation_errors = [
        Diagnostic(
            code=OBSERVATION_MISSING,
            level=LEVEL_ERROR,
            message=f"{repository}#{number} was not observed; it is withheld, not absent",
            subject={"issue": {"repository": repository, "number": number}},
        )
        for repository, number in sorted(unobserved)
    ]

    diff = reconcile.diff(
        desired,
        observed_graph(snapshot),
        manifest_path=epic["manifest"]["path"],
        manifest_sha256=loaded.sha256,
        source=snapshot["source"],
        unobserved=unobserved,
    )
    state, diagnostics = evaluate(diff=diff, observation_errors=observation_errors)
    return document(
        epic=epic, state=state, diagnostics=diagnostics, source=snapshot["source"]
    )


# ------------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "manifests",
        nargs="*",
        help="manifests to assess (default: every manifest in the corpus)",
    )
    parser.add_argument("--corpus", default=str(reconcile.DEFAULT_CORPUS))
    parser.add_argument("--schema", default=str(validate.DEFAULT_SCHEMA))
    parser.add_argument("--snapshot", help="replay a captured or synthetic snapshot")
    parser.add_argument("--live", action="store_true", help="read live GitHub state")
    parser.add_argument("--api-base", default=github_graph.DEFAULT_API_BASE)
    parser.add_argument("--output", help="write the health document here")
    args = parser.parse_args(argv)
    args.capture = None

    if bool(args.snapshot) == bool(args.live):
        print("exactly one of --snapshot or --live is required", file=sys.stderr)
        return EXIT_USAGE

    corpus_root = Path(args.corpus)
    try:
        schema = validate._load_schema(Path(args.schema))
        if not validate._manifest_paths([str(corpus_root)]):
            raise reconcile.ReconcileError(
                f"no EpicDefinition manifests found under {corpus_root}"
            )
        validation = reconcile.validate_corpus(corpus_root, schema)
    except (reconcile.ReconcileError, OSError, ValueError, json.JSONDecodeError, SchemaError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE

    known = set(validation.manifests) | set(validation.failures)
    if args.manifests:
        selected = []
        for raw in args.manifests:
            path = Path(raw).resolve()
            if path not in known:
                print(f"ERROR: {raw} is not part of the corpus at {corpus_root}", file=sys.stderr)
                return EXIT_USAGE
            selected.append(path)
    else:
        selected = sorted(known)

    documents: list[dict[str, Any]] = []
    if validation.failures:
        documents = [invalid(path, corpus_root, validation.failures) for path in selected]
    else:
        try:
            source_adapter = reconcile._build_source(args)
        except (ObservationError, OSError, ValueError, json.JSONDecodeError) as exc:
            for path in selected:
                state, diagnostics = evaluate(observation_errors=[_source_failure(exc)])
                documents.append(
                    document(
                        epic=_epic(path, validation.manifests[path], corpus_root),
                        state=state,
                        diagnostics=diagnostics,
                    )
                )
        else:
            documents = [
                assess(path, validation.manifests[path], source_adapter, corpus=corpus_root)
                for path in selected
            ]

    rendered = json.dumps(render(documents), indent=2, sort_keys=True)
    try:
        if args.output:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE

    return EXIT_CODES[worst(HealthState(item["state"]) for item in documents)]


if __name__ == "__main__":
    raise SystemExit(main())
