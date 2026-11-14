"""Start the formal Agy review only when its verdict can stay current (mctlhq/.github#347).

Trusted default-branch code. It reads GitHub API metadata of the target
repository (pull requests, check runs, reviews, compare, branch rules) and of
this repository's own dispatch runs. It never checks out or reads PR-head
files, and PR-controlled text (titles, bodies, branch names) never steers it:
the only PR fields used are numbers, SHAs, repository names and flags.

A formal run is dispatched for a PR head only when all of these hold:

1. every other required status check of the base branch is passing on the head;
2. a claude[bot] terminal verdict records reviewing that exact head;
3. the head is not BEHIND the base branch tip;
4. no formal check exists on the head and no dispatch run for the PR is active.

Dispatch runs for an older head of the same PR are cancelled, unless their
publish job has already started.

A failed, partial or malformed read is UNKNOWN: it never dispatches and never
counts as "nothing to wait for". Nothing here creates or completes the
`agy-formal-review` check, so until a dispatched run starts it the required
check stays expected/pending, never skipped or green.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

FORMAL_CHECK = 'agy-formal-review'
DISPATCH_WORKFLOW = 'agy-formal-dispatch.yml'
CLAUDE = 'claude[bot]'
SHA = re.compile('[0-9a-f]{40}')
TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z')
REPO = re.compile(r'mctlhq/[A-Za-z0-9_.-]+')
# Kept in step with `run-name` in agy-formal-dispatch.yml: the run list is the
# only place a queued run (which has not created its check yet) is visible.
RUN_NAME = re.compile(r'Agy formal review (mctlhq/[A-Za-z0-9_.-]+)#([1-9][0-9]*) @ ([0-9a-f]{40}|any)')
ACTIVE_STATUSES = ('queued', 'in_progress', 'requested', 'waiting', 'pending')
# GitHub's own rule for a required check run: these conclusions satisfy it.
PASSING = ('success', 'neutral', 'skipped')
VERDICT_STATES = ('APPROVED', 'CHANGES_REQUESTED')
# Two self-hosted runners serve every agy review; more formal runs in flight
# only queue behind each other.
MAX_ACTIVE_FORMAL_RUNS = 2
PAGE_LIMIT = 20


class Unknown(Exception):
    """A read failed, was partial or did not have the documented shape."""


def _int(value, what):
    if type(value) is not int or value < 0:
        raise Unknown(what + ' is not a non-negative integer')
    return value


def _str(value, what):
    if not isinstance(value, str):
        raise Unknown(what + ' is not a string')
    return value


def _dict(value, what):
    if not isinstance(value, dict):
        raise Unknown(what + ' is not an object')
    return value


def _list_of_dicts(value, what):
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise Unknown(what + ' is not a list of objects')
    return value


def _sha(value, what):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise Unknown(what + ' is not a commit SHA')
    return value


# --------------------------------------------------------------------------
# Pure parsers: raw API objects in, facts out, Unknown on any malformation.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    required: tuple  # ((context, integration_id), ...) excluding the formal check
    formal_app_id: int


def parse_policy(rules) -> Policy:
    rules = _list_of_dicts(rules, 'branch rules')
    required, formal = [], []
    for rule in rules:
        if rule.get('type') != 'required_status_checks':
            continue
        params = _dict(rule.get('parameters'), 'required_status_checks parameters')
        for check in _list_of_dicts(params.get('required_status_checks'), 'required checks'):
            context = _str(check.get('context'), 'required check context')
            app = check.get('integration_id')
            if app is None:
                # A check any integration may satisfy cannot be told apart
                # from a look-alike status; refuse rather than guess.
                raise Unknown(f'required check {context!r} is not bound to an integration')
            app = _int(app, 'required check integration_id')
            (formal if context == FORMAL_CHECK else required).append((context, app))
    if len(formal) != 1:
        raise Unknown('base branch does not require exactly one App-bound ' + FORMAL_CHECK)
    return Policy(tuple(required), formal[0][1])


def check_state(check_runs, context, app_id) -> str:
    """'pass', 'fail', 'pending' or 'missing' for one required check."""
    matching = []
    for run in check_runs:
        if run.get('name') != context:
            continue
        app = _dict(run.get('app'), 'check run app')
        if app.get('id') != app_id:
            continue
        _int(run.get('id'), 'check run id')
        matching.append(run)
    if not matching:
        return 'missing'
    latest = max(matching, key=lambda r: r['id'])
    status = _str(latest.get('status'), 'check run status')
    if status != 'completed':
        return 'pending'
    conclusion = _str(latest.get('conclusion'), 'check run conclusion')
    return 'pass' if conclusion in PASSING else 'fail'


def has_formal_check(check_runs, app_id) -> bool:
    for run in check_runs:
        if run.get('name') == FORMAL_CHECK and _dict(run.get('app'), 'check run app').get('id') == app_id:
            return True
    return False


def _time(value, what):
    value = _str(value, what)
    if not TIMESTAMP.fullmatch(value):
        raise Unknown(what + ' is not a UTC timestamp')
    return value  # ISO 8601 UTC strings of one shape order lexically


def has_claude_verdict(reviews, head, head_committed_at) -> bool:
    """A claude[bot] terminal verdict that was given on this exact head.

    commit_id alone is not evidence: after an update-branch GitHub moves the
    latest approval onto the new head (mctlhq/.github#283). Current
    claude-review.yml verdicts record the head they reviewed in the body. A
    verdict from an older pinned caller has no such line; it counts only when
    it was submitted no earlier than the head commit was created, which a
    moved approval never is. `head_committed_at` is read lazily, only then.
    """
    evidence = f'Reviewed head={head} base='
    legacy = []
    for review in reviews:
        user = review.get('user')
        if not isinstance(user, dict) or user.get('login') != CLAUDE or user.get('type') != 'Bot':
            continue
        if review.get('commit_id') != head or review.get('state') not in VERDICT_STATES:
            continue
        body = _str(review.get('body'), 'review body')
        if evidence in body:
            return True
        if 'Reviewed head=' not in body:
            legacy.append(_time(review.get('submitted_at'), 'review submitted_at'))
    if not legacy:
        return False
    committed = _time(_dict(_dict(_load(head_committed_at), 'commit').get('committer'), 'committer').get('date'),
                      'head committer date')
    return any(submitted >= committed for submitted in legacy)


def is_behind(compare) -> bool:
    compare = _dict(compare, 'compare')
    behind_by = _int(compare.get('behind_by'), 'compare behind_by')
    status = _str(compare.get('status'), 'compare status')
    if status not in ('ahead', 'behind', 'identical', 'diverged'):
        raise Unknown('unknown compare status')
    if (status in ('behind', 'diverged')) != (behind_by > 0):
        raise Unknown('compare status contradicts behind_by')
    return behind_by > 0


@dataclass(frozen=True)
class ActiveRun:
    id: int
    status: str
    repository: str | None  # None: the run does not say which PR it targets
    number: int | None
    head: str | None  # None: manual dispatch without a pinned head


def parse_active_runs(runs) -> list:
    result = {}
    for run in _list_of_dicts(runs, 'workflow runs'):
        run_id = _int(run.get('id'), 'run id')
        status = _str(run.get('status'), 'run status')
        if status not in ACTIVE_STATUSES:
            continue
        match = RUN_NAME.fullmatch(_str(run.get('display_title'), 'run display_title'))
        if match:
            head = None if match[3] == 'any' else match[3]
            result[run_id] = ActiveRun(run_id, status, match[1], int(match[2]), head)
        else:
            result[run_id] = ActiveRun(run_id, status, None, None, None)
    return sorted(result.values(), key=lambda r: r.id)


def publish_started(jobs) -> bool:
    jobs = _list_of_dicts(jobs, 'run jobs')
    for job in jobs:
        name = _str(job.get('name'), 'job name')
        status = _str(job.get('status'), 'job status')
        if re.search(r'(^|/ )publish$', name) and status in ('in_progress', 'completed'):
            return True
    return False


@dataclass(frozen=True)
class PullRequest:
    number: int
    head: str
    eligible: bool
    why_not: str = ''


def parse_pr(pr, repo, default_branch) -> PullRequest:
    pr = _dict(pr, 'pull request')
    number = _int(pr.get('number'), 'PR number')
    head = _dict(pr.get('head'), 'PR head')
    base = _dict(pr.get('base'), 'PR base')
    sha = _sha(head.get('sha'), 'PR head sha')
    draft = pr.get('draft')
    if type(draft) is not bool or pr.get('state') != 'open':
        raise Unknown('PR state/draft malformed')
    head_repo = head.get('repo')
    head_name = head_repo.get('full_name') if isinstance(head_repo, dict) else None
    base_name = _dict(base.get('repo'), 'PR base repo').get('full_name')
    if base_name != repo:
        raise Unknown('PR base repository mismatch')
    if draft:
        return PullRequest(number, sha, False, 'draft')
    if head_name != repo:
        return PullRequest(number, sha, False, 'fork or deleted head repository')
    if base.get('ref') != default_branch:
        return PullRequest(number, sha, False, 'base is not the default branch')
    return PullRequest(number, sha, True)


# --------------------------------------------------------------------------
# The decision. Inputs are raw API objects, or an Unknown captured when the
# read itself failed.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    action: str  # 'dispatch', 'wait', 'unknown', 'skip', 'done'
    reason: str


def _load(value):
    """A read is passed as its result, a captured Unknown, or a zero-argument
    callable performing it, so guards that already said 'wait' cost no calls."""
    if callable(value):
        try:
            value = value()
        except Unknown as exc:
            value = exc
    if isinstance(value, Unknown):
        raise Unknown('read failed: ' + str(value))
    return value


def decide(repo: str, pr: PullRequest, policy: Policy, active: list, check_runs, reviews, compare,
           commit) -> Decision:
    """Whether to dispatch a formal run for pr.head now. Never 'dispatch' on doubt."""
    if not pr.eligible:
        return Decision('skip', pr.why_not)
    try:
        if any(r.repository is None for r in active):
            return Decision('unknown', 'an active dispatch run does not name its PR')
        mine = [r for r in active if (r.repository, r.number) == (repo, pr.number)]
        if any(r.head == pr.head for r in mine):
            return Decision('wait', 'a formal run for this head is already active')
        if any(r.head is None for r in mine):
            return Decision('wait', 'a manual formal run for this PR is active')
        check_runs = _list_of_dicts(_load(check_runs), 'check runs')
        if has_formal_check(check_runs, policy.formal_app_id):
            return Decision('done', FORMAL_CHECK + ' already exists on this head')
        for context, app in policy.required:
            state = check_state(check_runs, context, app)
            if state != 'pass':
                return Decision('wait', f'required check {context} is {state}')
        if not has_claude_verdict(_list_of_dicts(_load(reviews), 'reviews'), pr.head, commit):
            return Decision('wait', 'no Claude verdict for this head')
        if is_behind(_load(compare)):
            return Decision('wait', 'branch is BEHIND its base')
        if len(active) >= MAX_ACTIVE_FORMAL_RUNS:
            return Decision('wait', 'formal runner capacity is in use')
    except Unknown as exc:
        return Decision('unknown', str(exc))
    return Decision('dispatch', 'all conditions hold')


def stale_runs(active: list, repo: str, open_heads: dict) -> list:
    """Active runs pinned to a head that is no longer the head of its open PR."""
    return [r for r in active
            if r.repository == repo and r.number in open_heads and r.head is not None
            and r.head != open_heads[r.number]]


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

class API:
    def __init__(self, token):
        if not token:
            raise Unknown('missing token')
        self.token = token

    def __call__(self, method, path, payload=None):
        req = urllib.request.Request(
            'https://api.github.com/' + path, method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status in (202, 204):
                    return None
                raw = response.read(5_000_001)
                if len(raw) > 5_000_000:
                    raise Unknown('response exceeds bounded size')
                return json.loads(raw.decode())
        except urllib.error.HTTPError as exc:
            raise Unknown(f'{method} {path.split("?")[0]}: HTTP {exc.code}') from None
        except (OSError, ValueError) as exc:
            raise Unknown(f'{method} {path.split("?")[0]}: unavailable or malformed') from exc


def pages(api, path, field_name=None):
    """Every page or Unknown; a later page failing is a failed read, not a shorter list."""
    result, total = [], None
    for page in range(1, PAGE_LIMIT + 1):
        sep = '&' if '?' in path else '?'
        obj = api('GET', f'{path}{sep}per_page=100&page={page}')
        if field_name:
            obj = _dict(obj, path)
            count = _int(obj.get('total_count'), 'total_count')
            if total is None:
                total = count
            elif count != total:
                raise Unknown('total_count changed between pages')
            obj = obj.get(field_name)
        values = _list_of_dicts(obj, path)
        result.extend(values)
        if len(values) < 100:
            if total is not None and len(result) != total:
                raise Unknown(f'{path}: listing length {len(result)} differs from total_count {total}')
            return result
    raise Unknown('listing exceeded bounded pagination')


@dataclass
class Report:
    decisions: list = field(default_factory=list)
    dispatched: list = field(default_factory=list)
    cancelled: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _capture(fn):
    try:
        return fn()
    except Unknown as exc:
        return exc


def evaluate(api, source_repo, repo, live, report: Report, source_branch='main'):
    """Read everything for one target repository, then act. Returns nothing; fills report."""
    if not REPO.fullmatch(repo) or not REPO.fullmatch(source_repo):
        raise Unknown('repository outside supported owner')
    try:
        default_branch = _str(_dict(api('GET', 'repos/' + repo), 'repository').get('default_branch'),
                              'default_branch')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', default_branch):
            raise Unknown('unsupported default branch')
        policy = parse_policy(pages(api, f'repos/{repo}/rules/branches/{default_branch}'))
        prs = [parse_pr(x, repo, default_branch)
               for x in pages(api, f'repos/{repo}/pulls?state=open&base={default_branch}')]
        runs = []
        for status in ACTIVE_STATUSES:
            runs += pages(api, f'repos/{source_repo}/actions/workflows/{DISPATCH_WORKFLOW}/runs?status={status}',
                          'workflow_runs')
        active = parse_active_runs(runs)
    except Unknown as exc:
        report.errors.append(f'{repo}: {exc}')
        return

    # Cancel first, so its slot is not counted against capacity below.
    open_heads = {p.number: p.head for p in prs}
    stale = stale_runs(active, repo, open_heads)
    still_active = [r for r in active if r not in stale]
    for run in stale:
        jobs = _capture(lambda: pages(api, f'repos/{source_repo}/actions/runs/{run.id}/jobs', 'jobs'))
        try:
            if isinstance(jobs, Unknown):
                raise jobs
            if publish_started(jobs):
                report.decisions.append((repo, run.number, 'keep', f'run {run.id} for old head is publishing'))
                still_active.append(run)
                continue
        except Unknown as exc:
            report.errors.append(f'{repo}#{run.number}: cannot cancel run {run.id}: {exc}')
            still_active.append(run)
            continue
        if live:
            try:
                api('POST', f'repos/{source_repo}/actions/runs/{run.id}/cancel')
            except Unknown as exc:
                report.errors.append(f'{repo}#{run.number}: cancel of run {run.id} failed: {exc}')
                still_active.append(run)
                continue
        report.cancelled.append((repo, run.number, run.id, run.head))

    for pr in prs:
        if not pr.eligible:
            report.decisions.append((repo, pr.number, 'skip', pr.why_not))
            continue
        head, number = pr.head, pr.number
        check_runs = lambda: pages(api, f'repos/{repo}/commits/{head}/check-runs', 'check_runs')  # noqa: E731
        reviews = lambda: pages(api, f'repos/{repo}/pulls/{number}/reviews')  # noqa: E731
        compare = lambda: api('GET', f'repos/{repo}/compare/{default_branch}...{head}?per_page=1')  # noqa: E731
        commit = lambda: api('GET', f'repos/{repo}/git/commits/{head}')  # noqa: E731
        decision = decide(repo, pr, policy, still_active, check_runs, reviews, compare, commit)
        if decision.action == 'dispatch' and live:
            try:
                api('POST', f'repos/{source_repo}/actions/workflows/{DISPATCH_WORKFLOW}/dispatches',
                    {'ref': source_branch, 'inputs': {'repository': repo, 'pr_number': str(pr.number),
                                                      'head_sha': head}})
            except Unknown as exc:
                # The outcome of a failed write is unknown: the next round's
                # run listing tells whether it happened.
                report.errors.append(f'{repo}#{pr.number}: dispatch failed: {exc}')
                continue
        if decision.action == 'dispatch':
            report.dispatched.append((repo, pr.number, head))
            still_active.append(ActiveRun(-pr.number, 'queued', repo, pr.number, head))
        report.decisions.append((repo, pr.number, decision.action, decision.reason))


def render(report: Report, live: bool) -> str:
    lines = ['## Agy formal auto-dispatch (' + ('live' if live else 'dry run: nothing dispatched or cancelled') + ')', '']
    for repo, number, action, reason in report.decisions:
        lines.append(f'- {repo}#{number}: **{action}**: {reason}')
    for repo, number, head in report.dispatched:
        lines.append(f'- {"dispatched" if live else "would dispatch"} {repo}#{number} @ {head}')
    for repo, number, run_id, head in report.cancelled:
        lines.append(f'- {"cancelled" if live else "would cancel"} run {run_id} for {repo}#{number} @ {head}'
                     ' (no longer the PR head)')
    for error in report.errors:
        lines.append(f'- ERROR (nothing dispatched for it; retried next round): {error}')
    return '\n'.join(lines) + '\n'


def main(env=os.environ, api=None, out=None) -> int:
    source_repo = env['GITHUB_REPOSITORY']
    repos = env.get('AUTO_DISPATCH_REPOSITORIES', '').split()
    live = env.get('LIVE') == 'true'
    api = api or API(env.get('GITHUB_TOKEN'))
    report = Report()
    try:
        source_branch = _str(_dict(api('GET', 'repos/' + source_repo), 'source repository').get('default_branch'),
                             'source default_branch')
        if env.get('GITHUB_REF') != 'refs/heads/' + source_branch:
            raise Unknown('runs only from the default branch of ' + source_repo)
        for repo in repos:
            evaluate(api, source_repo, repo, live, report, source_branch)
    except Unknown as exc:
        report.errors.append(str(exc))
    text = render(report, live)
    print(text, file=out or sys.stdout)
    summary = env.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as stream:
            stream.write(text)
    return 1 if report.errors else 0


if __name__ == '__main__':
    sys.exit(main())
