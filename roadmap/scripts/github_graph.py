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
from dataclasses import dataclass, field
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

# fromisoformat() alone also accepts ISO 8601 forms RFC 3339 does not: a space
# instead of "T", omitted seconds, a date with no time. Shape first, then parse.
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
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
        if not _RFC3339.match(value):
            errors.append(f"{path}: {value!r} is not an RFC 3339 timestamp")
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
    # Resolved identity -> (state, stateReason) for every found issue whose state
    # was observed. An issue with no entry had no state captured: its completion is
    # unknown, never assumed open or closed.
    states: dict[IssueKey, tuple[str, str | None]] = field(default_factory=dict)

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
    states: dict[IssueKey, tuple[str, str | None]] = {}

    for observation in snapshot.get("issues", []):
        requested = _ref_key(observation["requested"])
        if not observation.get("found", False):
            missing.add(requested)
            continue

        resolved = _ref_key(observation["resolved"])
        resolution[requested] = resolved
        observed.add(resolved)
        if "state" in observation:
            states[resolved] = (observation["state"], observation.get("stateReason"))

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
        states=dict(sorted(states.items())),
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

    def snapshot(
        self, keys: Sequence[IssueKey], *, require_complete: bool = True
    ) -> dict[str, Any]:
        """Return the replayed snapshot.

        By default a snapshot that never observed one of `keys` is an error.
        Health evaluation passes `require_complete=False` so it can report which
        endpoints were not observed instead of failing outright; it then treats
        those endpoints as unobservable, never as absent.
        """

        # A source built from a dict never went through load_snapshot(), so it
        # is validated here, before require_observations() indexes into it.
        errors = snapshot_errors(self._snapshot)
        if errors:
            raise ValueError("snapshot is invalid: " + "; ".join(errors))
        if require_complete:
            require_observations(self._snapshot, keys)
        return self._snapshot


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within the API origin.

    urllib's default handler copies every request header except content-length
    and content-type onto the redirected request -- Authorization included --
    for any Location, on any host. Redirects cannot simply be disabled: a
    transferred issue answers with one. So each Location is held to the same
    origin rule as the original URL before the credential goes with it.
    """

    def __init__(self, allowed: Any) -> None:
        super().__init__()
        self._allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self._allowed(newurl):
            raise ObservationError(
                f"refusing to follow a redirect off the API origin: {newurl}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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
        self._origin = urllib.parse.urlsplit(self._api_base)
        # The token is sent with every request, so the base must be HTTPS. There
        # is deliberately no insecure opt-in: nothing this tool does needs one.
        if self._origin.scheme != "https" or not self._origin.hostname:
            raise ObservationError(
                f"api_base must be an https URL, got {api_base!r}: "
                "the token is sent with every request"
            )
        # `https://api.github.com@attacker.example` has scheme https and a
        # non-empty netloc, yet its host is attacker.example -- the text before
        # "@" is userinfo. A base may carry no userinfo, query or fragment: the
        # first would redirect the token, the others would copy whatever they
        # hold into every request and into `source.apiBase` of every capture.
        if (
            "@" in self._origin.netloc
            or self._origin.query
            or self._origin.fragment
        ):
            raise ObservationError(
                "api_base must not contain userinfo, a query or a fragment: "
                f"{api_base!r}"
            )
        self._opener = opener or urllib.request.build_opener(
            _SameOriginRedirectHandler(self._is_allowed)
        )
        self._timeout = timeout
        # One issue is observed once per process, however many manifests or
        # capture passes ask for it. Re-reading would also let a graph change
        # between two halves of the same diff.
        self._cache: dict[IssueKey, dict[str, Any]] = {}
        # Keyed by RESOLVED identity. A transferred issue can be requested under
        # its old key and its new one in the same run; without this, the second
        # alias re-reads the same live object and the snapshot can describe it
        # in two states at once.
        self._resolved: dict[IssueKey, dict[str, Any]] = {}
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

        # Every request carries the bearer token, and pagination follows URLs
        # taken from a response header. Only the configured API origin may
        # receive the token: a malformed or cross-origin `rel="next"` link must
        # fail the read, not forward the credential to another host. Redirects
        # are held to the same rule by _SameOriginRedirectHandler.
        if not self._is_allowed(url):
            raise ObservationError(
                f"refusing to send credentials outside {self._api_base}: {url}"
            )

        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", GITHUB_API_VERSION)
        request.add_header("Authorization", f"Bearer {self._token}")
        return self._opener.open(request, timeout=self._timeout)

    def _is_allowed(self, url: str) -> bool:
        target = urllib.parse.urlsplit(url)
        if (target.scheme, target.netloc) != (self._origin.scheme, self._origin.netloc):
            return False
        prefix = self._origin.path.rstrip("/")
        return not prefix or target.path == prefix or target.path.startswith(prefix + "/")

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

        # The timeout covers reading the body too, not only opening the
        # connection; a stall or reset mid-body is the same failed read.
        try:
            with response:
                raw = response.read()
                headers = {key.lower(): value for key, value in response.headers.items()}
                status = getattr(response, "status", 200) or 200
        except OSError as error:
            raise ObservationError(f"GET {url}: failed reading the response: {error}") from error
        try:
            payload = json.loads(raw) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
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
        visited: set[str] = set()
        while next_url:
            # A `rel="next"` that points back at a page already read would loop
            # forever without ever reaching a timeout.
            if next_url in visited:
                raise ObservationError(f"GET {next_url}: pagination revisits a page")
            visited.add(next_url)
            status, payload, headers = self._get(next_url)
            if status in (404, 410):
                raise ObservationError(
                    f"GET {next_url}: HTTP {status} on a relation listing"
                )
            # Only a 200 is a complete page. A 206 or any other non-200 success
            # carrying an array is not evidence of the full relation set.
            if status != 200:
                raise ObservationError(
                    f"GET {next_url}: HTTP {status} is not a complete relation page"
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
        if status != 200:
            raise ObservationError(f"GET {base}: HTTP {status} is not a complete read")
        if not isinstance(payload, dict):
            # Only 404/410 mean "not found". Anything else unreadable is an
            # observation failure, never evidence that the issue does not exist.
            raise ObservationError(
                f"GET {base}: expected an issue object, got {type(payload).__name__}"
            )

        resolved = self._resolved_key(payload)
        known = self._resolved.get(resolved)
        if known is not None:
            return {"requested": requested, **known}

        observation: dict[str, Any] = {
            "requested": requested,
            "resolved": {"repository": resolved[0], "number": resolved[1]},
            "found": True,
        }
        state = payload.get("state")
        if state in ("open", "closed"):
            observation["state"] = state
        # Kept verbatim, whatever the value: dropping a reason this code does not
        # recognise would turn "closed for an unknown reason" into "closed", which
        # completion would count as delivered.
        state_reason = payload.get("state_reason")
        if isinstance(state_reason, str) and state_reason:
            observation["stateReason"] = state_reason
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
        elif parent_status != 200:
            raise ObservationError(
                f"GET {resolved_base}/parent: HTTP {parent_status} is not a complete read"
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
        self._resolved[resolved] = {
            key: value for key, value in observation.items() if key != "requested"
        }
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
