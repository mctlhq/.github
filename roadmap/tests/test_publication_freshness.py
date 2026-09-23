"""Publication freshness: who may ask for a publication, and when one may land.

Three pieces, one contract (roadmap/README.md, "Publication freshness"):

* `publication_request` sends exactly one request, the publisher's
  `workflow_dispatch`, and a failure leaves the publication as old as it was;
* `apply.py` sends it after a live run in which a write landed, and never after
  a plan-only run, a replay, or a live run that wrote nothing;
* `publication_order` keeps `capturedAt` from moving backwards and lets the
  schedule skip only when the publication is both current and young.

`publish.capture_cost` is checked against the GETs the live reader really
makes, because the workflow's budget preflight trusts it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

ROADMAP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROADMAP / "scripts"))

import apply as apply_module  # noqa: E402
import github_graph  # noqa: E402
import publication_order  # noqa: E402
import publication_request  # noqa: E402
import publish  # noqa: E402
import reconcile  # noqa: E402
import validate  # noqa: E402

DISPATCH_URL = (
    "https://api.github.com/repos/mctlhq/.github/actions/workflows/"
    "roadmap-publish.yml/dispatches"
)


class _Response:
    def __init__(self, body: bytes = b"", status: int = 204, headers=None) -> None:
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        return None


class _Opener:
    """Records every request and answers with a fixed response or error."""

    def __init__(self, answer) -> None:  # noqa: ANN001
        self.answer = answer
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def open(self, request, timeout=None):  # noqa: ANN001, ANN201
        self.calls.append(
            (request.get_method(), request.full_url, request.data, dict(request.header_items()))
        )
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class PublicationRequestTest(unittest.TestCase):
    def test_it_sends_exactly_the_one_dispatch_and_nothing_else(self) -> None:
        opener = _Opener(_Response(status=204))
        publication_request.request_publication("token", opener=opener)
        self.assertEqual(1, len(opener.calls))
        method, url, body, headers = opener.calls[0]
        self.assertEqual("POST", method)
        self.assertEqual(DISPATCH_URL, url)
        self.assertEqual({"ref": "main"}, json.loads(body))
        self.assertEqual("Bearer token", headers["Authorization"])

    def test_only_204_is_a_queued_run(self) -> None:
        for answer in (
            _Response(status=200, body=b"{}"),
            urllib.error.HTTPError(DISPATCH_URL, 403, "Forbidden", {}, None),
            urllib.error.HTTPError(DISPATCH_URL, 422, "Unprocessable", {}, None),
            urllib.error.URLError("connection reset"),
            TimeoutError(),
        ):
            with self.subTest(answer=answer):
                with self.assertRaises(publication_request.PublicationRequestFailed):
                    publication_request.request_publication(
                        "token", opener=_Opener(answer)
                    )

    def test_no_token_and_no_https_are_refused_before_transmission(self) -> None:
        opener = _Opener(AssertionError("reached the transport"))
        with self.assertRaises(publication_request.PublicationRequestFailed):
            publication_request.request_publication("", opener=opener)
        with self.assertRaises(publication_request.PublicationRequestFailed):
            publication_request.request_publication(
                "token", api_base="http://api.github.com", opener=opener
            )
        self.assertEqual([], opener.calls)

    def test_a_redirect_is_refused_not_followed(self) -> None:
        requester = publication_request.PublicationRequester("token")
        handlers = [
            handler
            for handler in requester._opener.handlers
            if isinstance(handler, github_graph._RefusedRedirectHandler)
        ]
        self.assertEqual(1, len(handlers))

    def test_the_cli_reports_a_failed_request_as_failed(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""}):
            with redirect_stderr(StringIO()) as err:
                code = publication_request.main([])
        self.assertEqual(publication_request.EXIT_FAILED, code)
        self.assertIn("needs a GitHub token", err.getvalue())


def _document(applied: int = 0, failed: int = 0, skipped: int = 0, satisfied: int = 0) -> dict:
    return {
        "kind": "RoadmapApplyResult",
        "summary": {
            "applied": applied,
            "alreadySatisfied": satisfied,
            "skipped": skipped,
            "failed": failed,
        },
    }


class ApplyRequestsPublicationTest(unittest.TestCase):
    """`apply.main` around a stand-in `_run`, so the live paths need no network."""

    def _main(self, extra: list[str], documents: list[dict], raises=None):  # noqa: ANN001
        def fake_run(args, collected):  # noqa: ANN001
            for index, document in enumerate(documents):
                collected.append((Path(f"m{index}.yaml"), document))
            if raises is not None:
                raise raises

        requests: list[tuple[str, str]] = []

        def fake_request(token, api_base=github_graph.DEFAULT_API_BASE, opener=None):  # noqa: ANN001
            requests.append((token, api_base))

        err = StringIO()
        with mock.patch.object(apply_module, "_run", fake_run), mock.patch.object(
            publication_request, "request_publication", fake_request
        ), mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}):
            with redirect_stdout(StringIO()), redirect_stderr(err):
                code = apply_module.main(["--actor", "roadmap-tests", *extra])
        return code, requests, err.getvalue()

    def test_a_live_run_with_a_landed_write_requests_one_publication(self) -> None:
        code, requests, err = self._main(
            ["--live", "--execute"], [_document(applied=2), _document(satisfied=1)]
        )
        self.assertEqual(apply_module.EXIT_OK, code)
        self.assertEqual([("token", github_graph.DEFAULT_API_BASE)], requests)
        self.assertIn("after 2 landed write(s)", err)

    def test_a_partial_run_that_landed_a_write_still_requests(self) -> None:
        # The graph changed even though the run did not finish: the publication
        # describing the old graph is wrong exactly where the write landed.
        code, requests, _ = self._main(
            ["--live", "--execute"], [_document(applied=1, failed=1)]
        )
        self.assertEqual(apply_module.EXIT_FAILED, code)
        self.assertEqual(1, len(requests))
        code, requests, _ = self._main(
            ["--live", "--execute"],
            [_document(applied=1)],
            raises=apply_module.MutationRefused("simulated refusal on manifest 2"),
        )
        self.assertEqual(apply_module.EXIT_REFUSED, code)
        self.assertEqual(1, len(requests))

    def test_runs_that_wrote_nothing_request_nothing(self) -> None:
        cases = {
            "plan-only": (["--live"], [_document(applied=0, satisfied=0)], None),
            "all satisfied": (["--live", "--execute"], [_document(satisfied=3)], None),
            "all failed": (["--live", "--execute"], [_document(failed=2)], None),
            "all skipped": (["--live", "--execute"], [_document(skipped=1)], None),
            "refused before any write": (
                ["--live", "--execute"],
                [],
                apply_module.ApplyRefused("guard"),
            ),
            "error before any write": (
                ["--live", "--execute"],
                [],
                apply_module.ApplyError("auth"),
            ),
        }
        for name, (extra, documents, raises) in cases.items():
            with self.subTest(name):
                _, requests, _ = self._main(extra, documents, raises)
                self.assertEqual([], requests)

    def test_a_replay_never_requests_even_when_it_applies(self) -> None:
        _, requests, _ = self._main(
            ["--snapshot", "unused.json", "--execute"], [_document(applied=3)]
        )
        self.assertEqual([], requests)

    def test_the_opt_out_is_honoured(self) -> None:
        _, requests, _ = self._main(
            ["--live", "--execute", "--no-publication-request"], [_document(applied=1)]
        )
        self.assertEqual([], requests)

    def test_a_failed_request_is_loud_and_leaves_the_exit_code_to_the_writes(self) -> None:
        def failing(token, api_base=None, opener=None):  # noqa: ANN001
            raise publication_request.PublicationRequestFailed("HTTP 403")

        err = StringIO()
        with mock.patch.object(
            apply_module, "_run", lambda args, docs: docs.append((Path("m.yaml"), _document(applied=1)))
        ), mock.patch.object(publication_request, "request_publication", failing), mock.patch.dict(
            os.environ, {"GITHUB_TOKEN": "token"}
        ):
            with redirect_stdout(StringIO()), redirect_stderr(err):
                code = apply_module.main(["--actor", "t", "--live", "--execute"])
        self.assertEqual(apply_module.EXIT_OK, code)
        self.assertIn("WARNING: 1 write(s) landed", err.getvalue())
        self.assertIn("still describes the graph before this run", err.getvalue())


def _publication(directory: Path, captured_at: str | None, revision: str = "a" * 40) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    observation = {"mode": "live-capture", "apiBase": "https://api.github.com"}
    if captured_at is not None:
        observation["capturedAt"] = captured_at
    else:
        observation["mode"] = "synthetic-fixture"
    (directory / "publication.json").write_text(
        json.dumps(
            {
                "kind": "RoadmapPublication",
                "observation": observation,
                "source": {"revision": revision},
            }
        ),
        encoding="utf-8",
    )
    return directory


class PublicationOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _newer(self, current: str | None, candidate: str | None) -> int:
        current_dir = self.root / "current"
        if current is not None:
            _publication(current_dir, current)
        else:
            current_dir.mkdir()
        candidate_dir = _publication(self.root / "candidate", candidate)
        with redirect_stderr(StringIO()):
            return publication_order.main(["newer", str(current_dir), str(candidate_dir)])

    def test_only_a_strictly_newer_capture_replaces(self) -> None:
        self.assertEqual(0, self._newer("2026-09-23T21:13:40Z", "2026-09-23T22:57:52Z"))

    def test_an_overtaken_capture_is_not_pushed(self) -> None:
        self.assertEqual(1, self._newer("2026-09-23T22:57:52Z", "2026-09-23T21:13:40Z"))

    def test_an_equal_capture_is_not_pushed(self) -> None:
        self.assertEqual(1, self._newer("2026-09-23T22:57:52Z", "2026-09-23T22:57:52Z"))

    def test_the_first_publication_needs_no_predecessor(self) -> None:
        self.assertEqual(0, self._newer(None, "2026-09-23T22:57:52Z"))

    def test_a_synthetic_candidate_is_a_failed_run(self) -> None:
        self.assertEqual(
            publication_order.EXIT_INVALID, self._newer("2026-09-23T22:57:52Z", None)
        )

    def _fresh(self, captured_at: str | None, revision: str, now: datetime, max_age: int = 5400):
        state = _publication(self.root / "state", captured_at, revision="a" * 40)
        return publication_order.is_fresh(state, revision, max_age, now)[0]

    def test_the_schedule_skips_only_a_current_young_publication(self) -> None:
        captured = datetime(2026, 9, 23, 22, 57, 52, tzinfo=timezone.utc)
        stamp = "2026-09-23T22:57:52Z"
        self.assertTrue(self._fresh(stamp, "a" * 40, captured + timedelta(minutes=89)))
        self.assertFalse(self._fresh(stamp, "a" * 40, captured + timedelta(minutes=91)))
        # main moved since the capture: the manifests or evaluator changed.
        self.assertFalse(self._fresh(stamp, "b" * 40, captured + timedelta(minutes=1)))
        # a capture from the future is a broken clock, not freshness.
        self.assertFalse(self._fresh(stamp, "a" * 40, captured - timedelta(minutes=1)))
        self.assertFalse(self._fresh(None, "a" * 40, captured))

    def test_no_state_means_capture(self) -> None:
        empty = self.root / "none"
        empty.mkdir()
        fresh, _ = publication_order.is_fresh(
            empty, "a" * 40, 5400, datetime.now(timezone.utc)
        )
        self.assertFalse(fresh)

    def test_the_guard_needs_no_third_party_package(self) -> None:
        # The publish job holds the write token and installs nothing.
        source = (ROADMAP / "scripts" / "publication_order.py").read_text(encoding="utf-8")
        imported = {
            line.split()[1].split(".")[0]
            for line in source.splitlines()
            if line.startswith(("import ", "from "))
        }
        self.assertLessEqual(imported, {"__future__", "argparse", "json", "sys", "datetime", "pathlib"})


class _LiveGitHub:
    """Answers every GET the live reader makes with an empty, valid graph."""

    def __init__(self) -> None:
        self.gets = 0

    def open(self, request, timeout=None):  # noqa: ANN001, ANN201
        assert request.get_method() == "GET"
        self.gets += 1
        path = urllib.parse.urlsplit(request.full_url).path
        parts = path.strip("/").split("/")
        if len(parts) == 3:  # /repos/{owner}/{repo}
            return _Response(json.dumps({"has_issues": True}).encode(), status=200)
        owner, repo, number = parts[1], parts[2], int(parts[4])
        tail = parts[5:]
        if not tail:
            body = {
                "number": number,
                "repository_url": f"https://api.github.com/repos/{owner}/{repo}",
                "state": "open",
            }
            return _Response(json.dumps(body).encode(), status=200)
        if tail == ["parent"]:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        return _Response(b"[]", status=200)


class CaptureCostTest(unittest.TestCase):
    def test_the_cost_is_what_a_live_capture_actually_spends(self) -> None:
        corpus = ROADMAP / "epics"
        github = _LiveGitHub()
        source = github_graph.LiveGraphSource("token", opener=github)
        validation = reconcile.validate_corpus(corpus, validate._load_schema(validate.DEFAULT_SCHEMA))
        keys = publish._union_keys(validation)
        source.check_repositories([repository for repository, _ in keys])
        source.snapshot(keys)
        self.assertEqual(publish.capture_cost(corpus), github.gets)

    def test_the_cli_prints_the_cost(self) -> None:
        with redirect_stdout(StringIO()) as out:
            code = publish.main(["cost"])
        self.assertEqual(publish.EXIT_OK, code)
        self.assertEqual(publish.capture_cost(ROADMAP / "epics"), int(out.getvalue()))


if __name__ == "__main__":
    unittest.main()
