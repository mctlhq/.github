"""Central trusted review-gate orchestrator (mctlhq/.github#398).

Runs strictly from default branch `main` on mctlhq/.github. Evaluates open
non-draft same-repo PRs targeting the repository default branch, preserves
blocking Claude/Agy CHANGES_REQUESTED precedence, marks `independent-review` (or
`independent-review-shadow` in shadow mode) green on trusted Claude PASS without
invoking Antigravity, and dispatches formal Antigravity fallback ONLY on
allowlisted transient failures (NO_VERDICT_TRANSIENT) or trusted workflow
SELF_EDIT.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import pr_diff_digest  # noqa: E402
import review_trust  # noqa: E402

SHA = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z')
REPO = re.compile(r'mctlhq/[A-Za-z0-9_.-]+')
DISPATCH_WORKFLOW = 'agy-formal-dispatch.yml'
ENFORCE_CHECK = 'independent-review'
SHADOW_CHECK = 'independent-review-shadow'
FORMAL_CHECK_LEGACY = 'agy-formal-review'
ALLOWED_GATE_MODES = frozenset({'enforce', 'shadow'})
ACTIVE_STATUSES = ('queued', 'in_progress', 'requested', 'waiting', 'pending')
MAX_ACTIVE_FORMAL_RUNS = 2
PAGE_LIMIT = 20

RUN_TITLE_RE = re.compile(
    r'Agy formal review (mctlhq/[A-Za-z0-9_.-]+)#([1-9][0-9]*) @ ([0-9a-f]{40}|any)'
    r'(?: \[(agy-formal-review|independent-review):(enforce|shadow)\])?'
)
EXTERNAL_ID_RE = re.compile(
    r'agy/v([12]):(mctlhq/[A-Za-z0-9_.-]+):([1-9][0-9]*):([0-9]+):([0-9]+)(?::(sha256:[0-9a-f]{64}))?'
)
CONTROLLER_EXT_ID_RE = re.compile(
    r'gate/v1:(mctlhq/[A-Za-z0-9_.-]+):([1-9][0-9]*):([0-9a-f]{40}):(enforce|shadow):(sha256:[0-9a-f]{64})'
)

OUTCOME_PASS = 'PASS'
OUTCOME_BLOCKING = 'BLOCKING_FINDINGS'
OUTCOME_TRANSIENT = 'NO_VERDICT_TRANSIENT'
OUTCOME_SELF_EDIT = 'SELF_EDIT'
OUTCOME_OTHER = 'NO_VERDICT_OTHER'
OUTCOME_STALE = 'STALE'
OUTCOME_UNKNOWN = 'UNKNOWN'


class Unknown(Exception):
    """Any unreadable, partial, or malformed API read."""


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


def _parse_ts(ts: str) -> datetime:
    if not isinstance(ts, str) or not TIMESTAMP.fullmatch(ts):
        raise Unknown('timestamp is not UTC ISO-8601')
    return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def target_check_name(gate_mode: str) -> str:
    if gate_mode not in ALLOWED_GATE_MODES:
        raise Unknown(f'invalid gate_mode: {gate_mode!r}')
    return SHADOW_CHECK if gate_mode == 'shadow' else ENFORCE_CHECK


def parse_repo_configs(raw: str, override_repo: str = '', override_mode: str = '') -> list[tuple[str, str]]:
    """Parse `<owner/repo>:<mode>` list and apply optional safe manual filters (downgrade-only)."""
    configs: dict[str, str] = {}
    for token in raw.replace(',', ' ').split():
        if ':' in token:
            repo, mode = token.split(':', 1)
        else:
            repo, mode = token, 'shadow'
        if not REPO.fullmatch(repo) or mode not in ALLOWED_GATE_MODES:
            raise Unknown(f'invalid controller repo config {token!r}')
        if repo in configs:
            raise Unknown(f'duplicate controller repo config {repo!r}')
        configs[repo] = mode
    if not configs:
        return []
    items = list(configs.items())
    if override_repo:
        if override_repo not in configs:
            raise Unknown(f'repository {override_repo!r} is not configured on controller')
        items = [(override_repo, configs[override_repo])]
    if override_mode in ('', 'config'):
        return items
    if override_mode not in ALLOWED_GATE_MODES:
        raise Unknown(f'invalid gate_mode override {override_mode!r}')
    out = []
    for repo, configured in items:
        if override_mode == 'enforce' and configured == 'shadow':
            raise Unknown(f'cannot promote shadow-configured repository {repo} to enforce via dispatch')
        out.append((repo, override_mode))
    return out


class API:
    def __init__(self, token: str):
        if not token:
            raise Unknown('missing token')
        self.token = token

    def __call__(self, method: str, path: str, payload=None):
        req = urllib.request.Request(
            'https://api.github.com/' + path,
            method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                'Authorization': 'Bearer ' + self.token,
                'Accept': 'application/vnd.github+json',
                'Content-Type': 'application/json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
        )
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


def pages(api, path: str, field_name: str | None = None) -> list[dict]:
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


@dataclass(frozen=True)
class Policy:
    strict: bool
    stale_dismissal: bool
    queued: bool
    required_bound: bool


def parse_policy(rules, classic, app_id: int, check_name: str = ENFORCE_CHECK) -> Policy:
    rules = _list_of_dicts(rules, 'branch rules')
    stale = False
    strict = False
    queued = False
    bound = False
    for rule in rules:
        params = _dict(rule.get('parameters') or {}, 'rule parameters')
        rtype = rule.get('type')
        if rtype == 'merge_queue':
            queued = True
        elif rtype == 'pull_request':
            stale |= params.get('dismiss_stale_reviews_on_push') is True
        elif rtype == 'required_status_checks':
            strict |= params.get('strict_required_status_checks_policy') is True
            for chk in _list_of_dicts(params.get('required_status_checks') or [], 'required checks'):
                if chk.get('context') == check_name and chk.get('integration_id') == app_id:
                    bound = True
    if classic is not None:
        if not isinstance(classic, dict):
            raise Unknown('classic protection malformed')
        stale |= (classic.get('required_pull_request_reviews') or {}).get('dismiss_stale_reviews') is True
        checks = classic.get('required_status_checks') or {}
        strict |= checks.get('strict') is True
        bound |= any(x.get('context') == check_name and x.get('app_id') == app_id for x in checks.get('checks', []))
    return Policy(strict=strict, stale_dismissal=stale, queued=queued, required_bound=bound)


@dataclass(frozen=True)
class PullRequest:
    number: int
    head: str
    base: str
    base_ref: str
    author: str
    eligible: bool
    why_not: str = ''


def parse_pr(pr, repo: str, default_branch: str) -> PullRequest:
    pr = _dict(pr, 'pull request')
    number = _int(pr.get('number'), 'PR number')
    head = _dict(pr.get('head'), 'PR head')
    base = _dict(pr.get('base'), 'PR base')
    head_sha = _sha(head.get('sha'), 'PR head sha')
    base_sha = _sha(base.get('sha'), 'PR base sha')
    draft = pr.get('draft')
    if type(draft) is not bool or pr.get('state') != 'open':
        raise Unknown('PR state/draft malformed')
    head_repo = head.get('repo')
    head_name = head_repo.get('full_name') if isinstance(head_repo, dict) else None
    base_name = _dict(base.get('repo'), 'PR base repo').get('full_name')
    if base_name != repo:
        raise Unknown('PR base repository mismatch')
    user = pr.get('user')
    author = user.get('login') if isinstance(user, dict) else None
    if not isinstance(author, str) or not author:
        raise Unknown('PR author missing')
    base_ref = _str(base.get('ref'), 'PR base ref')
    if draft:
        return PullRequest(number, head_sha, base_sha, base_ref, author, False, 'draft')
    if head_name != repo:
        return PullRequest(number, head_sha, base_sha, base_ref, author, False, 'fork or deleted head repository')
    if base_ref != default_branch:
        return PullRequest(number, head_sha, base_sha, base_ref, author, False, 'base is not default branch')
    return PullRequest(number, head_sha, base_sha, base_ref, author, True)


@dataclass(frozen=True)
class ActiveRun:
    id: int
    status: str
    repository: str | None
    number: int | None
    head: str | None
    check_name: str | None = None
    gate_mode: str | None = None
    created_at: str = ''


def parse_dispatch_runs(runs, statuses=ACTIVE_STATUSES) -> list[ActiveRun]:
    out = {}
    for run in _list_of_dicts(runs, 'workflow runs'):
        run_id = _int(run.get('id'), 'run id')
        status = _str(run.get('status'), 'run status')
        if statuses is not None and status not in statuses:
            continue
        match = RUN_TITLE_RE.fullmatch(_str(run.get('display_title'), 'run display_title'))
        if match:
            head = None if match[3] == 'any' else match[3]
            out[run_id] = ActiveRun(
                id=run_id, status=status, repository=match[1], number=int(match[2]),
                head=head, check_name=match[4], gate_mode=match[5],
                created_at=str(run.get('created_at') or ''),
            )
        else:
            out[run_id] = ActiveRun(run_id, status, None, None, None, created_at=str(run.get('created_at') or ''))
    return sorted(out.values(), key=lambda r: r.id)


def publish_started(jobs) -> bool:
    for job in _list_of_dicts(jobs, 'run jobs'):
        name = _str(job.get('name'), 'job name')
        status = _str(job.get('status'), 'job status')
        if re.search(r'(^|/ )publish$', name) and status in ('in_progress', 'completed'):
            return True
    return False


def classify_claude_run(
    api,
    repo: str,
    run: dict,
    default_branch: str,
    head_sha: str,
    merge_base_sha: str,
    reviews: list[dict],
) -> tuple[str, str]:
    """Classify a completed Claude workflow run into an outcome state with caller-trust check."""
    run_id = _int(run.get('id'), 'claude run id')
    jobs = pages(api, f'repos/{repo}/actions/runs/{run_id}/jobs', 'jobs')
    review_jobs = [j for j in jobs if j.get('name') == 'review' or str(j.get('name', '')).endswith('/ review')]
    if not review_jobs:
        gate_jobs = [j for j in jobs if j.get('name') == 'gate' or str(j.get('name', '')).endswith('/ gate')]
        if gate_jobs and gate_jobs[0].get('conclusion') not in ('success', 'skipped'):
            return OUTCOME_OTHER, 'claude gate job failed'
        return OUTCOME_OTHER, 'claude run has no review job'
    job = sorted(review_jobs, key=lambda j: int(j.get('id', 0)))[-1]
    conclusion = _str(job.get('conclusion'), 'claude review job conclusion')
    steps = _list_of_dicts(job.get('steps') or [], 'claude review steps')
    step_map = {s.get('name'): s for s in steps if isinstance(s.get('name'), str)}

    def step_succeeded(name: str) -> bool:
        s = step_map.get(name)
        return s is not None and s.get('status') == 'completed' and s.get('conclusion') == 'success'

    # Verify caller run provenance before accepting SELF_EDIT or NO_VERDICT_TRANSIENT.
    is_self_edit = step_succeeded('Classify outcome: SELF_EDIT')
    if is_self_edit:
        try:
            trusted, reason = review_trust.verify_run_caller_trust(
                lambda p: api('GET', p), repo, run, default_branch, head_sha, merge_base_sha,
                require_unchanged_blob=False,
            )
        except review_trust.TrustError as exc:
            raise Unknown(str(exc)) from None
        if not trusted:
            return OUTCOME_OTHER, 'untrusted SELF_EDIT run: ' + reason
        return OUTCOME_SELF_EDIT, 'trusted Claude self-edit deferral'

    if step_succeeded('Classify outcome: STALE'):
        return OUTCOME_STALE, 'Claude run marked head stale'
    if step_succeeded('Classify outcome: BLOCKING_FINDINGS'):
        return OUTCOME_BLOCKING, 'Claude run reported blocking findings'

    is_transient_marker = step_succeeded('Classify outcome: NO_VERDICT_TRANSIENT')
    is_timeout_without_verdict = (
        conclusion in ('timed_out', 'startup_failure')
        and not any(
            isinstance(r.get('user'), dict)
            and r['user'].get('login') == review_trust.CLAUDE_LOGIN
            and r.get('commit_id') == head_sha
            and r.get('state') in ('APPROVED', 'CHANGES_REQUESTED')
            for r in reviews
        )
    )
    if is_transient_marker or is_timeout_without_verdict:
        try:
            trusted, reason = review_trust.verify_run_caller_trust(
                lambda p: api('GET', p), repo, run, default_branch, head_sha, merge_base_sha,
                require_unchanged_blob=True,
            )
        except review_trust.TrustError as exc:
            raise Unknown(str(exc)) from None
        if not trusted:
            return OUTCOME_OTHER, 'untrusted Claude transient run: ' + reason
        why = 'Claude quota/transient marker' if is_transient_marker else f'Claude job {conclusion}'
        return OUTCOME_TRANSIENT, why

    if step_succeeded('Classify outcome: NO_VERDICT_OTHER') or conclusion in ('failure', 'cancelled'):
        return OUTCOME_OTHER, f'Claude run ended with {conclusion}/NO_VERDICT_OTHER'
    return OUTCOME_OTHER, f'Claude run finished ({conclusion}) without a verifiable PASS'


def find_qualifying_agy_reuse(
    api,
    source_repo: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    live_digest: str,
    check_runs: list[dict],
    reviews: list[dict],
    app_id: int,
    agy_login: str,
    gate_mode: str,
    target_check: str,
) -> tuple[bool, dict | None]:
    """Return (True, check_run) when an exact-head/digest formal Agy result qualifies for reuse."""
    allowed_contexts = (SHADOW_CHECK,) if gate_mode == 'shadow' else (ENFORCE_CHECK, FORMAL_CHECK_LEGACY)
    candidates = []
    for c in check_runs:
        if c.get('name') not in allowed_contexts:
            continue
        if _dict(c.get('app'), 'check app').get('id') != app_id:
            continue
        if c.get('status') != 'completed' or c.get('conclusion') != 'success' or c.get('head_sha') != head_sha:
            continue
        eid = str(c.get('external_id', ''))
        m = EXTERNAL_ID_RE.fullmatch(eid)
        if not m or m[1] != '2' or m[2] != source_repo or int(m[3]) != pr_number or m[6] != live_digest:
            continue
        out = c.get('output')
        if not isinstance(out, dict) or str(out.get('title', '')).startswith('Agy formal review: success (carried'):
            continue
        # Verify backing workflow run in source_repo came from agy-formal-review.yml on default branch.
        run_id = int(m[4])
        run_obj = api('GET', f'repos/{source_repo}/actions/runs/{run_id}')
        refs = _dict(run_obj, 'formal run').get('referenced_workflows')
        if not isinstance(refs, list) or not any(
            isinstance(x, dict) and str(x.get('path', '')).startswith(review_trust.TRUSTED_AGY_REUSABLE)
            for x in refs
        ):
            continue
        candidates.append((c, m[4], m[5]))
    if not candidates:
        return False, None

    if gate_mode == 'shadow':
        # Completed non-carried independent-review-shadow check for exact head & digest prevents */5 re-dispatch.
        shadow_hits = [item[0] for item in candidates if item[0].get('name') == target_check]
        if shadow_hits:
            return True, shadow_hits[-1]

    # For enforce (or if shadow sees an actual PR review approval), verify active unedited mctl-agy-reviewer APPROVED review.
    for chk, rid, att in candidates:
        review_nodes = []
        for r in reviews:
            u = r.get('user')
            if not isinstance(u, dict) or u.get('login') != agy_login or u.get('type') != 'Bot':
                continue
            if r.get('commit_id') != head_sha or r.get('state') != 'APPROVED':
                continue
            if f'<!-- agy-formal:{rid}:{att}:{head_sha}:PASS -->' not in str(r.get('body', '')):
                continue
            nid = r.get('node_id')
            if not isinstance(nid, str) or not nid:
                raise Unknown('Agy APPROVED review missing node_id')
            review_nodes.append(nid)
        if review_nodes:
            try:
                unedited = review_trust.unedited_review_ids(lambda q: api('POST', 'graphql', q), review_nodes)
            except review_trust.TrustError as exc:
                raise Unknown(str(exc)) from None
            if unedited:
                return True, chk
    return False, None


def has_terminal_fallback_failure(
    api,
    source_repo: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    live_digest: str,
    check_runs: list[dict],
    completed_dispatches: list[dict],
    app_id: int,
    gate_mode: str,
    target_check: str,
) -> tuple[bool, str]:
    """Return True if a completed formal fallback attempt already reached terminal failure on this scope."""
    for c in check_runs:
        if c.get('name') != target_check or _dict(c.get('app'), 'check app').get('id') != app_id:
            continue
        if c.get('status') != 'completed' or c.get('conclusion') != 'failure' or c.get('head_sha') != head_sha:
            continue
        eid = str(c.get('external_id', ''))
        m = EXTERNAL_ID_RE.fullmatch(eid)
        if m and m[1] == '2' and m[2] == source_repo and int(m[3]) == pr_number:
            # Terminal formal publisher check failure on this head (if digest present, must match).
            if m[6] is None or m[6] == live_digest:
                return True, f'existing terminal {target_check} failure (check {c.get("id")})'
        cm = CONTROLLER_EXT_ID_RE.fullmatch(eid)
        if cm and cm[1] == repo and int(cm[2]) == pr_number and cm[3] == head_sha and cm[4] == gate_mode and cm[5] == live_digest:
            return True, f'existing controller {target_check} failure on same head/digest/mode'

    for run in completed_dispatches:
        title = _str(run.get('display_title'), 'dispatch title')
        tm = RUN_TITLE_RE.fullmatch(title)
        if not tm or tm[1] != repo or int(tm[2]) != pr_number or tm[3] != head_sha:
            continue
        if tm[4] not in (target_check, None) and not (gate_mode == 'shadow' and tm[4] == ENFORCE_CHECK):
            continue
        if tm[5] not in (gate_mode, None):
            continue
        conc = run.get('conclusion')
        if conc not in ('failure', 'cancelled', 'timed_out'):
            continue
        run_id = _int(run.get('id'), 'completed dispatch run id')
        jobs = pages(api, f'repos/{source_repo}/actions/runs/{run_id}/jobs', 'jobs')
        # Exclude prepare-only UNKNOWN API/runner errors so transient infra blips can retry.
        review_or_publish_ran = any(
            (str(j.get('name', '')).endswith('review') or str(j.get('name', '')).endswith('publish'))
            and j.get('conclusion') in ('failure', 'timed_out', 'success')
            for j in jobs
        )
        if review_or_publish_ran:
            return True, f'completed formal dispatch run {run_id} ended with terminal failure'
    return False, ''


@dataclass
class Decision:
    outcome: str
    action: str  # 'pass_check', 'fail_check', 'dispatch', 'wait', 'skip', 'unknown', 'done'
    reason: str
    trigger_reason: str = ''


def upsert_check_run(api, repo: str, app_id: int, check_name: str, head_sha: str, status: str,
                     conclusion: str | None, external_id: str, title: str, summary: str,
                     details_url: str) -> None:
    existing = [
        c for c in pages(api, f'repos/{repo}/commits/{head_sha}/check-runs', 'check_runs')
        if c.get('name') == check_name and _dict(c.get('app'), 'check app').get('id') == app_id
    ]
    payload: dict = {
        'name': check_name,
        'status': status,
        'external_id': external_id,
        'details_url': details_url,
        'output': {'title': title, 'summary': summary},
    }
    if conclusion is not None:
        payload['conclusion'] = conclusion
    if existing:
        latest = max(existing, key=lambda c: int(c.get('id', 0)))
        api('PATCH', f'repos/{repo}/check-runs/{_int(latest.get("id"), "check id")}', payload)
    else:
        payload['head_sha'] = head_sha
        api('POST', f'repos/{repo}/check-runs', payload)


@dataclass
class Metrics:
    claude_evaluated: int = 0
    agy_fallback_dispatched: int = 0
    no_verdict_by_provider: dict = field(default_factory=lambda: {
        'claude': {OUTCOME_TRANSIENT: 0, OUTCOME_OTHER: 0, OUTCOME_UNKNOWN: 0},
        'agy': {OUTCOME_TRANSIENT: 0, OUTCOME_OTHER: 0, OUTCOME_UNKNOWN: 0},
    })
    queue_wait_seconds: list[float] = field(default_factory=list)
    time_to_valid_verdict_seconds: list[float] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: list[tuple[str, int, int, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        denom = self.claude_evaluated + self.agy_fallback_dispatched
        return {
            'fallback_rate': round(self.agy_fallback_dispatched / denom, 4) if denom else 0.0,
            'claude_evaluated': self.claude_evaluated,
            'agy_fallback_dispatched': self.agy_fallback_dispatched,
            'no_verdict_rate': self.no_verdict_by_provider,
            'queue_wait_seconds': self.queue_wait_seconds,
            'time_to_valid_verdict_seconds': self.time_to_valid_verdict_seconds,
            'decisions': self.decisions,
            'errors': self.errors,
        }


def decide_pr(
    api,
    source_repo: str,
    repo: str,
    default_branch: str,
    pr: PullRequest,
    policy: Policy,
    gate_mode: str,
    target_check: str,
    agy_login: str,
    app_id: int,
    active_dispatches: list[ActiveRun],
    completed_dispatches: list[dict],
    force_rerun: bool,
    compute_digest_fn: Callable[[str, str, str], str],
    metrics: Metrics,
    actions_api=None,
) -> Decision:
    actions_api = actions_api or api
    if not pr.eligible:
        return Decision(OUTCOME_OTHER, 'skip', pr.why_not)
    if pr.author == agy_login:
        return Decision(OUTCOME_OTHER, 'fail_check', 'PR author equals formal reviewer identity')

    # Never approve unknown-active-dispatch runs.
    if any(r.repository is None for r in active_dispatches):
        return Decision(OUTCOME_UNKNOWN, 'unknown', 'an active formal dispatch run does not name its PR')

    # Compute authoritative live merge-base and pr_diff_digest_v1.
    cmp_obj = _dict(api('GET', f'repos/{repo}/compare/{pr.base}...{pr.head}?per_page=1'), 'compare')
    mb_obj = cmp_obj.get('merge_base_commit')
    merge_base = _sha(mb_obj.get('sha') if isinstance(mb_obj, dict) else None, 'merge_base')
    try:
        live_digest = compute_digest_fn(repo, merge_base, pr.head)
    except Exception as exc:
        raise Unknown(f'could not compute live pr_diff_digest_v1: {type(exc).__name__}') from None

    reviews = pages(api, f'repos/{repo}/pulls/{pr.number}/reviews')
    check_runs = pages(api, f'repos/{repo}/commits/{pr.head}/check-runs', 'check_runs')

    # 1. Blocking precedence before any PASS or caller-trust filter.
    try:
        if review_trust.has_blocking_changes_requested(reviews, pr.head, (review_trust.CLAUDE_LOGIN, agy_login)):
            metrics.claude_evaluated += 1
            return Decision(OUTCOME_BLOCKING, 'fail_check', f'CHANGES_REQUESTED present on {pr.head}')
    except review_trust.TrustError as exc:
        raise Unknown(str(exc)) from None

    all_repo_runs = pages(actions_api, f'repos/{repo}/actions/workflows/'
                               f'{review_trust.caller_allowlist_for(repo)[0].rsplit("/", 1)[1]}/runs', 'workflow_runs')
    claude_runs = review_trust.find_matching_claude_runs(all_repo_runs, pr.number, pr.head)

    # 2. Check trusted unedited Claude PASS.
    try:
        def _trusted_get(path: str):
            if '/actions/runs' in path:
                return actions_api('GET', path)
            return api('GET', path)

        claude_ok, _, rev_base = review_trust.verify_claude_pass(
            reviews=reviews,
            graphql_fn=lambda q: api('POST', 'graphql', q),
            api_get=_trusted_get,
            repo=repo,
            pr_number=pr.number,
            default_branch=default_branch,
            head_sha=pr.head,
            live_base_sha=pr.base,
            merge_base_sha=merge_base,
            strict=policy.strict,
            live_diff_digest=live_digest,
            claude_runs=claude_runs,
            compute_digest_fn=compute_digest_fn,
        )
    except review_trust.TrustError as exc:
        raise Unknown(str(exc)) from None
    if claude_ok:
        metrics.claude_evaluated += 1
        return Decision(OUTCOME_PASS, 'pass_check', f'trusted claude[bot] PASS for {pr.head} (base {rev_base})')

    # 3. Inspect matching Claude runs for active, transient, self-edit, or non-transient states.
    if any(r.get('status') in ACTIVE_STATUSES for r in claude_runs):
        return Decision(OUTCOME_STALE, 'wait', 'Claude review is still running on this PR')

    completed_claude = sorted(
        [r for r in claude_runs if r.get('status') == 'completed'],
        key=lambda r: int(r.get('id', 0)),
        reverse=True,
    )
    if not completed_claude:
        return Decision(OUTCOME_UNKNOWN, 'wait', 'no matching Claude run completed yet for this head')

    claude_outcome, claude_reason = classify_claude_run(
        lambda m, p, payload=None: (actions_api if '/actions/' in p else api)(m, p, payload),
        repo, completed_claude[0], default_branch, pr.head, merge_base, reviews
    )
    metrics.claude_evaluated += 1
    if claude_outcome in (OUTCOME_TRANSIENT, OUTCOME_SELF_EDIT):
        metrics.no_verdict_by_provider['claude'][OUTCOME_TRANSIENT] += 1
    elif claude_outcome == OUTCOME_OTHER:
        metrics.no_verdict_by_provider['claude'][OUTCOME_OTHER] += 1
        return Decision(OUTCOME_OTHER, 'fail_check', claude_reason)
    elif claude_outcome == OUTCOME_BLOCKING:
        return Decision(OUTCOME_BLOCKING, 'fail_check', claude_reason)
    elif claude_outcome == OUTCOME_STALE:
        return Decision(OUTCOME_STALE, 'wait', claude_reason)

    # 4. Reuse qualifying formal Agy result, preserve terminal failure, or dispatch.
    reused, _ = find_qualifying_agy_reuse(
        lambda m, p, payload=None: (actions_api if '/actions/' in p else api)(m, p, payload),
        source_repo, repo, pr.number, pr.head, live_digest,
        check_runs, reviews, app_id, agy_login, gate_mode, target_check,
    )
    if reused:
        return Decision(OUTCOME_PASS, 'pass_check', f'reused qualifying formal Agy result for {pr.head}')

    if not force_rerun:
        term_fail, term_reason = has_terminal_fallback_failure(
            lambda m, p, payload=None: (actions_api if '/actions/' in p else api)(m, p, payload),
            source_repo, repo, pr.number, pr.head, live_digest,
            check_runs, completed_dispatches, app_id, gate_mode, target_check,
        )
        if term_fail:
            metrics.no_verdict_by_provider['agy'][OUTCOME_OTHER] += 1
            return Decision(OUTCOME_OTHER, 'fail_check', term_reason)

    mine = [r for r in active_dispatches if (r.repository, r.number) == (repo, pr.number)]
    if any(r.head in (pr.head, None) for r in mine):
        return Decision(claude_outcome, 'wait', 'formal Agy dispatch already active for this PR/head')
    if len(active_dispatches) >= MAX_ACTIVE_FORMAL_RUNS:
        return Decision(claude_outcome, 'wait', 'formal runner capacity is in use')

    return Decision(
        claude_outcome, 'dispatch',
        f'{claude_reason}; dispatching formal Agy fallback ({gate_mode})',
        trigger_reason=f'{claude_outcome}:{live_digest}',
    )


def evaluate_repo(
    api,
    dispatch_api,
    source_repo: str,
    repo: str,
    gate_mode: str,
    app_id: int,
    agy_login: str,
    live: bool,
    force_rerun: bool,
    pr_filter: int | None,
    details_url: str,
    compute_digest_fn: Callable[[str, str, str], str],
    metrics: Metrics,
    source_branch: str = 'main',
) -> None:
    target_check = target_check_name(gate_mode)
    try:
        default_branch = _str(_dict(api('GET', f'repos/{repo}'), 'repository').get('default_branch'), 'default_branch')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', default_branch):
            raise Unknown('unsupported default branch')
        rules = pages(api, f'repos/{repo}/rules/branches/{default_branch}')
        try:
            classic = api('GET', f'repos/{repo}/branches/{default_branch}/protection')
        except Unknown as exc:
            if 'HTTP 404' in str(exc):
                classic = None
            else:
                raise
        policy = parse_policy(rules, classic, app_id, ENFORCE_CHECK)
        if gate_mode == 'enforce' and not (policy.stale_dismissal and (policy.strict or policy.queued) and policy.required_bound):
            raise Unknown(f'{repo} lacks enforceable policy (stale_dismissal, strict/merge_queue, bound {ENFORCE_CHECK})')
        prs = [parse_pr(x, repo, default_branch) for x in pages(api, f'repos/{repo}/pulls?state=open&base={default_branch}')]
        if pr_filter is not None:
            prs = [p for p in prs if p.number == pr_filter]
        active_raw = []
        for status in ACTIVE_STATUSES:
            active_raw += pages(dispatch_api, f'repos/{source_repo}/actions/workflows/{DISPATCH_WORKFLOW}/runs?status={status}', 'workflow_runs')
        active_dispatches = parse_dispatch_runs(active_raw, ACTIVE_STATUSES)
        completed_dispatches = pages(
            dispatch_api, f'repos/{source_repo}/actions/workflows/{DISPATCH_WORKFLOW}/runs?status=completed', 'workflow_runs'
        )[:50]
    except Unknown as exc:
        metrics.errors.append(f'{repo}: {exc}')
        return

    # Cancel superseded queued/running formal runs for older heads unless `publish` started.
    open_heads = {p.number: p.head for p in prs}
    stale = [r for r in active_dispatches if r.repository == repo and r.number in open_heads and r.head and r.head != open_heads[r.number]]
    still_active = [r for r in active_dispatches if r not in stale]
    for run in stale:
        try:
            jobs = pages(dispatch_api, f'repos/{source_repo}/actions/runs/{run.id}/jobs', 'jobs')
            if publish_started(jobs):
                still_active.append(run)
                continue
            if live:
                dispatch_api('POST', f'repos/{source_repo}/actions/runs/{run.id}/cancel')
            metrics.cancelled.append((repo, run.number or 0, run.id, run.head or ''))
        except Unknown as exc:
            metrics.errors.append(f'{repo}#{run.number}: cancel failed for run {run.id}: {exc}')
            still_active.append(run)

    for pr in prs:
        try:
            dec = decide_pr(
                api, source_repo, repo, default_branch, pr, policy, gate_mode, target_check,
                agy_login, app_id, still_active, completed_dispatches, force_rerun,
                compute_digest_fn, metrics, actions_api=dispatch_api,
            )
            ext_id = f'gate/v1:{repo}:{pr.number}:{pr.head}:{gate_mode}'
            if ':' in dec.trigger_reason:
                ext_id += ':' + dec.trigger_reason.split(':', 1)[1]
            else:
                ext_id += ':sha256:' + ('0' * 64)

            if live and dec.action == 'pass_check':
                upsert_check_run(
                    api, repo, app_id, target_check, pr.head, 'completed', 'success',
                    ext_id, f'{target_check}: satisfied', dec.reason, details_url,
                )
            elif live and dec.action == 'fail_check':
                upsert_check_run(
                    api, repo, app_id, target_check, pr.head, 'completed', 'failure',
                    ext_id, f'{target_check}: failed', dec.reason, details_url,
                )
            elif dec.action == 'dispatch':
                if live:
                    upsert_check_run(
                        api, repo, app_id, target_check, pr.head, 'in_progress', None,
                        ext_id, f'{target_check}: awaiting formal Agy fallback', dec.reason, details_url,
                    )
                    dispatch_api(
                        'POST', f'repos/{source_repo}/actions/workflows/{DISPATCH_WORKFLOW}/dispatches',
                        {'ref': source_branch, 'inputs': {
                            'repository': repo,
                            'pr_number': str(pr.number),
                            'head_sha': pr.head,
                            'check_name': ENFORCE_CHECK,
                            'gate_mode': gate_mode,
                        }},
                    )
                metrics.agy_fallback_dispatched += 1
                still_active.append(ActiveRun(-pr.number, 'queued', repo, pr.number, pr.head, ENFORCE_CHECK, gate_mode))
            metrics.decisions.append({
                'repository': repo, 'pr_number': pr.number, 'head_sha': pr.head,
                'gate_mode': gate_mode, 'target_check': target_check,
                'outcome': dec.outcome, 'action': dec.action, 'reason': dec.reason,
            })
        except Unknown as exc:
            metrics.no_verdict_by_provider['agy'][OUTCOME_UNKNOWN] += 1
            metrics.errors.append(f'{repo}#{pr.number}: {exc}')


def render_summary(metrics: Metrics, live: bool) -> str:
    d = metrics.to_dict()
    lines = [
        f'## Independent Review Gate Controller ({"live" if live else "dry-run"})',
        '',
        f'- `fallback_rate`: `{d["fallback_rate"]}` ({d["agy_fallback_dispatched"]} dispatches / '
        f'{d["claude_evaluated"] + d["agy_fallback_dispatched"]} evaluated)',
        f'- `claude` no-verdict counts: `{json.dumps(d["no_verdict_rate"]["claude"])}`',
        f'- `agy` no-verdict counts: `{json.dumps(d["no_verdict_rate"]["agy"])}`',
        '',
        '| Repository / PR | Mode | Outcome | Action | Reason |',
        '| --- | --- | --- | --- | --- |',
    ]
    for item in metrics.decisions:
        lines.append(
            f'| {item["repository"]}#{item["pr_number"]} | `{item["gate_mode"]}` | '
            f'`{item["outcome"]}` | `{item["action"]}` | {item["reason"]} |'
        )
    for err in metrics.errors:
        lines.append(f'- ERROR (`UNKNOWN`, no approval): {err}')
    return '\n'.join(lines) + '\n'


def main(env=os.environ, api=None, dispatch_api=None, compute_digest_fn=None) -> int:
    source_repo = env['GITHUB_REPOSITORY']
    live = env.get('LIVE') == 'true'
    force_rerun = env.get('FORCE_RERUN') == 'true'
    raw_pr = (env.get('PR_NUMBER') or '').strip()
    pr_filter = int(raw_pr) if raw_pr else None
    app_id = int(env.get('AGY_REVIEW_APP_ID') or '5732062')
    agy_login = env.get('AGY_REVIEW_LOGIN') or 'mctl-agy-reviewer[bot]'
    details_url = env.get('CONTROLLER_RUN_URL') or f'https://github.com/{source_repo}/actions'

    target_token = env.get('AGY_REVIEW_TOKEN') or env.get('GITHUB_TOKEN') or ''
    gh_token = env.get('GITHUB_TOKEN') or target_token
    api = api or API(target_token)
    dispatch_api = dispatch_api or API(gh_token)
    if compute_digest_fn is None:
        compute_digest_fn = lambda r, mb, h: pr_diff_digest.compute_remote_diff_digest(r, mb, h, token=target_token)  # noqa: E731

    metrics = Metrics()
    try:
        if not REPO.fullmatch(source_repo):
            raise Unknown('invalid source repository')
        src_obj = _dict(dispatch_api('GET', f'repos/{source_repo}'), 'source repo')
        src_branch = _str(src_obj.get('default_branch'), 'source default_branch')
        if env.get('GITHUB_REF') != 'refs/heads/' + src_branch:
            raise Unknown('controller must run only on default branch')
        configs = parse_repo_configs(
            env.get('GATE_REPOSITORIES', 'mctlhq/mctl-gitops:shadow'),
            override_repo=(env.get('OVERRIDE_REPOSITORY') or '').strip(),
            override_mode=(env.get('OVERRIDE_GATE_MODE') or '').strip(),
        )
        for repo, gate_mode in configs:
            evaluate_repo(
                api, dispatch_api, source_repo, repo, gate_mode, app_id, agy_login,
                live, force_rerun, pr_filter, details_url, compute_digest_fn, metrics, src_branch,
            )
    except Unknown as exc:
        metrics.errors.append(str(exc))

    metrics_path = env.get('METRICS_PATH')
    if metrics_path:
        Path(metrics_path).write_text(json.dumps(metrics.to_dict(), indent=2) + '\n', encoding='utf-8')
    summary_text = render_summary(metrics, live)
    print(summary_text)
    if env.get('GITHUB_STEP_SUMMARY'):
        with open(env['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(summary_text)
    return 1 if metrics.errors else 0


if __name__ == '__main__':
    sys.exit(main())
