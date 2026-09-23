#!/usr/bin/env python3
"""Publish one versioned RoadmapPublication for the whole canonical corpus.

`ready.py` and `health.py` answer per invocation and print. Consumers such as
mctl-api (mctlhq/mctl-api#333) must not run them: they read one published
answer instead, so every consumer that reads the same publication sees the same
readiness (mctlhq/.github#119).

A publication is four files written together:

    snapshot.json     the single GitHubGraphSnapshot everything is derived from
    ready-set.json    RoadmapReadySetList over every manifest in the corpus
    health.json       RoadmapHealthList over every manifest in the corpus
    publication.json  RoadmapPublication: provenance and digests of the other three

The observation happens exactly once. A live run captures the union of every
manifest's authored issue keys in one pass, and the ready set and health are
then derived by replaying that snapshot, never by reading GitHub again. So the
three derived files are a pure function of (manifests, snapshot): replaying
`snapshot.json` at the recorded revision reproduces them byte for byte, and
`verify` checks exactly that.

There is no wall clock in the output. Freshness is the snapshot's own
`capturedAt`; a run that fails to observe or evaluate writes nothing, so the
previous publication keeps its older `capturedAt` and cannot look fresher than
the last observation that actually succeeded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

import github_graph
import health
import ready
import reconcile
import validate
from github_graph import FixtureGraphSource, ObservationError, SnapshotIncomplete

API_VERSION = "roadmap.mctl.ai/v1alpha1"
KIND = "RoadmapPublication"

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLICATION_SCHEMA = ROOT / "schemas" / "roadmap-publication.schema.json"

SNAPSHOT_FILE = "snapshot.json"
READY_SET_FILE = "ready-set.json"
HEALTH_FILE = "health.json"
PUBLICATION_FILE = "publication.json"
DERIVED_FILES = (SNAPSHOT_FILE, READY_SET_FILE, HEALTH_FILE)
PUBLISHED_FILES = (*DERIVED_FILES, PUBLICATION_FILE)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INVALID = 3
EXIT_OBSERVATION_FAILED = 4
EXIT_MISMATCH = 5


class PublishError(Exception):
    """A publication could not be produced; carries the exit code to use."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_bytes(document: dict[str, Any]) -> bytes:
    """The one serialization every published file uses."""

    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_publication_schema(path: Path = DEFAULT_PUBLICATION_SCHEMA) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return schema


def _validated_corpus(corpus: Path, schema_path: Path) -> reconcile.CorpusValidation:
    try:
        schema = validate._load_schema(schema_path)
        validation = reconcile.validate_corpus(corpus, schema)
    except (reconcile.ReconcileError, OSError, ValueError, json.JSONDecodeError, SchemaError) as exc:
        raise PublishError(EXIT_USAGE, str(exc)) from exc
    if validation.failures:
        rendered = "; ".join(
            f"{path}: {message}"
            for path in sorted(validation.failures)
            for message in validation.failures[path]
        )
        # An invalid corpus is never published: a consumer would otherwise read
        # readiness derived from a desired graph nobody could validate.
        raise PublishError(EXIT_INVALID, f"corpus validation failed: {rendered}")
    return validation


def _union_keys(validation: reconcile.CorpusValidation) -> list[github_graph.IssueKey]:
    keys: set[github_graph.IssueKey] = set()
    for loaded in validation.manifests.values():
        keys |= set(reconcile.desired_graph(loaded.document).authored_keys())
    return sorted(keys)


