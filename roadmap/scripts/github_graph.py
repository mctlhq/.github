#!/usr/bin/env python3
"""Observed GitHub issue graph for roadmap reconciliation.

This module owns everything that touches the provider: loading and validating a
`GitHubGraphSnapshot`, normalizing it into a provider-neutral graph, and reading
live state.

The live adapter is structurally read-only. Every request funnels through one
helper that refuses a non-GET method or a request body before transmission, so
the detector has no mutation primitive to misuse -- not as a policy, but because
there is nothing here that can write.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from validate import _json_path, issue_key

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_SCHEMA = ROOT / "schemas" / "github-graph-snapshot.schema.json"

DEFAULT_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
PAGE_SIZE = 100
# Without an explicit timeout urllib waits on the socket forever, so a hung
# endpoint or proxy stalls the whole run with no error and no output -- the one
# failure mode a read-only tool has no way to report.
REQUEST_TIMEOUT_SECONDS = 30

IssueKey = tuple[str, int]

_REPOSITORY_URL = re.compile(r"/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)$")
_LINK_NEXT = re.compile(r'<(?P<url>[^>]+)>\s*;\s*rel="next"')


class WriteAttempted(RuntimeError):
    """Raised before transmission when something tries to mutate GitHub."""


class ObservationError(RuntimeError):
    """A read could not be completed: transport, auth, or malformed response."""


class SnapshotIncomplete(RuntimeError):
    """The snapshot holds no observation for an issue the manifest binds.

    Absent is not the same as absent-from-GitHub. Reporting "not found" for an
    issue nobody looked at would invent evidence, so this is an error rather
    than a diff entry.
    """


def load_schema(path: Path = DEFAULT_SNAPSHOT_SCHEMA) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return schema


def _timestamp_errors(snapshot: dict[str, Any]) -> list[str]:
    """Check the `format: date-time` fields the JSON Schema only annotates.

    `format` is an annotation, not an assertion, unless a format checker is
    wired in -- and wiring one in would mean a new runtime dependency for
    RFC 3339. A live capture whose `capturedAt` is free text would be evidence
    that claims a time nobody can read, so it is checked here instead.
    """

    errors: list[str] = []

    def check(path: str, value: Any) -> None:
        if not isinstance(value, str):
            return
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{path}: {value!r} is not an RFC 3339 timestamp")
            return
        if parsed.tzinfo is None:
            errors.append(f"{path}: {value!r} has no timezone offset")

    source = snapshot.get("source")
    if isinstance(source, dict):
        check("$.source.capturedAt", source.get("capturedAt"))
    for index, observation in enumerate(snapshot.get("issues", []) or []):
        if isinstance(observation, dict):
            check(f"$.issues[{index}].updatedAt", observation.get("updatedAt"))
    return errors


def _duplicate_request_errors(snapshot: dict[str, Any]) -> list[str]:
    """One canonical issue may be observed at most once.

    Two observations of the same request make normalization order-dependent:
    whichever lands last decides the resolved identity, and a found/missing
    pair leaves the issue in two states at once. That is an unusable input, not
    a graph to diff.
    """

    seen: set[tuple[str, int]] = set()
    duplicates: set[tuple[str, int]] = set()
    for observation in snapshot.get("issues", []) or []:
        if not isinstance(observation, dict):
            continue
        key = issue_key(observation.get("requested"))
        if key is None:
            continue
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return [
        f"$.issues: {repository}#{number} is observed more than once"
        for repository, number in sorted(duplicates)
    ]


def snapshot_errors(
    snapshot: dict[str, Any], schema: dict[str, Any] | None = None
) -> list[str]:
    validator = Draft202012Validator(schema if schema is not None else load_schema())
    failures = sorted(
        validator.iter_errors(snapshot),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    errors = [
        f"{_json_path(error.absolute_path)}: {error.message}" for error in failures
    ]
    if errors:
        return errors
    return _timestamp_errors(snapshot) + _duplicate_request_errors(snapshot)


def load_snapshot(path: Path, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        snapshot = json.load(handle)
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot root must be an object")
    errors = snapshot_errors(snapshot, schema)
    if errors:
        raise ValueError("; ".join(errors))
    return snapshot


@dataclass(frozen=True)
class ObservedGraph:
    """Provider-neutral normalized view of one snapshot.

    Keys are canonical `(repository, number)` pairs. `resolution` maps the
    identity that was asked for onto the identity GitHub answered with, which
    differ exactly when an issue was transferred.
    """

    resolution: dict[IssueKey, IssueKey]
    missing: frozenset[IssueKey]
    observed: frozenset[IssueKey]
    parents: dict[IssueKey, tuple[IssueKey, ...]]
    children: dict[IssueKey, tuple[IssueKey, ...]]
    blocked_by: frozenset[tuple[IssueKey, IssueKey]]

    def resolve(self, key: IssueKey) -> IssueKey | None:
        return self.resolution.get(key)

    def parent_of(self, key: IssueKey) -> tuple[IssueKey, ...]:
        return self.parents.get(key, ())


def _ref_key(ref: Any) -> IssueKey:
    key = issue_key(ref)
    if key is None:
        raise ValueError(f"not an issue ref: {ref!r}")
    return key


def observed_graph(snapshot: dict[str, Any]) -> ObservedGraph:
    """Normalize a schema-valid snapshot into a comparable graph."""

    resolution: dict[IssueKey, IssueKey] = {}
    missing: set[IssueKey] = set()
    observed: set[IssueKey] = set()
    parent_edges: set[tuple[IssueKey, IssueKey]] = set()
    blocked_by: set[tuple[IssueKey, IssueKey]] = set()

    for observation in snapshot.get("issues", []):
        requested = _ref_key(observation["requested"])
        if not observation.get("found", False):
            missing.add(requested)
            continue

        resolved = _ref_key(observation["resolved"])
        resolution[requested] = resolved
        observed.add(resolved)

        parent = observation.get("parent")
        if parent is not None:
            parent_key = _ref_key(parent)
            if parent_key != resolved:
                parent_edges.add((resolved, parent_key))

        for child in observation.get("subIssues", []):
            child_key = _ref_key(child)
            if child_key != resolved:
                parent_edges.add((child_key, resolved))

        for blocker in observation.get("blockedBy", []):
            blocker_key = _ref_key(blocker)
            if blocker_key != resolved:
                blocked_by.add((resolved, blocker_key))

    parents: dict[IssueKey, set[IssueKey]] = {}
    children: dict[IssueKey, set[IssueKey]] = {}
    for child_key, parent_key in parent_edges:
        parents.setdefault(child_key, set()).add(parent_key)
        children.setdefault(parent_key, set()).add(child_key)

    return ObservedGraph(
        resolution=resolution,
        missing=frozenset(missing),
        observed=frozenset(observed),
        parents={key: tuple(sorted(value)) for key, value in sorted(parents.items())},
        children={key: tuple(sorted(value)) for key, value in sorted(children.items())},
        blocked_by=frozenset(blocked_by),
    )


def require_observations(snapshot: dict[str, Any], keys: Iterable[IssueKey]) -> None:
    """Fail loudly when the snapshot never looked at an issue we must compare."""

    seen = {_ref_key(item["requested"]) for item in snapshot.get("issues", [])}
    absent = sorted(set(keys) - seen)
    if absent:
        rendered = ", ".join(f"{repository}#{number}" for repository, number in absent)
        raise SnapshotIncomplete(f"snapshot holds no observation for: {rendered}")


class FixtureGraphSource:
    """Replays a captured or synthetic snapshot. Performs no I/O beyond the file."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot

    @classmethod
    def from_path(
        cls, path: Path, schema: dict[str, Any] | None = None
    ) -> "FixtureGraphSource":
        return cls(load_snapshot(path, schema))

    def snapshot(self, keys: Sequence[IssueKey]) -> dict[str, Any]:
        require_observations(self._snapshot, keys)
        return self._snapshot


