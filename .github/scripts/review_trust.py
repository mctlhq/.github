"""Shared fail-closed review & caller-workflow trust verification helpers."""
from __future__ import annotations

import re
from typing import Callable

SHA = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
REVIEWED_RE = re.compile(r'Reviewed head=([0-9a-f]{40}(?:[0-9a-f]{24})?) base=([0-9a-f]{40}(?:[0-9a-f]{24})?)')
AGY_FORMAL_MARKER_RE = re.compile(
    r'<!-- agy-formal:([0-9]+):([0-9]+):([0-9a-f]{40}):(PASS|FAIL:P1|FAIL:P2|FAIL:P1,P2) -->'
)
CLAUDE_LOGIN = 'claude[bot]'
TRUSTED_CLAUDE_REUSABLE = 'mctlhq/.github/.github/workflows/claude-review.yml@'
TRUSTED_AGY_REUSABLE = 'mctlhq/.github/.github/workflows/agy-formal-review.yml@'

EDIT_GRAPHQL_QUERY = (
    'query($ids: [ID!]!) { nodes(ids: $ids) { ... on PullRequestReview '
    '{ id lastEditedAt updatedAt submittedAt } } }'
)


class TrustError(RuntimeError):
    """Any unreadable, partial, or malformed API read during verification."""


def caller_allowlist_for(repo: str) -> tuple[str, ...]:
    if repo == 'mctlhq/.github':
        return ('.github/workflows/pr-review.yml',)
    return ('.github/workflows/claude-review.yml',)


def has_blocking_changes_requested(reviews: list[dict], head_sha: str, logins: tuple[str, ...]) -> bool:
    """Return True if ANY CHANGES_REQUESTED review by logins sits on head_sha (edited or unedited)."""
    for r in reviews:
        user = r.get('user')
        if not isinstance(user, dict):
            raise TrustError('malformed review user')
        if user.get('login') not in logins or user.get('type') != 'Bot':
            continue
        if r.get('commit_id') == head_sha and r.get('state') == 'CHANGES_REQUESTED':
            return True
    return False


def unedited_review_ids(graphql_fn: Callable[[dict], dict], node_ids: list[str]) -> set[str]:
    """Verify via GraphQL which review node IDs have never been edited or moved."""
    if not node_ids:
        return set()
    unique_ids = sorted(set(node_ids))
    if any(not isinstance(nid, str) or not nid for nid in unique_ids):
        raise TrustError('invalid review node_id')
    resp = graphql_fn({'query': EDIT_GRAPHQL_QUERY, 'variables': {'ids': unique_ids}})
    if not isinstance(resp, dict) or 'errors' in resp:
        raise TrustError('GraphQL edit-state query returned errors')
    data = resp.get('data')
    nodes = data.get('nodes') if isinstance(data, dict) else None
    if not isinstance(nodes, list):
        raise TrustError('GraphQL edit-state nodes malformed')
    seen_ids = []
    unedited = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise TrustError('GraphQL review node malformed')
        nid = node.get('id')
        last_edited = node.get('lastEditedAt')
        updated_at = node.get('updatedAt')
        submitted_at = node.get('submittedAt')
        if (not isinstance(nid, str) or not nid
                or not (last_edited is None or isinstance(last_edited, str))
                or not isinstance(updated_at, str) or not updated_at
                or not isinstance(submitted_at, str) or not submitted_at):
            raise TrustError('GraphQL review timestamps malformed')
        seen_ids.append(nid)
        if last_edited is None and updated_at == submitted_at:
            unedited.add(nid)
    if sorted(seen_ids) != unique_ids:
        raise TrustError('GraphQL review node set mismatch')
    return unedited


def extract_reviewed_markers(body: str) -> tuple[str, str] | None:
    """Return (head_sha, base_sha) if body contains an unambiguous Reviewed marker."""
    if not isinstance(body, str):
        return None
    matches = REVIEWED_RE.findall(body)
    if len(matches) != 1:
        return None
    return matches[0]


def is_trusted_reusable_ref(entry: object, prefix: str) -> bool:
    """Verify referenced_workflows entry uses @main or exact @<commit-sha> of prefix."""
    if not isinstance(entry, dict):
        return False
    path = entry.get('path')
    sha = entry.get('sha')
    if not isinstance(path, str) or not isinstance(sha, str) or not SHA.fullmatch(sha):
        return False
    if not path.startswith(prefix):
        return False
    ref = path[len(prefix):]
    return ref == 'main' or ref == sha