def build(
    corpus: Path,
    source_adapter: Any,
    *,
    evaluator_revision: str,
    source_repository: str,
    source_ref: str,
    source_revision: str,
    schema_path: Path = validate.DEFAULT_SCHEMA,
    publication_schema: dict[str, Any] | None = None,
) -> dict[str, bytes]:
    """Observe once, derive everything from that one snapshot, return file bytes.

    Nothing is written here; `write` publishes the result only if this returns.
    """

    validation = _validated_corpus(corpus, schema_path)
    selected = sorted(validation.manifests)

    keys = _union_keys(validation)
    try:
        # A live source first proves it can see every repository: otherwise a
        # repository the token lost sight of reads as every issue in it gone.
        # Keyed on the type, not on duck typing, so a renamed method fails
        # loudly instead of skipping the guard.
        if isinstance(source_adapter, github_graph.LiveGraphSource):
            source_adapter.check_repositories([repository for repository, _ in keys])
        snapshot = source_adapter.snapshot(keys)
    except (ObservationError, SnapshotIncomplete, ValueError, OSError) as exc:
        raise PublishError(EXIT_OBSERVATION_FAILED, f"observation failed: {exc}") from exc
    errors = github_graph.snapshot_errors(snapshot)
    if errors:
        raise PublishError(EXIT_OBSERVATION_FAILED, "snapshot is invalid: " + "; ".join(errors))

    return derive(
        corpus,
        validation,
        selected,
        snapshot,
        evaluator_revision=evaluator_revision,
        source_repository=source_repository,
        source_ref=source_ref,
        source_revision=source_revision,
        publication_schema=publication_schema,
    )


def derive(
    corpus: Path,
    validation: reconcile.CorpusValidation,
    selected: list[Path],
    snapshot: dict[str, Any],
    *,
    evaluator_revision: str,
    source_repository: str,
    source_ref: str,
    source_revision: str,
    publication_schema: dict[str, Any] | None = None,
) -> dict[str, bytes]:
    """Pure: the published bytes for one corpus and one snapshot."""

    replay = FixtureGraphSource(snapshot)
    ready_schema = ready._load_ready_schema()
    ready_documents: list[dict[str, Any]] = []
    health_documents: list[dict[str, Any]] = []
    try:
        for path in selected:
            loaded = validation.manifests[path]
            result, unobserved = ready.assess(
                path, loaded, replay, corpus=corpus, ready_schema=ready_schema
            )
            if unobserved:
                raise PublishError(
                    EXIT_OBSERVATION_FAILED,
                    f"{path}: the snapshot did not observe {sorted(unobserved)}",
                )
            ready_documents.append(result)
            health_documents.append(health.assess(path, loaded, replay, corpus=corpus))
    except (ObservationError, SnapshotIncomplete, ValueError, OSError) as exc:
        raise PublishError(EXIT_OBSERVATION_FAILED, f"evaluation failed: {exc}") from exc

    files = {
        SNAPSHOT_FILE: canonical_bytes(snapshot),
        READY_SET_FILE: canonical_bytes(_as_list("RoadmapReadySetList", ready_documents)),
        HEALTH_FILE: canonical_bytes(_as_list("RoadmapHealthList", health_documents)),
    }

    source = snapshot["source"]
    observation: dict[str, Any] = {"mode": source["mode"]}
    for key in ("capturedAt", "apiBase"):
        if key in source:
            observation[key] = source[key]
    publication = {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "evaluator": {"revision": evaluator_revision},
        "source": {
            "repository": source_repository,
            "ref": source_ref,
            "revision": source_revision,
        },
        "manifests": [
            {
                "path": reconcile._manifest_label(path, corpus),
                "sha256": validation.manifests[path].sha256,
            }
            for path in selected
        ],
        "observation": observation,
        "files": {name: {"sha256": sha256(data)} for name, data in sorted(files.items())},
    }
    schema = publication_schema if publication_schema is not None else _load_publication_schema()
    failures = validate.schema_errors(publication, schema)
    if failures:
        raise PublishError(EXIT_INVALID, "publication failed schema validation: " + "; ".join(failures))
    files[PUBLICATION_FILE] = canonical_bytes(publication)
    return files


