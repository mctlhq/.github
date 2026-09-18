#!/usr/bin/env python3
"""The only module in this control plane that may write to GitHub.

It is deliberately separate from `github_graph.py`. That module's safety
property is that it contains no mutation primitive at all, which is checkable by
reading one function; putting a writer beside the reader would replace a
structural guarantee with a code-review convention.

This module is the mirror image. Every request funnels through one place that
refuses, BEFORE transmission:

* any (method, path template) pair outside `ALLOWED` -- four endpoints, no more;
* any `GET` -- reads belong to `github_graph`, and a reader here would let this
  module observe the graph it is about to change;
* any request touching an identity outside the owned set the caller supplied,
  which `apply.py` takes from the validated manifest and nothing else.

Origin, HTTPS, userinfo and redirect rules are inherited from
`github_graph.OriginBoundClient`, so the credential rules of the read and write
halves cannot drift apart.

`FakeMutator` applies the same operations to an in-memory `GitHubGraphSnapshot`
under the same assertions, so the whole apply path is provable offline against
the existing fixtures. It is the write-side analogue of `FixtureGraphSource`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import github_graph
from github_graph import (
    DEFAULT_API_BASE,
    GITHUB_API_VERSION,
    REQUEST_TIMEOUT_SECONDS,
    IssueKey,
)

ADD_SUB_ISSUE = "AddSubIssue"
REMOVE_SUB_ISSUE = "RemoveSubIssue"
ADD_DEPENDENCY = "AddDependency"
REMOVE_DEPENDENCY = "RemoveDependency"

SUB_ISSUES = "/repos/{owner}/{repo}/issues/{number}/sub_issues"
SUB_ISSUE = "/repos/{owner}/{repo}/issues/{number}/sub_issue"
BLOCKED_BY = "/repos/{owner}/{repo}/issues/{number}/dependencies/blocked_by"
BLOCKED_BY_ONE = (
    "/repos/{owner}/{repo}/issues/{number}/dependencies/blocked_by/{dependency}"
)

# The closed list. Four native relation endpoints, nothing that creates,
# closes, labels, comments on or transfers an issue. A change to the set of
# writes this control plane may perform is a change to this constant, in one
# place, reviewable on its own.
ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", SUB_ISSUES),
        ("DELETE", SUB_ISSUE),
        ("POST", BLOCKED_BY),
        ("DELETE", BLOCKED_BY_ONE),
    }
)

SUCCESS_STATUSES = frozenset({200, 201, 202, 204})


class MutationRefused(RuntimeError):
    """Raised before transmission: this write was never allowed to happen."""


class MutationFailed(RuntimeError):
    """The write was allowed, was attempted, and did not succeed."""


@dataclass(frozen=True)
class MutationRequest:
    """One write, described in full before anything is sent.

    `template` is the allow-list key and `path` the concrete instance of it.
    Keeping both means the guard matches on a fixed shape rather than on a
    string an issue number could smuggle a different endpoint into.
    """

    kind: str
    method: str
    template: str
    path: str
    target: IssueKey
    related: IssueKey
    body: dict[str, Any] | None = None


def _label(key: IssueKey) -> str:
    return f"{key[0]}#{key[1]}"


def _owner_repo(key: IssueKey) -> tuple[str, str]:
    owner, _, repo = key[0].partition("/")
    if not owner or not repo:
        raise MutationRefused(f"not an owner/repo identity: {key[0]!r}")
    return owner, repo


def build_request(
    kind: str, target: IssueKey, related: IssueKey, related_id: int
) -> MutationRequest:
    """Describe one of the four permitted writes.

    `related_id` is GitHub's numeric issue id of the *related* issue: the
    sub-issue and dependency endpoints identify it by id, never by number.
    """

    owner, repo = _owner_repo(target)
    number = target[1]

    if kind == ADD_SUB_ISSUE:
        return MutationRequest(
            kind=kind,
            method="POST",
            template=SUB_ISSUES,
            path=f"/repos/{owner}/{repo}/issues/{number}/sub_issues",
            target=target,
            related=related,
            body={"sub_issue_id": related_id},
        )
    if kind == REMOVE_SUB_ISSUE:
        return MutationRequest(
            kind=kind,
            method="DELETE",
            template=SUB_ISSUE,
            path=f"/repos/{owner}/{repo}/issues/{number}/sub_issue",
            target=target,
            related=related,
            body={"sub_issue_id": related_id},
        )
    if kind == ADD_DEPENDENCY:
        return MutationRequest(
            kind=kind,
            method="POST",
            template=BLOCKED_BY,
            path=f"/repos/{owner}/{repo}/issues/{number}/dependencies/blocked_by",
            target=target,
            related=related,
            body={"issue_id": related_id},
        )
    if kind == REMOVE_DEPENDENCY:
        return MutationRequest(
            kind=kind,
            method="DELETE",
            template=BLOCKED_BY_ONE,
            path=(
                f"/repos/{owner}/{repo}/issues/{number}"
                f"/dependencies/blocked_by/{related_id}"
            ),
            target=target,
            related=related,
        )
    raise MutationRefused(f"unknown mutation: {kind!r}")


def check_request(
    request: MutationRequest,
    owned: frozenset[IssueKey],
    authored: frozenset[IssueKey] | None = None,
) -> None:
    """The guard. Every funnel calls it, and it raises before transmission.

    Deliberately re-checked at the point of transmission rather than trusted
    from the point of construction: a caller that assembled a `MutationRequest`
    by hand is exactly the caller this has to stop.

    The two ends of a request are held to different standards, because they are
    different claims. `request.target` is the issue whose relations are being
    edited -- the repository the call is made against -- so it must be an
    identity the manifest itself owns. `request.related` is only named in the
    body (or, for one endpoint, the trailing id) of a call made elsewhere, so it
    may come from the wider authored set, which includes `externalDependsOn`
    targets. "We depend on their issue" is a statement the manifest is allowed
    to make; "we may edit their issue's children" is not, and collapsing both
    into one set turned the former into the latter for `MoveSubIssue`, whose
    `observedParent` comes off the observed graph rather than the manifest.

    `authored` defaults to `owned`, i.e. to the strictest reading, so a caller
    that knows nothing of the split gets the narrow boundary rather than the
    wide one.
    """

    if authored is None:
        authored = owned
    if request.method == "GET":
        raise MutationRefused(
            f"GET {request.path}: reads belong to github_graph, not to this client"
        )
    if (request.method, request.template) not in ALLOWED:
        raise MutationRefused(
            f"{request.method} {request.template}: outside the write allow-list"
        )
    if request.target not in owned:
        raise MutationRefused(
            f"{request.kind} writes against an identity outside the owned set: "
            f"{_label(request.target)}"
        )
    if request.related not in authored:
        raise MutationRefused(
            f"{request.kind} names an identity outside the authored set: "
            f"{_label(request.related)}"
        )


class Mutator:
    """Vocabulary of permitted writes, shared by the live and offline clients.

    Subclasses provide transmission (`_perform`) and id resolution
    (`_issue_id`). They do not get to provide the guard.
    """

    def __init__(
        self,
        owned: Iterable[IssueKey],
        authored: Iterable[IssueKey] | None = None,
    ) -> None:
        self._owned = frozenset(owned)
        # See `check_request`: the target of a write must be owned, the related
        # end need only be authored. Defaulting to `owned` keeps the narrow
        # boundary for any caller that does not know about the split.
        self._authored = self._owned if authored is None else frozenset(authored)
        if not self._owned <= self._authored:
            raise MutationRefused("the owned set must be part of the authored set")
        # Every request that passed the guard, in order. Tests assert on this to
        # prove a refused or skipped operation opened no socket at all.
        self.writes: list[MutationRequest] = []

    @property
    def owned(self) -> frozenset[IssueKey]:
        return self._owned

    @property
    def authored(self) -> frozenset[IssueKey]:
        return self._authored

    # -- vocabulary ------------------------------------------------------

    def add_sub_issue(self, parent: IssueKey, child: IssueKey) -> None:
        self._submit(ADD_SUB_ISSUE, parent, child)

    def remove_sub_issue(self, parent: IssueKey, child: IssueKey) -> None:
        self._submit(REMOVE_SUB_ISSUE, parent, child)

    def add_dependency(self, blocked: IssueKey, blocker: IssueKey) -> None:
        self._submit(ADD_DEPENDENCY, blocked, blocker)

    def remove_dependency(self, blocked: IssueKey, blocker: IssueKey) -> None:
        self._submit(REMOVE_DEPENDENCY, blocked, blocker)

    # -- plumbing --------------------------------------------------------

    def _submit(self, kind: str, target: IssueKey, related: IssueKey) -> None:
        # Ownership is checked before the id is resolved: resolving an id for a
        # foreign issue would already be a lookup this client has no business
        # making.
        if target not in self._owned:
            raise MutationRefused(
                f"{kind} writes against an identity outside the owned set: "
                f"{_label(target)}"
            )
        if related not in self._authored:
            raise MutationRefused(
                f"{kind} names an identity outside the authored set: {_label(related)}"
            )
        request = build_request(kind, target, related, self._issue_id(related))
        check_request(request, self._owned, self._authored)
        self._perform(request)

    def _issue_id(self, key: IssueKey) -> int:
        raise NotImplementedError

    def _perform(self, request: MutationRequest) -> None:
        raise NotImplementedError


class LiveMutator(Mutator, github_graph.OriginBoundClient):
    """Allow-listed GitHub REST writer.

    GraphQL is unused here for the same reason it is unused in the reader: a
    GraphQL document is an open-ended mutation surface, and an allow-list of
    four REST endpoints is not.
    """

    origin_error = MutationRefused

    def __init__(
        self,
        token: str,
        owned: Iterable[IssueKey],
        api_base: str = DEFAULT_API_BASE,
        opener: Any | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        resolve_id: Callable[[IssueKey], int] | None = None,
        authored: Iterable[IssueKey] | None = None,
    ) -> None:
        super().__init__(owned, authored)
        if not token:
            raise MutationRefused("live apply requires a GitHub token")
        self._token = token
        self._bind_origin(api_base)
        # Writes refuse redirects outright; see `_RefusedRedirectHandler`. The
        # read client's same-origin handler is not enough here: it follows a
        # same-origin 301 from a transferred repository, and urllib turns the
        # POST into a GET on the way.
        self._opener = self._build_opener(opener, follow_redirects=False)
        self._timeout = timeout
        self._resolve_id = resolve_id

    def _issue_id(self, key: IssueKey) -> int:
        # GitHub identifies the related issue of a sub-issue or dependency call
        # by numeric id, and `github-graph-snapshot.schema.json` records identity
        # as {repository, number} only. The id therefore has to come from the
        # caller; this client will not read GitHub to find it, because a reader
        # here would defeat the point of the module split.
        if self._resolve_id is None:
            raise MutationRefused(
                "live apply needs an issue-id resolver: the sub-issue and "
                "dependency endpoints identify the related issue by id, which "
                "the observed snapshot does not carry"
            )
        issue_id = self._resolve_id(key)
        if not isinstance(issue_id, int) or issue_id <= 0:
            raise MutationRefused(f"no usable GitHub issue id for {_label(key)}")
        return issue_id

    def _url(self, path: str) -> str:
        # An absolute path is joined to the configured base; anything that
        # already looks like a URL is left alone so the origin rule -- not this
        # helper -- decides whether it may be contacted.
        if "://" in path:
            return path
        return f"{self._api_base}{path}"

    def _perform(self, request: MutationRequest) -> None:
        check_request(request, self._owned, self._authored)
        url = self._url(request.path)
        if not self._is_allowed(url):
            raise MutationRefused(
                f"refusing to send credentials outside {self._api_base}: {url}"
            )

        payload = (
            json.dumps(request.body, sort_keys=True).encode("utf-8")
            if request.body is not None
            else None
        )
        http = urllib.request.Request(url, data=payload, method=request.method)
        http.add_header("Accept", "application/vnd.github+json")
        http.add_header("X-GitHub-Api-Version", GITHUB_API_VERSION)
        http.add_header("Authorization", f"Bearer {self._token}")
        if payload is not None:
            http.add_header("Content-Type", "application/json")

        self.writes.append(request)
        try:
            response = self._opener.open(http, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            raise MutationFailed(
                f"{request.method} {url}: HTTP {error.code}"
            ) from error
        except urllib.error.URLError as error:
            raise MutationFailed(f"{request.method} {url}: {error.reason}") from error
        except TimeoutError as error:
            raise MutationFailed(f"{request.method} {url}: timed out") from error

        with response:
            status = getattr(response, "status", 200) or 200
            response.read()
        if status not in SUCCESS_STATUSES:
            raise MutationFailed(f"{request.method} {url}: HTTP {status}")


class FakeMutator(Mutator):
    """Applies the permitted writes to an in-memory GitHubGraphSnapshot.

    The write-side analogue of `FixtureGraphSource`: same guard, same request
    shapes, no socket. The snapshot is mutated in place and stays schema-valid,
    so a run can be reconciled again afterwards and proved to have converged.
    """

    def __init__(
        self,
        snapshot: dict[str, Any],
        owned: Iterable[IssueKey],
        authored: Iterable[IssueKey] | None = None,
    ) -> None:
        super().__init__(owned, authored)
        self._snapshot = snapshot

    @property
    def state(self) -> dict[str, Any]:
        """The snapshot as it stands now, including every applied write."""

        return self._snapshot

    def source(self) -> github_graph.FixtureGraphSource:
        """A reader over the current state, for the re-read before each write."""

        return github_graph.FixtureGraphSource(self._snapshot)

    def _issue_id(self, key: IssueKey) -> int:
        # The offline graph has no separate id space. The number stands in for
        # the id so the request shape is still built and still checked; nothing
        # in the fake consumes the value.
        return key[1]

    # -- in-memory graph -------------------------------------------------

    def _observation(self, key: IssueKey) -> dict[str, Any]:
        for observation in self._snapshot.get("issues", []):
            identity = observation.get("resolved") or observation.get("requested")
            if github_graph._ref_key(identity) == key:
                if not observation.get("found", False):
                    raise MutationFailed(f"{_label(key)} does not exist")
                return observation
        raise MutationFailed(f"{_label(key)} was never observed")

    @staticmethod
    def _without(refs: list[dict[str, Any]], key: IssueKey) -> list[dict[str, Any]]:
        return [ref for ref in refs if github_graph._ref_key(ref) != key]

    def _perform(self, request: MutationRequest) -> None:
        check_request(request, self._owned, self._authored)
        # Recorded before the graph is touched, exactly like the live client
        # records before it opens the socket: a write that was attempted and
        # failed is still a write that was attempted.
        self.writes.append(request)
        target = request.target
        related = request.related
        ref = {"repository": related[0], "number": related[1]}

        if request.kind == ADD_SUB_ISSUE:
            parent = self._observation(target)
            child = self._observation(related)
            child["parent"] = {"repository": target[0], "number": target[1]}
            children = self._without(parent.setdefault("subIssues", []), related)
            parent["subIssues"] = children + [ref]
        elif request.kind == REMOVE_SUB_ISSUE:
            parent = self._observation(target)
            child = self._observation(related)
            if child.get("parent") is not None and github_graph._ref_key(
                child["parent"]
            ) == target:
                child["parent"] = None
            parent["subIssues"] = self._without(
                parent.setdefault("subIssues", []), related
            )
        elif request.kind == ADD_DEPENDENCY:
            blocked = self._observation(target)
            blockers = self._without(blocked.setdefault("blockedBy", []), related)
            blocked["blockedBy"] = blockers + [ref]
        else:
            blocked = self._observation(target)
            blocked["blockedBy"] = self._without(
                blocked.setdefault("blockedBy", []), related
            )
