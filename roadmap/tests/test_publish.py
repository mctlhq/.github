from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROADMAP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROADMAP / "scripts"))
sys.path.insert(0, str(ROADMAP / "tests"))

import github_graph  # noqa: E402
import mutations  # noqa: E402
import publish  # noqa: E402
import validate  # noqa: E402
from github_graph import FixtureGraphSource, ObservationError  # noqa: E402

HUMAN_INPUT = ROADMAP / "epics" / "human-input.yaml"
UNIFIED_IDENTITY = ROADMAP / "epics" / "unified-identity.yaml"
EPIC_66 = ROADMAP / "epics" / "roadmap-control-plane.yaml"
CONVERGED_HUMAN_INPUT = ROADMAP / "fixtures" / "human-input" / "converged-fixture.json"
CAPTURE_66 = ROADMAP / "fixtures" / "roadmap-control-plane" / "live-capture.json"

REVISION = "0123456789abcdef0123456789abcdef01234567"
PROVENANCE = {
    "evaluator_revision": REVISION,
    "source_repository": "mctlhq/.github",
    "source_ref": "main",
    "source_revision": REVISION,
}


class FailingSource:
    """A source whose observation fails, as a live capture can."""

    def snapshot(self, keys):  # noqa: ANN001 - mirrors the source protocol
        raise ObservationError("GitHub answered 502")


class StaticSource:
    """A source that returns one fixed snapshot, whatever it was asked for."""

    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot

    def snapshot(self, keys):  # noqa: ANN001 - mirrors the source protocol
        return self._snapshot


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status = 200
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        return None


class PublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def corpus(self, *manifests: Path) -> Path:
        corpus = self.root / "epics"
        corpus.mkdir(exist_ok=True)
        for manifest in manifests:
            shutil.copy(manifest, corpus / manifest.name)
        return corpus

    def snapshot(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def build(self, corpus: Path, snapshot: dict) -> dict[str, bytes]:
        return publish.build(corpus, FixtureGraphSource(snapshot), **PROVENANCE)

    # -- composition and determinism ----------------------------------------

    def test_a_publication_is_exactly_the_four_files(self) -> None:
        files = self.build(self.corpus(EPIC_66), self.snapshot(CAPTURE_66))
        self.assertEqual(sorted(publish.PUBLISHED_FILES), sorted(files))

    def test_the_same_manifests_and_snapshot_give_the_same_bytes(self) -> None:
        corpus = self.corpus(EPIC_66, HUMAN_INPUT)
        snapshot = self.snapshot(CAPTURE_66)
        snapshot["issues"] += [
            issue
            for issue in self.snapshot(CONVERGED_HUMAN_INPUT)["issues"]
            if issue["requested"] not in [i["requested"] for i in snapshot["issues"]]
        ]
        first = self.build(corpus, copy.deepcopy(snapshot))
        # Reordering the observed issues must not change a single byte.
        snapshot["issues"].reverse()
        second = self.build(corpus, snapshot)
        self.assertEqual(first[publish.READY_SET_FILE], second[publish.READY_SET_FILE])
        self.assertEqual(first[publish.HEALTH_FILE], second[publish.HEALTH_FILE])

    def test_replaying_the_published_snapshot_reproduces_every_file(self) -> None:
        corpus = self.corpus(EPIC_66)
        files = self.build(corpus, self.snapshot(CAPTURE_66))
        replayed = self.build(corpus, json.loads(files[publish.SNAPSHOT_FILE]))
        self.assertEqual(files, replayed)

    def test_verify_accepts_a_publication_and_rejects_a_tampered_one(self) -> None:
        corpus = self.corpus(EPIC_66)
        output = self.root / "out"
        publish.write(self.build(corpus, self.snapshot(CAPTURE_66)), output)
        self.assertEqual([], publish.verify(output, corpus))

        ready_set = json.loads((output / publish.READY_SET_FILE).read_bytes())
        forged = ready_set["items"][0]["items"][0]
        forged["state"] = "blocked" if forged["state"] == "ready" else "ready"
        (output / publish.READY_SET_FILE).write_bytes(publish.canonical_bytes(ready_set))
        problems = publish.verify(output, corpus)
        self.assertTrue(any(p.startswith("ready-set.json: digest") for p in problems), problems)
        self.assertTrue(any("differs from the evaluator" in p for p in problems), problems)

    def test_the_verify_cli_exits_mismatch_on_any_damage(self) -> None:
        corpus = self.corpus(EPIC_66)
        output = self.root / "out"
        publish.write(self.build(corpus, self.snapshot(CAPTURE_66)), output)

        def run() -> int:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                return publish.main(["verify", str(output), "--corpus", str(corpus)])

        self.assertEqual(publish.EXIT_OK, run())
        for damage in (b"[]", b'{"files": null}', b"null"):
            with self.subTest(damage=damage):
                (output / publish.PUBLICATION_FILE).write_bytes(damage)
                self.assertEqual(publish.EXIT_MISMATCH, run())

    # -- shape --------------------------------------------------------------

    def test_a_single_manifest_publication_is_still_a_list(self) -> None:
        files = self.build(self.corpus(EPIC_66), self.snapshot(CAPTURE_66))
        ready_set = json.loads(files[publish.READY_SET_FILE])
        health_list = json.loads(files[publish.HEALTH_FILE])
        self.assertEqual("RoadmapReadySetList", ready_set["kind"])
        self.assertEqual("RoadmapHealthList", health_list["kind"])
        self.assertEqual(1, len(ready_set["items"]))
        self.assertEqual("RoadmapReadySet", ready_set["items"][0]["kind"])
        self.assertEqual([], validate.schema_errors(
            ready_set, json.loads((ROADMAP / "schemas" / "roadmap-ready-set.schema.json").read_text())))
        self.assertEqual([], validate.schema_errors(
            health_list, json.loads((ROADMAP / "schemas" / "roadmap-health.schema.json").read_text())))

    # -- schema and provenance ----------------------------------------------

    def test_publication_validates_and_carries_its_provenance(self) -> None:
        corpus = self.corpus(EPIC_66, HUMAN_INPUT)
        snapshot = self.snapshot(CAPTURE_66)
        snapshot["issues"] += [
            issue
            for issue in self.snapshot(CONVERGED_HUMAN_INPUT)["issues"]
            if issue["requested"] not in [i["requested"] for i in snapshot["issues"]]
        ]
        files = self.build(corpus, snapshot)
        publication = json.loads(files[publish.PUBLICATION_FILE])
        schema = publish._load_publication_schema()
        self.assertEqual([], validate.schema_errors(publication, schema))
        self.assertEqual(("roadmap.mctl.ai/v1alpha1", "RoadmapPublication"),
                         (publication["apiVersion"], publication["kind"]))
        self.assertEqual({"revision": REVISION}, publication["evaluator"])
        self.assertEqual(
            {"repository": "mctlhq/.github", "ref": "main", "revision": REVISION},
            publication["source"],
        )
        # Every manifest, with the digest of the bytes that were validated.
        recorded = {m["path"].split("/")[-1]: m["sha256"] for m in publication["manifests"]}
        expected = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (EPIC_66, HUMAN_INPUT)
        }
        self.assertEqual(expected, recorded)
        # And the digests of the files it covers.
        for name in publish.DERIVED_FILES:
            self.assertEqual(
                hashlib.sha256(files[name]).hexdigest(), publication["files"][name]["sha256"]
            )

    def test_a_publication_without_an_evaluator_revision_is_refused(self) -> None:
        with self.assertRaises(publish.PublishError) as raised:
            publish.build(
                self.corpus(EPIC_66),
                FixtureGraphSource(self.snapshot(CAPTURE_66)),
                **{**PROVENANCE, "evaluator_revision": "main"},
            )
        self.assertEqual(publish.EXIT_INVALID, raised.exception.code)

    # -- freshness ----------------------------------------------------------

    def test_freshness_is_the_captures_own_timestamp(self) -> None:
        snapshot = self.snapshot(CAPTURE_66)
        publication = json.loads(
            self.build(self.corpus(EPIC_66), snapshot)[publish.PUBLICATION_FILE]
        )
        self.assertEqual(snapshot["source"], publication["observation"])

    def test_a_synthetic_observation_claims_no_capture_time(self) -> None:
        publication = json.loads(
            self.build(self.corpus(HUMAN_INPUT), self.snapshot(CONVERGED_HUMAN_INPUT))[
                publish.PUBLICATION_FILE
            ]
        )
        self.assertEqual({"mode": "synthetic-fixture"}, publication["observation"])

    def test_a_failed_observation_leaves_the_previous_publication_untouched(self) -> None:
        corpus = self.corpus(EPIC_66)
        output = self.root / "out"
        publish.write(self.build(corpus, self.snapshot(CAPTURE_66)), output)
        before = {name: (output / name).read_bytes() for name in publish.PUBLISHED_FILES}

        with self.assertRaises(publish.PublishError) as raised:
            publish.build(corpus, FailingSource(), **PROVENANCE)
        self.assertEqual(publish.EXIT_OBSERVATION_FAILED, raised.exception.code)
        after = {name: (output / name).read_bytes() for name in publish.PUBLISHED_FILES}
        self.assertEqual(before, after)

    def test_the_cli_writes_nothing_when_observation_fails(self) -> None:
        corpus = self.corpus(EPIC_66)
        output = self.root / "out"
        incomplete = self.snapshot(CAPTURE_66)
        incomplete["issues"] = incomplete["issues"][1:]
        snapshot_path = self.root / "incomplete.json"
        snapshot_path.write_text(json.dumps(incomplete), encoding="utf-8")
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = publish.main([
                "build", "--corpus", str(corpus), "--snapshot", str(snapshot_path),
                "--output", str(output),
                "--evaluator-revision", REVISION, "--source-repository", "mctlhq/.github",
                "--source-ref", "main", "--source-revision", REVISION,
            ])
        self.assertEqual(publish.EXIT_OBSERVATION_FAILED, code)
        self.assertFalse(output.exists())

    def test_a_snapshot_missing_a_key_is_never_published(self) -> None:
        # A source that hands back less than it was asked for, without raising.
        partial = self.snapshot(CAPTURE_66)
        partial["issues"] = partial["issues"][1:]
        with self.assertRaises(publish.PublishError) as raised:
            publish.build(self.corpus(EPIC_66), StaticSource(partial), **PROVENANCE)
        self.assertEqual(publish.EXIT_OBSERVATION_FAILED, raised.exception.code)
        self.assertIn("did not observe", str(raised.exception))

    def test_an_invalid_snapshot_is_never_published(self) -> None:
        # The same issue observed twice: replay would silently keep the last one.
        duplicated = self.snapshot(CAPTURE_66)
        duplicated["issues"].append(copy.deepcopy(duplicated["issues"][0]))
        with self.assertRaises(publish.PublishError) as raised:
            publish.build(self.corpus(EPIC_66), StaticSource(duplicated), **PROVENANCE)
        self.assertEqual(publish.EXIT_OBSERVATION_FAILED, raised.exception.code)
        # Refused as an observation, before anything is evaluated from it.
        self.assertTrue(str(raised.exception).startswith("snapshot is invalid"), raised.exception)

    def test_a_repository_the_token_cannot_see_fails_the_observation(self) -> None:
        class BlindLiveSource(github_graph.LiveGraphSource):
            snapshots = 0

            def check_repositories(self, repositories):  # noqa: ANN001
                raise ObservationError("repository mctlhq/.github is not visible to this token (HTTP 404)")

            def snapshot(self, keys):  # noqa: ANN001
                BlindLiveSource.snapshots += 1
                return {}

        with self.assertRaises(publish.PublishError) as raised:
            publish.build(self.corpus(EPIC_66), BlindLiveSource("token"), **PROVENANCE)
        self.assertEqual(publish.EXIT_OBSERVATION_FAILED, raised.exception.code)
        self.assertEqual(0, BlindLiveSource.snapshots, "the capture must not start")

    def test_the_live_source_tells_an_invisible_repository_from_a_missing_issue(self) -> None:
        class Opener:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def open(self, request, timeout=None):  # noqa: ANN001
                self.urls.append(request.full_url)
                if request.full_url.endswith("/repos/mctlhq/.github"):
                    return _Response(b'{"full_name": "mctlhq/.github", "has_issues": true}')
                if request.full_url.endswith("/repos/mctlhq/no-issues"):
                    return _Response(b'{"full_name": "mctlhq/no-issues", "has_issues": false}')
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        opener = Opener()
        source = github_graph.LiveGraphSource("token", opener=opener)
        source.check_repositories(["mctlhq/.github", "mctlhq/.github"])
        with self.assertRaises(ObservationError):
            source.check_repositories(["mctlhq/.github", "mctlhq/private-now"])
        with self.assertRaises(ObservationError):
            source.check_repositories(["mctlhq/no-issues"])
        self.assertEqual(
            ["https://api.github.com/repos/mctlhq/.github"] * 2
            + ["https://api.github.com/repos/mctlhq/private-now",
               "https://api.github.com/repos/mctlhq/no-issues"],
            opener.urls,
        )
        self.assertTrue(all(url.startswith("https://api.github.com/repos/") for url in opener.urls))

    # -- unbound vs unknown -------------------------------------------------

    def test_unbound_and_unobservable_stay_distinguishable(self) -> None:
        corpus = self.corpus(HUMAN_INPUT, UNIFIED_IDENTITY)
        # An observed-but-missing issue: bound, yet its state cannot be proven.
        snapshot = mutations.mark_missing(
            self.snapshot(CONVERGED_HUMAN_INPUT), "mctlhq/mctl-api#261"
        )
        # unified-identity's root issue has to be observed for the epic itself.
        snapshot["issues"].append({
            "blockedBy": [], "found": True, "parent": None,
            "requested": {"number": 91, "repository": "mctlhq/.github"},
            "resolved": {"number": 91, "repository": "mctlhq/.github"},
            "state": "open", "subIssues": [],
        })
        errors = github_graph.snapshot_errors(snapshot)
        self.assertEqual([], errors)
        ready_set = json.loads(self.build(corpus, snapshot)[publish.READY_SET_FILE])
        items = {
            item["id"]: item for document in ready_set["items"] for item in document["items"]
        }
        self.assertEqual(("unknown", "unbound"),
                         (items["principal-model"]["state"],
                          items["principal-model"]["completion"]["reason"]))
        self.assertEqual("unknown", items["human-input-api"]["state"])
        self.assertNotEqual("unbound", items["human-input-api"]["completion"]["reason"])

    # -- the planning source is never written -------------------------------

    def test_publishing_never_rewrites_the_manifests(self) -> None:
        corpus = self.corpus(EPIC_66)
        before = {p.name: p.read_bytes() for p in corpus.iterdir()}
        publish.write(self.build(corpus, self.snapshot(CAPTURE_66)), self.root / "out")
        self.assertEqual(before, {p.name: p.read_bytes() for p in corpus.iterdir()})
        self.assertEqual(sorted(publish.PUBLISHED_FILES),
                         sorted(p.name for p in (self.root / "out").iterdir()))

    def test_an_invalid_corpus_is_never_published(self) -> None:
        corpus = self.corpus(EPIC_66)
        (corpus / "broken.yaml").write_text("apiVersion: nope\n", encoding="utf-8")
        with self.assertRaises(publish.PublishError) as raised:
            self.build(corpus, self.snapshot(CAPTURE_66))
        self.assertEqual(publish.EXIT_INVALID, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
