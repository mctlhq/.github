#!/usr/bin/env python3
"""Ask the publisher for a fresh RoadmapPublication. Nothing else.

A governed apply changes the GitHub graph without touching `main`, so the
`push` trigger of `roadmap-publish.yml` never sees it and the published
readiness stays behind the graph until the next scheduled run -- hours, and
mctl-api refuses to plan a wave from a publication older than 30 minutes.
`apply.py` therefore calls `request_publication` after a live run in which a
write landed, and an operator can run this module directly before planning a
wave (see "Publication freshness" in `roadmap/README.md`).

This module does not publish and does not observe. It sends exactly one
request, `workflow_dispatch` for the publisher on `main`, and the publisher
does its own GET-only capture, evaluation and verification exactly as for any
other trigger. So a request can make the publication fresher, never different
in kind, and a request that fails changes nothing: `roadmap-state` keeps its
older `capturedAt` and reads exactly as old as it is.

It is kept apart from `github_apply.py` on purpose. That module's allow-list
is the four relation writes and a change to it is a change to what the
roadmap may do to issues; triggering a workflow is not an issue write and
does not belong on that list. Here the allow-list is one request, fixed in
full -- method, path and body -- with no argument that can change any of the
three. Origin, HTTPS and redirect rules come from
`github_graph.OriginBoundClient`, like both other clients.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

import github_graph
from github_graph import DEFAULT_API_BASE, GITHUB_API_VERSION, REQUEST_TIMEOUT_SECONDS

PUBLISHER_REPOSITORY = "mctlhq/.github"
PUBLISHER_WORKFLOW = "roadmap-publish.yml"
PUBLISHER_REF = "main"

# The one request this module can send, in full.
METHOD = "POST"
PATH = (
    f"/repos/{PUBLISHER_REPOSITORY}/actions/workflows/{PUBLISHER_WORKFLOW}/dispatches"
)
BODY = {"ref": PUBLISHER_REF}

# GitHub answers a created dispatch with 204. Anything else -- a 200 carrying a
# body included -- is not evidence that a run was queued.
SUCCESS_STATUSES = frozenset({204})

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAILED = 4


class PublicationRequestFailed(RuntimeError):
    """The request was not accepted; the publication is exactly as old as before."""


class PublicationRequester(github_graph.OriginBoundClient):
    """Sends the publisher's `workflow_dispatch`, and only that."""

    origin_error = PublicationRequestFailed

    def __init__(
        self,
        token: str,
        api_base: str = DEFAULT_API_BASE,
        opener: Any | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not token:
            raise PublicationRequestFailed("requesting a publication needs a GitHub token")
        self._token = token
        self._bind_origin(api_base)
        # A dispatch is a write: a redirect is refused, never followed, for the
        # same reason as in `github_apply` -- urllib would turn the POST into a
        # GET and a 200 from that GET is not a queued run.
        self._opener = self._build_opener(opener, follow_redirects=False)
        self._timeout = timeout

    def request(self) -> None:
        url = f"{self._api_base}{PATH}"
        if not self._is_allowed(url):
            raise PublicationRequestFailed(
                f"refusing to send credentials outside {self._api_base}: {url}"
            )
        http = urllib.request.Request(
            url,
            data=json.dumps(BODY, sort_keys=True).encode("utf-8"),
            method=METHOD,
        )
        http.add_header("Accept", "application/vnd.github+json")
        http.add_header("X-GitHub-Api-Version", GITHUB_API_VERSION)
        http.add_header("Authorization", f"Bearer {self._token}")
        http.add_header("Content-Type", "application/json")
        try:
            response = self._opener.open(http, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            raise PublicationRequestFailed(f"{METHOD} {url}: HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise PublicationRequestFailed(f"{METHOD} {url}: {error.reason}") from error
        except TimeoutError as error:
            raise PublicationRequestFailed(f"{METHOD} {url}: timed out") from error
        with response:
            status = getattr(response, "status", 200) or 200
            response.read()
        if status not in SUCCESS_STATUSES:
            raise PublicationRequestFailed(f"{METHOD} {url}: HTTP {status}")


def request_publication(
    token: str,
    api_base: str = DEFAULT_API_BASE,
    opener: Any | None = None,
) -> None:
    PublicationRequester(token, api_base=api_base, opener=opener).request()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    try:
        request_publication(token, api_base=args.api_base)
    except PublicationRequestFailed as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(
        f"requested {PUBLISHER_WORKFLOW} on {PUBLISHER_REPOSITORY}@{PUBLISHER_REF}; "
        "freshness is roadmap-state publication.json observation.capturedAt, "
        "not this message",
        file=sys.stderr,
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