def _as_list(kind: str, documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Always the List form, even for one manifest.

    `ready.render` and `health.render` return a bare document for a single
    manifest, which suits a CLI. A published file has a consumer, so its kind
    and the meaning of `items` must not change with the size of the corpus.
    """

    ordered = sorted(documents, key=lambda item: item["epic"]["manifest"]["path"])
    return {"apiVersion": API_VERSION, "kind": kind, "items": ordered}


def write(files: dict[str, bytes], output: Path) -> None:
    """Write `files` into `output`, replacing any previous publication.

    Called only after `build` succeeded, so a failed observation or evaluation
    never touches `output`. Staging in a sibling directory keeps a failed write
    (disk full, permissions) from leaving a truncated file behind. The unit a
    consumer sees atomically is the single commit the publish workflow makes
    from this directory, not this function. Only the published names are
    touched; anything else in `output` is left alone.
    """

    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as raw:
        staging = Path(raw)
        for name, data in files.items():
            (staging / name).write_bytes(data)
        for name in files:
            os.replace(staging / name, output / name)


def verify(publication_dir: Path, corpus: Path, schema_path: Path = validate.DEFAULT_SCHEMA) -> list[str]:
    """Recompute a publication from its own snapshot and report every difference.

    The corpus must be the one at the recorded revision. Empty means the
    publication is exactly what the evaluator produces for its snapshot.
    """

    problems: list[str] = []
    try:
        publication = json.loads((publication_dir / PUBLICATION_FILE).read_bytes())
        snapshot = json.loads((publication_dir / SNAPSHOT_FILE).read_bytes())
    except (OSError, ValueError) as exc:
        return [f"unreadable publication: {exc}"]
    # Shape first: everything below indexes into these documents.
    shape = validate.schema_errors(publication, _load_publication_schema())
    if shape:
        return [f"{PUBLICATION_FILE}: {error}" for error in shape]
    if not isinstance(snapshot, dict):
        return [f"{SNAPSHOT_FILE}: not a JSON object"]
    for name in DERIVED_FILES:
        try:
            data = (publication_dir / name).read_bytes()
        except OSError as exc:
            problems.append(f"{name}: {exc}")
            continue
        recorded = publication["files"][name]["sha256"]
        if recorded != sha256(data):
            problems.append(f"{name}: digest {sha256(data)} does not match the recorded {recorded}")
    try:
        validation = _validated_corpus(corpus, schema_path)
        recomputed = derive(
            corpus,
            validation,
            sorted(validation.manifests),
            snapshot,
            evaluator_revision=publication["evaluator"]["revision"],
            source_repository=publication["source"]["repository"],
            source_ref=publication["source"]["ref"],
            source_revision=publication["source"]["revision"],
        )
    except (PublishError, KeyError) as exc:
        return problems + [f"cannot recompute: {exc}"]
    for name, data in recomputed.items():
        try:
            if (publication_dir / name).read_bytes() != data:
                problems.append(f"{name}: differs from the evaluator's output for this snapshot")
        except OSError as exc:
            problems.append(f"{name}: {exc}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("build", help="observe once and write a publication")
    run.add_argument("--corpus", default=str(reconcile.DEFAULT_CORPUS))
    run.add_argument("--schema", default=str(validate.DEFAULT_SCHEMA))
    run.add_argument("--snapshot", help="derive from a captured or synthetic snapshot")
    run.add_argument("--live", action="store_true", help="capture live GitHub state (GET only)")
    run.add_argument("--api-base", default=github_graph.DEFAULT_API_BASE)
    run.add_argument("--output", required=True, help="directory to write the publication into")
    run.add_argument("--evaluator-revision", required=True)
    run.add_argument("--source-repository", required=True)
    run.add_argument("--source-ref", required=True)
    run.add_argument("--source-revision", required=True)

    check = sub.add_parser("verify", help="recompute a publication from its snapshot")
    check.add_argument("publication", help="directory holding a publication")
    check.add_argument("--corpus", default=str(reconcile.DEFAULT_CORPUS))
    check.add_argument("--schema", default=str(validate.DEFAULT_SCHEMA))

    args = parser.parse_args(argv)

    if args.command == "verify":
        problems = verify(Path(args.publication), Path(args.corpus), Path(args.schema))
        for problem in problems:
            print(f"MISMATCH: {problem}", file=sys.stderr)
        return EXIT_MISMATCH if problems else EXIT_OK

    args.capture = None
    if bool(args.snapshot) == bool(args.live):
        print("exactly one of --snapshot or --live is required", file=sys.stderr)
        return EXIT_USAGE
    try:
        source_adapter = reconcile._build_source(args)
    except (reconcile.ReconcileError, ObservationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_OBSERVATION_FAILED
    try:
        files = build(
            Path(args.corpus),
            source_adapter,
            evaluator_revision=args.evaluator_revision,
            source_repository=args.source_repository,
            source_ref=args.source_ref,
            source_revision=args.source_revision,
            schema_path=Path(args.schema),
        )
        write(files, Path(args.output))
    except PublishError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
