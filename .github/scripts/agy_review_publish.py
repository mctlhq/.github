"""Trusted formal-review publisher. Never execute this from a PR checkout."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import pr_diff_digest  # noqa: E402
import review_trust  # noqa: E402

CHECK = 'agy-formal-review'
SHADOW_CHECK = 'independent-review-shadow'
ALLOWED_CHECKS = frozenset({'agy-formal-review', 'independent-review'})
ALLOWED_MODES = frozenset({'enforce', 'shadow'})
VERDICTS = {'<!-- VERDICT: PASS -->': 'PASS', '<!-- VERDICT: FAIL:P1 -->': 'FAIL:P1',
            '<!-- VERDICT: FAIL:P2 -->': 'FAIL:P2', '<!-- VERDICT: FAIL:P1,P2 -->': 'FAIL:P1,P2'}
SHA = re.compile(r'[0-9a-f]{40}')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
# A merge queue entry for one pull request (mctlhq/.github#351).
QUEUE_REF = re.compile(r'refs/heads/gh-readonly-queue/([A-Za-z0-9_.-]+)/pr-([1-9][0-9]*)-([0-9a-f]{40})')

CARRY_TITLE = 'Agy formal review: success (carried from PR head)'
EXTERNAL_ID = re.compile(
    r'agy/v([12]):(mctlhq/[A-Za-z0-9_.-]+):([1-9][0-9]*):([0-9]+):([0-9]+)(?::(sha256:[0-9a-f]{64}))?'
)
CONTROLLER_EXT_ID = re.compile(
    r'gate/v1:(mctlhq/[A-Za-z0-9_.-]+):([1-9][0-9]*):([0-9a-f]{40}):(enforce|shadow):(sha256:[0-9a-f]{64})'
)
MAX_CARRY_FILES = 300


class Refused(RuntimeError):
    pass


class CarryMiss(Refused):
    """Queue diff/head lacks a carriable prior verdict; fall back to formal model review."""


def integer(value):
    if isinstance(value, bool) or not re.fullmatch('[1-9][0-9]*', str(value)):
        raise Refused('expected a positive integer')
    return int(value)


def object_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise Refused('duplicate JSON field')
        obj[key] = value
    return obj


def loads(text):
    return json.loads(text, object_pairs_hook=object_pairs)


class API:
    def __init__(self, token):
        if not token:
            raise Refused('missing token')
        self.token = token

    def __call__(self, method, path, payload=None, absent=False):
        # Paths come only from validated repository/number/SHA fields below.
        req = urllib.request.Request(
            'https://api.github.com/' + path,
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={
                'Authorization': 'Bearer ' + self.token,
                'Accept': 'application/vnd.github+json',
                'Content-Type': 'application/json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status == 204:
                    return None
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise Refused('GitHub response exceeds bounded size')
                return loads(raw.decode())
        except urllib.error.HTTPError as exc:
            if absent and exc.code == 404:
                return None
            raise Refused(f'GitHub read/write failed: HTTP {exc.code}') from None
        except (OSError, ValueError) as exc:
            # Never echo response bodies, headers or credentials.
            raise Refused('GitHub response unavailable or malformed') from exc


def pages(api, path, field=None):
    result = []
    for page in range(1, 101):
        sep = '&' if '?' in path else '?'
        obj = api('GET', f'{path}{sep}per_page=100&page={page}')
        values = obj.get(field) if field and isinstance(obj, dict) else obj
        if not isinstance(values, list) or not all(isinstance(x, dict) for x in values):
            raise Refused('unreadable paginated listing')
        result.extend(values)
        if len(values) < 100:
            return result
    raise Refused('listing exceeded bounded pagination')


class Publisher:
    def __init__(self, api, run_api, env, diff_digest_fn=None):
        self.api, self.run_api, self.env = api, run_api, env
        self.diff_digest_fn = diff_digest_fn
        self.source_repo = env['GITHUB_REPOSITORY']
        self.repo = env.get('TARGET_REPOSITORY') or self.source_repo
        if not re.fullmatch(r'mctlhq/[A-Za-z0-9_.-]+', self.source_repo):
            raise Refused('invalid source repository')
        if not re.fullmatch(r'mctlhq/[A-Za-z0-9_.-]+', self.repo):
            raise Refused('repository outside supported owner')
        self.number = integer(env['PR_NUMBER'])
        self.run = integer(env['GITHUB_RUN_ID'])
        self.attempt = integer(env['GITHUB_RUN_ATTEMPT'])
        self.app = integer(env['AGY_REVIEW_APP_ID'])
        self.login = env['AGY_REVIEW_LOGIN']
        if not re.fullmatch(r'[a-z0-9-]+\[bot\]', self.login):
            raise Refused('invalid reviewer App identity')
        self.requested_check = env.get('CHECK_NAME') or CHECK
        if self.requested_check not in ALLOWED_CHECKS:
            raise Refused(f'unsupported check_name: {self.requested_check!r}')
        self.gate_mode = env.get('GATE_MODE') or 'enforce'
        if self.gate_mode not in ALLOWED_MODES:
            raise Refused(f'unsupported gate_mode: {self.gate_mode!r}')
        self.check_name = SHADOW_CHECK if self.gate_mode == 'shadow' else self.requested_check
        expected_head = (env.get('EXPECTED_HEAD_SHA') or '').strip()
        if expected_head and not SHA.fullmatch(expected_head):
            raise Refused('invalid expected head SHA')
        self.expected_head = expected_head
        self.root = 'repos/' + self.repo
        self.pr_path = self.root + f'/pulls/{self.number}'
        self.external_id = f'agy/v2:{self.source_repo}:{self.number}:{self.run}:{self.attempt}'
        self.strict = True
        self.queued = False
        self.queue = None
        queue_ref = env.get('QUEUE_HEAD_REF', '')
        if queue_ref:
            match = QUEUE_REF.fullmatch(queue_ref)
            if not match:
                raise Refused('invalid merge queue ref')
            head, base = env.get('QUEUE_HEAD_SHA', ''), env.get('QUEUE_BASE_SHA', '')
            if not (SHA.fullmatch(head) and SHA.fullmatch(base)):
                raise Refused('invalid merge queue commit')
            if int(match[2]) != self.number:
                raise Refused('merge queue ref names another PR')
            if match[3] != base:
                raise Refused('merge queue ref names another base')
            self.queue = {'ref': queue_ref, 'branch': match[1], 'head': head, 'base': base}

    def _compute_digest(self, repo, merge_base, head_sha):
        if self.diff_digest_fn is not None:
            return self.diff_digest_fn(repo, merge_base, head_sha)
        token = getattr(self.api, 'token', None)
        return pr_diff_digest.compute_remote_diff_digest(repo, merge_base, head_sha, token=token)

    def _graphql(self, query_payload):
        return self.api('POST', 'graphql', query_payload)

    def live_diff_info(self, base_sha, head_sha):
        """Return (merge_base_sha, pr_diff_digest_v1) for base_sha...head_sha."""
        obj = self.api('GET', self.root + f'/compare/{base_sha}...{head_sha}?per_page=1')
        mb = obj.get('merge_base_commit') if isinstance(obj, dict) else None
        mb_sha = mb.get('sha') if isinstance(mb, dict) else None
        if not isinstance(mb_sha, str) or not SHA.fullmatch(mb_sha):
            raise Refused('comparison has no valid merge_base_commit')
        try:
            digest = self._compute_digest(self.repo, mb_sha, head_sha)
        except Refused:
            raise
        except Exception as exc:
            raise Refused(f'could not compute live diff digest: {type(exc).__name__}') from None
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise Refused('live diff digest malformed')
        return mb_sha, digest

    def context(self):
        repo = self.api('GET', self.root)
        branch = repo['default_branch']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', branch):
            raise Refused('unsupported default branch')
        if self.env['GITHUB_EVENT_NAME'] not in ('pull_request_target', 'workflow_dispatch'):
            raise Refused('publisher must run from a trusted default-branch event')
        source = self.run_api('GET', 'repos/' + self.source_repo)
        if self.env['GITHUB_REF'] != 'refs/heads/' + source['default_branch']:
            raise Refused('publisher ref is not the default branch')
        run = self.run_api('GET', 'repos/' + self.source_repo + f'/actions/runs/{self.run}')
        if type(run.get('run_attempt')) is not int or run['event'] != self.env['GITHUB_EVENT_NAME'] or run['run_attempt'] != self.attempt:
            raise Refused('run event or attempt mismatch')
        pr = self.api('GET', self.pr_path)
        if type(pr.get('number')) is not int or pr['number'] != self.number or pr['base']['repo']['full_name'] != self.repo:
            raise Refused('PR repository/number mismatch')
        if pr['head']['repo']['full_name'] != self.repo or pr['base']['ref'] != branch:
            raise Refused('fork or non-default-base PR is not eligible')
        if pr.get('state') not in ('open', 'closed') or type(pr.get('draft')) is not bool:
            raise Refused('PR state/draft response malformed')
        author = pr.get('user')
        if not isinstance(author, dict) or not isinstance(author.get('login'), str) or not author['login']:
            raise Refused('PR author identity missing')
        if author['login'] == self.login:
            raise Refused('author and reviewer cannot share the same login')
        for side in ('base', 'head'):
            if not SHA.fullmatch(pr[side]['sha']):
                raise Refused('invalid PR commit')
        return pr, branch

    def policy(self, branch):
        rules = pages(self.api, self.root + '/rules/branches/' + branch)
        classic = self.api('GET', self.root + '/branches/' + branch + '/protection', absent=True)
        stale = False
        strict = False
        bound = False
        queued = False
        methods = set()
        for rule in rules:
            params = rule.get('parameters', {})
            queued |= rule.get('type') == 'merge_queue'
            if rule.get('type') == 'merge_queue':
                methods.add(params.get('merge_method'))
            if rule.get('type') == 'pull_request':
                stale |= params.get('dismiss_stale_reviews_on_push') is True
            if rule.get('type') == 'required_status_checks':
                strict |= params.get('strict_required_status_checks_policy') is True
                for check in params.get('required_status_checks', []):
                    bound |= check.get('context') == self.requested_check and check.get('integration_id') == self.app
        if classic is not None:
            if not isinstance(classic, dict):
                raise Refused('classic protection response malformed')
            stale |= (classic.get('required_pull_request_reviews') or {}).get('dismiss_stale_reviews') is True
            checks = classic.get('required_status_checks') or {}
            strict |= checks.get('strict') is True
            bound |= any(x.get('context') == self.requested_check and x.get('app_id') == self.app for x in checks.get('checks', []))
        self.strict = strict
        self.queued = queued
        if self.gate_mode == 'enforce':
            if not (stale and (strict or queued) and bound):
                raise Refused(
                    f'formal approval needs stale-review dismissal, strict checks or a merge queue, and the App-bound {self.requested_check} check'
                )
        else:
            if not (stale and (strict or queued)):
                raise Refused('shadow formal review needs stale-review dismissal and strict checks or a merge queue')
        if self.queue and not queued:
            raise Refused('merge queue review needs a merge queue rule on the branch')
        if self.queue and queued and methods != {'MERGE'}:
            raise Refused('merge queue review needs merge_method MERGE')

    def queue_state(self, pr, branch):
        q = self.queue
        if q['branch'] != branch:
            raise Refused('merge queue ref targets another branch')
        if pr['state'] != 'open' or pr['draft']:
            raise Refused('queued PR is not open at the queued head')
        ref = self.api('GET', self.root + '/git/ref/' + q['ref'][len('refs/'):], absent=True)
        if ref is None:
            raise Refused('merge queue entry is gone')
        if (not isinstance(ref, dict) or ref.get('ref') != q['ref']
                or not isinstance(ref.get('object'), dict) or ref['object'].get('sha') != q['head']):
            raise Refused('merge queue ref does not point at the reviewed commit')
        commit = self.api('GET', self.root + f'/commits/{q["head"]}')
        parents = commit.get('parents') if isinstance(commit, dict) else None
        if (not isinstance(parents, list) or not all(isinstance(x, dict) for x in parents)
                or [x.get('sha') for x in parents] != [q['base'], pr['head']['sha']]):
            raise Refused('merge queue commit is not the base plus this PR')

    def _ensure_no_blocking_reviews(self, *head_shas):
        reviews = pages(self.api, self.pr_path + '/reviews')
        for sha in dict.fromkeys(head_shas):
            try:
                blocked = review_trust.has_blocking_changes_requested(
                    reviews, sha, (review_trust.CLAUDE_LOGIN, self.login)
                )
            except review_trust.TrustError as exc:
                raise Refused(str(exc)) from None
            if blocked:
                raise Refused(f'blocking CHANGES_REQUESTED review present on {sha}')
        return reviews

    def compare_files(self, base, head):
        """(merge base, sorted (path, status, previous path, blob) rows) of base...head."""
        rows, merge_base = [], None
        for page in range(1, 11):
            obj = self.api('GET', self.root + f'/compare/{base}...{head}?per_page=100&page={page}')
            if not isinstance(obj, dict) or not isinstance(obj.get('files'), list):
                raise Refused('comparison unreadable')
            mb = obj.get('merge_base_commit')
            if not isinstance(mb, dict) or not SHA.fullmatch(str(mb.get('sha'))):
                raise Refused('comparison has no merge base')
            if merge_base not in (None, mb['sha']):
                raise Refused('comparison merge base moved')
            merge_base = mb['sha']
            for item in obj['files']:
                if not isinstance(item, dict):
                    raise Refused('comparison entry malformed')
                name, status, blob = item.get('filename'), item.get('status'), item.get('sha')
                previous, changes = item.get('previous_filename'), item.get('changes')
                if (not isinstance(name, str) or not name or not isinstance(blob, str) or not SHA.fullmatch(blob)
                        or status not in ('added', 'removed', 'modified', 'renamed')
                        or not (previous is None or isinstance(previous, str))
                        or type(changes) is not int or changes < 1):
                    raise Refused('comparison entry not comparable')
                rows.append((name, status, previous or '', blob))
            if len(obj['files']) < 100:
                if len(rows) >= MAX_CARRY_FILES or len({r[0] for r in rows}) != len(rows):
                    raise Refused('comparison too large or ambiguous')
                return merge_base, sorted(rows)
        raise Refused('comparison exceeded bounded pagination')

    def _qualifying_agy_head_check(self, head, reviews, live_digest):
        found = [
            c for c in pages(self.api, self.root + f'/commits/{head}/check-runs', field='check_runs')
            if c.get('app', {}).get('id') == self.app and c.get('name') in ALLOWED_CHECKS
        ]
        # Prefer check matching requested_check, otherwise any valid formal check for this PR head.
        matching = [c for c in found if c.get('name') == self.check_name] or found
        if len(matching) != 1:
            return None
        check = matching[0]
        match = EXTERNAL_ID.fullmatch(str(check.get('external_id')))
        if (check.get('head_sha') != head or check.get('status') != 'completed'
                or check.get('conclusion') != 'success'
                or not match or match[1] != '2'
                or match[2] != self.source_repo or int(match[3]) != self.number
                or match[6] != live_digest):
            return None
        output = check.get('output')
        if not isinstance(output, dict) or str(output.get('title', '')).startswith(CARRY_TITLE):
            return None
        integer(check.get('id'))
        # Require active, unedited mctl-agy-reviewer[bot] APPROVED review on head.
        candidates = []
        for r in reviews:
            user = r.get('user')
            if not isinstance(user, dict):
                raise Refused('malformed review user')
            if user.get('login') != self.login or user.get('type') != 'Bot':
                continue
            if r.get('commit_id') != head or r.get('state') != 'APPROVED':
                continue
            if f'<!-- agy-formal:{match[4]}:{match[5]}:{head}:PASS -->' not in str(r.get('body', '')):
                continue
            nid = r.get('node_id')
            if not isinstance(nid, str) or not nid:
                raise Refused('formal approval missing node_id')
            candidates.append(nid)
        if not candidates:
            return None
        try:
            unedited = review_trust.unedited_review_ids(self._graphql, candidates)
        except review_trust.TrustError as exc:
            raise Refused(str(exc)) from None
        if not unedited:
            return None
        return check

    def _claude_workflow_runs(self):
        wf_file = review_trust.caller_allowlist_for(self.repo)[0].rsplit('/', 1)[1]
        try:
            runs = pages(self.run_api, self.root + f'/actions/workflows/{wf_file}/runs', field='workflow_runs')
        except Refused:
            # Cross-repo run_api (GITHUB_TOKEN on source_repo) may lack workflow listing on target repo.
            if self.repo != self.source_repo:
                return None
            raise
        return [r for r in runs if str(r.get('path', '')) in review_trust.caller_allowlist_for(self.repo)]

    def carry_proof(self, pr, branch):
        """Return a description dict authorizing carry-forward, or raise CarryMiss / Refused.

        Unreadable API reads or any CHANGES_REQUESTED on live_pr_head raise Refused
        (not CarryMiss), so prepare() fails closed immediately without falling
        through to running agy-review.yml.
        """
        q = self.queue
        if not q:
            raise CarryMiss('not a merge queue review')
        self.queue_state(pr, branch)
        head = pr['head']['sha']
        reviews = self._ensure_no_blocking_reviews(head)
        try:
            _, pr_rows = self.compare_files(q['base'], head)
            queue_base, queue_rows = self.compare_files(q['base'], q['head'])
        except Refused as exc:
            if str(exc) in ('comparison entry not comparable', 'comparison too large or ambiguous'):
                raise CarryMiss(str(exc)) from None
            raise
        if queue_base != q['base']:
            raise CarryMiss('queue commit does not descend from its base')
        if not pr_rows or pr_rows != queue_rows:
            raise CarryMiss('queue commit differs from the reviewed PR diff')
        merge_base, live_digest = self.live_diff_info(q['base'], head)

        source_desc = None
        # Option 1: qualifying trusted unedited claude[bot] APPROVED verdict on live_pr_head.
        try:
            claude_runs = self._claude_workflow_runs()
            if claude_runs is not None:
                def _carry_get(path):
                    return self.run_api('GET', path) if '/actions/' in path else self.api('GET', path)

                claude_ok, _, rev_base = review_trust.verify_claude_pass(
                    reviews=reviews,
                    graphql_fn=self._graphql,
                    api_get=_carry_get,
                    repo=self.repo,
                    pr_number=self.number,
                    default_branch=branch,
                    head_sha=head,
                    live_base_sha=q['base'],
                    merge_base_sha=merge_base,
                    strict=self.strict,
                    live_diff_digest=live_digest,
                    claude_runs=claude_runs,
                    compute_digest_fn=self._compute_digest,
                )
            else:
                claude_ok, rev_base = False, ''
        except Refused:
            if self.repo != self.source_repo:
                claude_ok, rev_base = False, ''
            else:
                raise
        except review_trust.TrustError as exc:
            raise Refused(str(exc)) from None
        if claude_ok:
            source_desc = f'trusted claude[bot] APPROVED verdict (base {rev_base})'
        else:
            # Option 2: non-carried agy/v2 App-owned check + active unedited mctl-agy-reviewer APPROVED review.
            check = self._qualifying_agy_head_check(head, reviews, live_digest)
            if check is not None:
                source_desc = f'complete agy review (check run {check["id"]}: success)'
        if not source_desc:
            raise CarryMiss('PR head has no qualifying unedited formal verdict')

        # Re-read queue entry after comparisons and checks: it must not have moved.
        self.queue_state(self.context()[0], branch)
        return {'description': source_desc, 'diff_digest': live_digest}

    def checks(self, head):
        found = pages(self.api, self.root + f'/commits/{head}/check-runs', field='check_runs')
        return [c for c in found if c.get('name') == self.check_name and c.get('app', {}).get('id') == self.app]

    def owned_check(self, head):
        checks = self.checks(head)
        if not checks:
            raise Refused('formal check missing')
        if len(checks) != 1:
            raise Refused('check belongs to another run; do not overwrite it')
        eid = str(checks[0].get('external_id', ''))
        if eid != self.external_id and not eid.startswith(self.external_id + ':'):
            raise Refused('check belongs to another run; do not overwrite it')
        return checks[0]

    def target(self, pr, branch):
        if self.queue:
            self.queue_state(pr, branch)
            return self.queue['head'], self.queue['base']
        return pr['head']['sha'], pr['base']['sha']

    def _upsert_check(self, old, payload):
        if old:
            body = dict(payload)
            body.pop('head_sha', None)
            self.api('PATCH', self.root + f'/check-runs/{integer(old[0]["id"])}', body)
        else:
            self.api('POST', self.root + '/check-runs', payload)

    def prepare(self):
        pr, branch = self.context()
        if not self.queue and (pr['state'] != 'open' or pr['draft']):
            return None
        if not self.queue and self.expected_head and pr['head']['sha'] != self.expected_head:
            return None
        self.policy(branch)
        head, base = self.target(pr, branch)
        old = self.checks(head)
        if len(old) > 1:
            raise Refused('ambiguous reviewer checks')
        if old:
            eid = str(old[0].get('external_id', ''))
            match = EXTERNAL_ID.fullmatch(eid)
            ctrl_match = CONTROLLER_EXT_ID.fullmatch(eid)
            is_controller_handoff = (
                ctrl_match is not None
                and ctrl_match[1] == self.repo
                and int(ctrl_match[2]) == self.number
                and ctrl_match[3] == head
                and ctrl_match[4] == self.gate_mode
            )
            if not is_controller_handoff:
                if not match or match[2] != self.source_repo or int(match[3]) != self.number or (int(match[4]), int(match[5])) > (self.run, self.attempt):
                    raise Refused('unknown or newer formal review owns this head')

        if self.queue:
            try:
                carried = self.carry_proof(pr, branch)
            except CarryMiss as exc:
                print('queue verdict not carried: ' + str(exc))
                carried = None
            except Refused as exc:
                # Unreadable API or CHANGES_REQUESTED on live_pr_head fails closed immediately.
                self._upsert_check(old, {
                    'name': self.check_name, 'head_sha': head, 'status': 'completed',
                    'conclusion': 'failure', 'external_id': self.external_id,
                    'details_url': f'https://github.com/{self.source_repo}/actions/runs/{self.run}',
                    'output': {'title': 'Formal review blocked or unreadable',
                               'summary': f'Merge queue entry refused without model review: {exc}'},
                })
                raise
            if carried:
                eid = f'{self.external_id}:{carried["diff_digest"]}'
                self._upsert_check(old, {
                    'name': self.check_name, 'head_sha': head, 'status': 'completed',
                    'conclusion': 'success', 'external_id': eid,
                    'details_url': f'https://github.com/{self.source_repo}/actions/runs/{self.run}',
                    'output': {'title': CARRY_TITLE,
                               'summary': f'Merge queue commit {head} (PR #{self.number}) applies exactly the diff of '
                                          f'PR head {pr["head"]["sha"]} onto {base}: same paths and blobs. That head has a '
                                          f'{carried["description"]}. The model was not run again.'},
                })
                print('queue verdict carried from PR head')
                return None

        # Before starting the model on PR head, verify no blocking CHANGES_REQUESTED exists.
        try:
            if self.queue:
                self._ensure_no_blocking_reviews(head, pr['head']['sha'])
            else:
                self._ensure_no_blocking_reviews(head)
        except Refused as exc:
            if 'blocking CHANGES_REQUESTED' in str(exc):
                self._upsert_check(old, {
                    'name': self.check_name, 'head_sha': head, 'status': 'completed',
                    'conclusion': 'failure', 'external_id': self.external_id,
                    'details_url': f'https://github.com/{self.source_repo}/actions/runs/{self.run}',
                    'output': {'title': 'Blocked by existing CHANGES_REQUESTED', 'summary': str(exc)},
                })
            raise

        self._upsert_check(old, {
            'name': self.check_name, 'head_sha': head, 'status': 'in_progress',
            'external_id': self.external_id,
            'details_url': f'https://github.com/{self.source_repo}/actions/runs/{self.run}',
            'output': {'title': 'Agy review running',
                       'summary': 'No approval until a complete current-head verdict is published.'},
        })
        return {'number': self.number, 'head': head, 'base': base}

    def record(self, raw):
        if len(raw.encode()) > 300_000:
            raise Refused('oversized result')
        result = loads(raw)
        required = {'schema_version', 'provider', 'repository', 'source_repository', 'pr_number',
                    'head_sha', 'base_sha', 'diff_digest', 'run_id', 'run_attempt', 'model',
                    'verdict', 'body'}
        if not isinstance(result, dict) or set(result) != required or result['schema_version'] != 'agy-review/v2':
            raise Refused('invalid result schema')
        if result['provider'] != 'agy':
            raise Refused('invalid reviewer provider')
        if not isinstance(result['diff_digest'], str) or not DIGEST.fullmatch(result['diff_digest']):
            raise Refused('invalid diff_digest')
        for key, value in {'repository': self.repo, 'source_repository': self.source_repo, 'pr_number': self.number,
                           'run_id': self.run, 'run_attempt': self.attempt}.items():
            if type(result[key]) is not type(value) or result[key] != value:
                raise Refused('result does not belong to this run/PR')
        for key in ('head_sha', 'base_sha'):
            if not isinstance(result[key], str) or not SHA.fullmatch(result[key]):
                raise Refused('invalid reviewed SHA')
        body = result['body']
        if not isinstance(body, str) or not body.strip() or len(body.encode()) > 48000:
            raise Refused('empty or oversized review')
        last = [line for line in body.splitlines() if line.strip()][-1]
        if last not in VERDICTS or result['verdict'] != VERDICTS[last]:
            raise Refused('invalid verdict')
        if result['verdict'] == 'PASS' and re.search(r'\bP[12]\b', '\n'.join(body.splitlines()[:-1]), re.IGNORECASE):
            raise Refused('PASS contradicts P1/P2 finding')
        if not isinstance(result['model'], str) or not re.fullmatch('[A-Za-z0-9_.-]{1,128}', result['model']):
            raise Refused('invalid reviewer model')
        return result

    def finish_check(self, check, conclusion, summary, external_id=None):
        payload = {'status': 'completed', 'conclusion': conclusion,
                   'output': {'title': 'Agy formal review: ' + conclusion, 'summary': summary}}
        if external_id:
            payload['external_id'] = external_id
        self.api('PATCH', self.root + f'/check-runs/{integer(check["id"])}', payload)

    def publish(self, raw):
        result = self.record(raw)
        pr, branch = self.context()
        self.policy(branch)
        if pr['state'] != 'open' or pr['draft']:
            raise Refused('PR no longer eligible')
        target_head, target_base = self.target(pr, branch)
        if target_head != result['head_sha']:
            raise Refused('head moved; reviewed result is stale')
        if target_base != result['base_sha'] and (self.queue or self.strict):
            raise Refused('head/base moved; reviewed result is stale')
        _, live_digest = self.live_diff_info(target_base, result['head_sha'])
        if live_digest != result['diff_digest']:
            raise Refused('live diff digest does not match reviewed digest')
        check = self.owned_check(result['head_sha'])
        if self.queue:
            return self.publish_queue(result, check, pr['head']['sha'])

        # Blocking CHANGES_REQUESTED on head_sha prevents approval publication.
        reviews = self._ensure_no_blocking_reviews(result['head_sha'])
        event = 'APPROVE' if result['verdict'] == 'PASS' else 'REQUEST_CHANGES'
        eid = f'{self.external_id}:{result["diff_digest"]}'
        if self.gate_mode == 'shadow':
            self.finish_check(
                check,
                'success' if event == 'APPROVE' else 'failure',
                f'Shadow formal review for {result["head_sha"]}: {result["verdict"]} (diff_digest={result["diff_digest"]})',
                external_id=eid,
            )
            return event

        marker = f'<!-- agy-formal:{self.run}:{self.attempt}:{result["head_sha"]}:{result["verdict"]} -->'
        matches = [r for r in reviews if r.get('user', {}).get('login') == self.login
                   and r.get('user', {}).get('type') == 'Bot' and marker in r.get('body', '')
                   and r.get('commit_id') == result['head_sha']
                   and r.get('state') == ('APPROVED' if event == 'APPROVE' else 'CHANGES_REQUESTED')]
        if len(matches) > 1:
            raise Refused('ambiguous duplicate published reviews')
        if matches:
            review = matches[0]
        else:
            review = self.api('POST', self.pr_path + '/reviews', {
                'commit_id': result['head_sha'],
                'event': event,
                'body': marker + '\n\n_Model: ' + result['model'] + '._\n\n' + result['body'],
            })
        integer(review.get('id'))
        if (review.get('user', {}).get('login') != self.login
                or review.get('user', {}).get('type') != 'Bot'
                or review.get('commit_id') != result['head_sha']
                or review.get('state') != ('APPROVED' if event == 'APPROVE' else 'CHANGES_REQUESTED')):
            raise Refused('published review identity/head/state mismatch')

        # Immediately re-read live PR, diff digest, and blocking reviews after posting APPROVE.
        try:
            current, branch = self.context()
            self.policy(branch)
            stale_head = current['head']['sha'] != result['head_sha']
            stale_base = self.strict and current['base']['sha'] != result['base_sha']
            _, post_digest = self.live_diff_info(current['base']['sha'], result['head_sha'])
            digest_moved = post_digest != result['diff_digest']
            if event == 'APPROVE':
                self._ensure_no_blocking_reviews(result['head_sha'])
        except Refused as exc:
            if event == 'APPROVE':
                self.api('PUT', self.pr_path + f'/reviews/{integer(review["id"])}/dismissals',
                         {'message': 'Agy verification failed after posting; dismissing pending fresh check.'})
            raise Refused(str(exc)) from None

        if stale_head or stale_base or digest_moved:
            if event == 'APPROVE':
                self.api('PUT', self.pr_path + f'/reviews/{integer(review["id"])}/dismissals',
                         {'message': 'Agy result became stale during publication; a fresh review is required.'})
            raise Refused('head/base/diff moved during publication')

        self.owned_check(result['head_sha'])
        self.finish_check(
            check,
            'success' if event == 'APPROVE' else 'failure',
            f'Complete review for {result["head_sha"]}: {result["verdict"]} (diff_digest={result["diff_digest"]})',
            external_id=eid,
        )
        return event

    def publish_queue(self, result, check, live_pr_head):
        event = 'APPROVE' if result['verdict'] == 'PASS' else 'REQUEST_CHANGES'
        current, branch = self.context()
        self.queue_state(current, branch)
        self._ensure_no_blocking_reviews(result['head_sha'], current['head']['sha'], live_pr_head)
        self.owned_check(result['head_sha'])
        self.policy(branch)
        eid = f'{self.external_id}:{result["diff_digest"]}'
        self.finish_check(
            check,
            'success' if event == 'APPROVE' else 'failure',
            f'Complete review of merge queue commit {result["head_sha"]} (PR #{self.number}): {result["verdict"]} '
            f'(diff_digest={result["diff_digest"]})',
            external_id=eid,
        )
        return event

    def fail(self, head):
        if not SHA.fullmatch(head):
            raise Refused('no valid reviewed head for failure status')
        check = self.owned_check(head)
        self.finish_check(
            check, 'failure',
            'Formal publication incomplete or failed; merge is not authorized by this check. Inspect reviews before retrying.',
        )


def main():
    env = os.environ
    pub = Publisher(API(env.get('AGY_REVIEW_TOKEN')), API(env.get('GITHUB_TOKEN')), env)
    mode = env['PUBLISH_MODE']
    if mode == 'prepare':
        result = pub.prepare()
        with open(env['GITHUB_OUTPUT'], 'a') as stream:
            stream.write('eligible=' + ('true' if result else 'false') + '\n')
            if result:
                for key, value in result.items():
                    stream.write(f'{key}={value}\n')
    elif mode == 'publish':
        try:
            print(pub.publish(env.get('REVIEW_RECORD', '')))
        except Exception:
            try:
                pub.fail(env.get('REVIEWED_HEAD', ''))
            except Exception as cleanup_error:
                print('Formal check cleanup could not complete (' +
                      type(cleanup_error).__name__ +
                      '); inspect check state. Original publication failure follows.',
                      file=sys.stderr)
            raise
    else:
        raise Refused('unknown mode')


if __name__ == '__main__':
    main()