class LiveGraphSource:
    """GET-only GitHub REST reader.

    GraphQL is deliberately unused: the safety contract of this slice is that
    every call is a GET with no body, which is checkable by construction.
    """

    def __init__(
        self,
        token: str,
        api_base: str = DEFAULT_API_BASE,
        opener: Any | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not token:
            raise ObservationError("live mode requires a GitHub token")
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._opener = opener or urllib.request.build_opener()
        self._timeout = timeout
        # One issue is observed once per process, however many manifests or
        # capture passes ask for it. Re-reading would also let a graph change
        # between two halves of the same diff.
        self._cache: dict[IssueKey, dict[str, Any]] = {}
        self._captured_at = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    # -- transport -------------------------------------------------------

    def _request(self, method: str, url: str, body: bytes | None = None) -> Any:
        if method != "GET":
            raise WriteAttempted(f"{method} {url}: this source may only read")
        if body is not None:
            raise WriteAttempted(f"request body for {url}: this source may only read")

        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", GITHUB_API_VERSION)
        request.add_header("Authorization", f"Bearer {self._token}")
        return self._opener.open(request, timeout=self._timeout)

    def _get(self, url: str) -> tuple[int, Any, dict[str, str]]:
        try:
            response = self._request("GET", url)
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                return error.code, None, {}
            raise ObservationError(f"GET {url}: HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise ObservationError(f"GET {url}: {error.reason}") from error
        except TimeoutError as error:
            raise ObservationError(f"GET {url}: timed out") from error

        with response:
            raw = response.read()
            headers = {key.lower(): value for key, value in response.headers.items()}
            status = getattr(response, "status", 200) or 200
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError as error:
            raise ObservationError(f"GET {url}: malformed JSON") from error
        return status, payload, headers

    def _get_all(self, url: str) -> list[Any]:
        """Follow rel=next to the last page, or raise.

        An empty relation list is `200 []`. A 404 or 410 on a collection
        endpoint -- first page or any later one -- means the observation failed
        or came back partial, never that there are no relations. Returning what
        was accumulated so far would pass a truncated list off as complete and
        report drift on the pages nobody read.
        """

        items: list[Any] = []
        next_url: str | None = f"{url}?per_page={PAGE_SIZE}"
        while next_url:
            status, payload, headers = self._get(next_url)
            if status in (404, 410):
                raise ObservationError(
                    f"GET {next_url}: HTTP {status} on a relation listing"
                )
            # A 200 that is not a list, or a list holding something other than
            # issue objects, is a response we could not read -- not a response
            # saying there are no relations. Dropping it would turn malformed
            # provider data into an observed absence and report drift on it.
            if not isinstance(payload, list):
                raise ObservationError(
                    f"GET {next_url}: expected a JSON array, got {type(payload).__name__}"
                )
            for item in payload:
                if not isinstance(item, dict):
                    raise ObservationError(
                        f"GET {next_url}: expected issue objects, got {type(item).__name__}"
                    )
            items.extend(payload)
            match = _LINK_NEXT.search(headers.get("link", ""))
            next_url = match.group("url") if match else None
        return items

    # -- observation -----------------------------------------------------

    def _issue_url(self, key: IssueKey) -> str:
        repository, number = key
        return f"{self._api_base}/repos/{repository}/issues/{number}"

    @staticmethod
    def _resolved_key(payload: dict[str, Any]) -> IssueKey:
        """Derive canonical identity from the body, never from the final URL.

        A transferred issue answers 301 and the HTTP client may follow it before
        anything reaches us, so the request URL is not evidence of where we
        landed. The response body is.
        """

        repository_url = payload.get("repository_url")
        number = payload.get("number")
        if not isinstance(repository_url, str) or not isinstance(number, int):
            raise ObservationError("issue response carries no usable identity")
        match = _REPOSITORY_URL.search(urllib.parse.urlparse(repository_url).path)
        if match is None:
            raise ObservationError(f"unparsable repository_url: {repository_url}")
        return _ref_key(
            {
                "repository": f"{match.group('owner')}/{match.group('repo')}",
                "number": number,
            }
        )

    def _observe(self, key: IssueKey) -> dict[str, Any]:
        requested = {"repository": key[0], "number": key[1]}
        base = self._issue_url(key)

        status, payload, _ = self._get(base)
        if status in (404, 410):
            return {"requested": requested, "found": False}
        if not isinstance(payload, dict):
            # Only 404/410 mean "not found". Anything else unreadable is an
            # observation failure, never evidence that the issue does not exist.
            raise ObservationError(
                f"GET {base}: expected an issue object, got {type(payload).__name__}"
            )

        resolved = self._resolved_key(payload)
        observation: dict[str, Any] = {
            "requested": requested,
            "resolved": {"repository": resolved[0], "number": resolved[1]},
            "found": True,
        }
        state = payload.get("state")
        if state in ("open", "closed"):
            observation["state"] = state
        updated_at = payload.get("updated_at")
        if isinstance(updated_at, str):
            observation["updatedAt"] = updated_at

        # Sub-resources are fetched from the RESOLVED identity. The issue GET
        # may have followed a transfer redirect, and asking the old location
        # for relations either 404s or answers about a different object --
        # either way the graph would come back empty and every relation on a
        # transferred issue would look like drift, which is exactly the
        # cascade the redirect contract exists to prevent.
        resolved_base = self._issue_url(resolved)

        parent_status, parent_payload, _ = self._get(f"{resolved_base}/parent")
        if parent_status == 404:
            # GitHub documents 404 on /parent as "this issue has no parent".
            # 410 is not that: the issue itself was just read successfully.
            observation["parent"] = None
        elif parent_status == 410:
            raise ObservationError(
                f"GET {resolved_base}/parent: HTTP 410 on an issue that exists"
            )
        elif not isinstance(parent_payload, dict):
            raise ObservationError(
                f"GET {resolved_base}/parent: expected an issue object, "
                f"got {type(parent_payload).__name__}"
            )
        else:
            parent_key = self._resolved_key(parent_payload)
            observation["parent"] = {
                "repository": parent_key[0],
                "number": parent_key[1],
            }

        sub_issues = self._get_all(f"{resolved_base}/sub_issues")
        observation["subIssues"] = [
            self._as_ref(item) for item in sub_issues
        ]

        blocked_by = self._get_all(f"{resolved_base}/dependencies/blocked_by")
        observation["blockedBy"] = [
            self._as_ref(item) for item in blocked_by
        ]
        return observation

    def _as_ref(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = self._resolved_key(payload)
        return {"repository": key[0], "number": key[1]}

    def _observation(self, key: IssueKey) -> dict[str, Any]:
        if key not in self._cache:
            self._cache[key] = self._observe(key)
        return self._cache[key]

    def snapshot(self, keys: Sequence[IssueKey]) -> dict[str, Any]:
        issues = [self._observation(key) for key in sorted(set(keys))]
        snapshot = {
            "apiVersion": "roadmap.mctl.ai/v1alpha1",
            "kind": "GitHubGraphSnapshot",
            "source": {
                "mode": "live-capture",
                "capturedAt": self._captured_at,
                "apiBase": self._api_base,
            },
            "issues": issues,
        }
        # The live source is a producer of the published contract, not an
        # exception to it: replay validates on load, so the capture that replay
        # will later load has to satisfy the same schema when it is written.
        errors = snapshot_errors(snapshot)
        if errors:
            raise ObservationError(
                "live snapshot violates github-graph-snapshot.schema.json: "
                + "; ".join(errors)
            )
        return snapshot