def verify_run_caller_trust(
    api_get: Callable[[str], dict],
    repo: str,
    run: dict,
    default_branch: str,
    head_sha: str,
    merge_base_sha: str,
    allowlisted_paths: tuple[str, ...] | None = None,
    require_unchanged_blob: bool = True,
) -> tuple[bool, str]:
    """Check run.path, referenced_workflows, and unchanged caller workflow blob vs merge_base / default_branch."""
    if allowlisted_paths is None:
        allowlisted_paths = caller_allowlist_for(repo)
    run_id = run.get('id')
    if type(run_id) is not int or run_id <= 0:
        raise TrustError('workflow run missing valid id')
    path = run.get('path')
    if not isinstance(path, str) or path not in allowlisted_paths:
        return False, f'run.path {path!r} not in allowlist {allowlisted_paths}'
    detail = api_get(f'repos/{repo}/actions/runs/{run_id}')
    if not isinstance(detail, dict):
        raise TrustError('workflow run detail malformed')
    refs = detail.get('referenced_workflows')
    if not isinstance(refs, list):
        raise TrustError('referenced_workflows missing or malformed')
    trusted_ref = any(is_trusted_reusable_ref(r, TRUSTED_CLAUDE_REUSABLE) for r in refs)
    if not trusted_ref:
        return False, 'referenced_workflows lacks trusted claude-review.yml'
    if not require_unchanged_blob:
        return True, 'ok (self-edit handoff)'
    if not (SHA.fullmatch(head_sha) and SHA.fullmatch(merge_base_sha)):
        raise TrustError('invalid SHA for caller blob check')
    head_file = api_get(f'repos/{repo}/contents/{path}?ref={head_sha}')
    mb_file = api_get(f'repos/{repo}/contents/{path}?ref={merge_base_sha}')
    default_file = api_get(f'repos/{repo}/contents/{path}?ref={default_branch}')
    for obj, label in ((head_file, 'head'), (mb_file, 'merge_base'), (default_file, 'default_branch')):
        if not isinstance(obj, dict) or not isinstance(obj.get('sha'), str) or not SHA.fullmatch(obj['sha']):
            raise TrustError(f'caller workflow blob unreadable at {label}')
    if head_file['sha'] != mb_file['sha']:
        return False, f'PR modified {path} relative to merge_base'
    if mb_file['sha'] != default_file['sha']:
        # Allow unchanged PR head if merge_base blob matches any recent commit on default_branch history
        # (verified safely by caller if needed, or exact default_branch check — walk up to 20 commits).
        commits = api_get(f'repos/{repo}/commits?sha={default_branch}&path={path}&per_page=20')
        if not isinstance(commits, list):
            raise TrustError('default_branch commit history malformed for workflow blob check')
        matched_hist = False
        for c in commits:
            csha = c.get('sha') if isinstance(c, dict) else None
            if not isinstance(csha, str) or not SHA.fullmatch(csha):
                continue
            hist_file = api_get(f'repos/{repo}/contents/{path}?ref={csha}')
            if isinstance(hist_file, dict) and hist_file.get('sha') == mb_file['sha']:
                matched_hist = True
                break
        if not matched_hist:
            return False, f'{path} at merge_base does not match any allowlisted {default_branch} revision'
    return True, 'ok'


def _issue_comment_targets_other_head(
    run: dict,
    head_sha: str,
    reviews: list[dict] | None,
    latest_other_pr_ts: str,
) -> bool:
    """Return True when an issue_comment run provably belongs to an earlier/other PR commit."""
    run_created = str(run.get('created_at') or '')
    run_updated = str(run.get('updated_at') or '')
    if latest_other_pr_ts and run_created and run_created <= latest_other_pr_ts:
        return True
    if reviews and run_created:
        for r in reviews:
            u = r.get('user')
            if not isinstance(u, dict) or u.get('login') != CLAUDE_LOGIN or u.get('type') != 'Bot':
                continue
            sub = str(r.get('submitted_at') or '')
            cid = str(r.get('commit_id') or '')
            if cid and cid != head_sha and sub and run_created <= sub and (not run_updated or sub <= run_updated):
                return True
    return False


def find_matching_claude_runs(
    workflow_runs: list[dict],
    pr_number: int,
    head_sha: str,
    jobs_fn: Callable[[int], list[dict]] | None = None,
    reviews: list[dict] | None = None,
) -> list[dict]:
    """Return workflow runs associated with pr_number / head_sha.

    Includes pull_request runs whose head_sha matches head_sha AND issue_comment
    runs referencing pr_number unless provably superseded by a pull_request run
    for another commit or linked via review timestamps to another commit_id.
    """
    other_pr_timestamps = []
    for run in workflow_runs:
        if not isinstance(run, dict):
            raise TrustError('malformed workflow run entry')
        prs = run.get('pull_requests') or []
        if not isinstance(prs, list):
            raise TrustError('workflow run pull_requests malformed')
        pr_nums = {p.get('number') for p in prs if isinstance(p, dict)}
        if run.get('event') == 'pull_request' and (not pr_nums or pr_number in pr_nums) and run.get('head_sha') != head_sha:
            ts = run.get('created_at')
            if isinstance(ts, str) and ts:
                other_pr_timestamps.append(ts)
    latest_other_pr_ts = max(other_pr_timestamps) if other_pr_timestamps else ''

    matched = []
    for run in workflow_runs:
        event = run.get('event')
        run_head = run.get('head_sha')
        prs = run.get('pull_requests') or []
        pr_nums = {p.get('number') for p in prs if isinstance(p, dict)}
        if event == 'pull_request':
            if run_head == head_sha and (not pr_nums or pr_number in pr_nums):
                matched.append(run)
        elif event == 'issue_comment':
            if _issue_comment_targets_other_head(run, head_sha, reviews, latest_other_pr_ts):
                continue
            if pr_number in pr_nums:
                matched.append(run)
            elif jobs_fn is not None and type(run.get('id')) is int:
                title = str(run.get('display_title') or '')
                if re.search(rf'(?<!\d)#{pr_number}(?!\d)', title):
                    jobs = jobs_fn(run['id'])
                    if any(
                        isinstance(j, dict) and (j.get('name') in ('gate', 'review') or str(j.get('name', '')).endswith(('/ gate', '/ review')))
                        for j in jobs
                    ):
                        matched.append(run)
    return matched


def verify_claude_pass(
    reviews: list[dict],
    graphql_fn: Callable[[dict], dict],
    api_get: Callable[[str], dict],
    repo: str,
    pr_number: int,
    default_branch: str,
    head_sha: str,
    live_base_sha: str,
    merge_base_sha: str,
    strict: bool,
    live_diff_digest: str,
    claude_runs: list[dict],
    compute_digest_fn: Callable[[str, str, str], str],
) -> tuple[bool, str, str]:
    """Verify an unedited, trusted claude[bot] APPROVED verdict matching current subject.

    Returns (ok, reason, reviewed_base_sha).
    """
    if has_blocking_changes_requested(reviews, head_sha, (CLAUDE_LOGIN,)):
        return False, 'claude[bot] CHANGES_REQUESTED present on head', ''
    candidates = []
    for r in reviews:
        user = r.get('user')
        if not isinstance(user, dict) or user.get('login') != CLAUDE_LOGIN or user.get('type') != 'Bot':
            continue
        if r.get('commit_id') != head_sha or r.get('state') != 'APPROVED':
            continue
        parsed = extract_reviewed_markers(r.get('body', ''))
        if not parsed:
            continue
        rev_head, rev_base = parsed
        if rev_head != head_sha:
            continue
        nid = r.get('node_id')
        if not isinstance(nid, str) or not nid:
            raise TrustError('APPROVED review missing node_id')
        candidates.append((r, nid, rev_base))
    if not candidates:
        return False, 'no claude[bot] APPROVED verdict with Reviewed head= marker on head', ''
    unedited = unedited_review_ids(graphql_fn, [c[1] for c in candidates])
    valid_base = ''
    for _, nid, rev_base in candidates:
        if nid not in unedited:
            continue
        if rev_base == live_base_sha:
            valid_base = rev_base
            break
        if not strict:
            cmp_obj = api_get(f'repos/{repo}/compare/{rev_base}...{head_sha}?per_page=1')
            mb_obj = cmp_obj.get('merge_base_commit') if isinstance(cmp_obj, dict) else None
            rev_mb = mb_obj.get('sha') if isinstance(mb_obj, dict) else None
            if not isinstance(rev_mb, str) or not SHA.fullmatch(rev_mb):
                raise TrustError('compare merge_base unreadable for reviewed_base')
            if compute_digest_fn(repo, rev_mb, head_sha) == live_diff_digest:
                valid_base = rev_base
                break
    if not valid_base:
        return False, 'no unedited claude[bot] APPROVED verdict matches current diff/base', ''
    # Verify caller workflow trust on completed matching Claude workflow runs.
    completed = [
        run for run in find_matching_claude_runs(claude_runs, pr_number, head_sha)
        if run.get('status') == 'completed'
    ]
    if not completed:
        return False, 'no completed claude-review workflow run matches PR/head', ''
    for run in sorted(completed, key=lambda r: int(r.get('id', 0)), reverse=True):
        ok, _ = verify_run_caller_trust(
            api_get, repo, run, default_branch, head_sha, merge_base_sha, require_unchanged_blob=True
        )
        if ok:
            return True, 'ok', valid_base
    return False, 'no trusted unmodified caller workflow run for claude[bot] PASS', ''
