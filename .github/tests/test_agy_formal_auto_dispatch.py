"""The formal-review auto-dispatcher (mctlhq/.github#347) against synthetic GitHub API responses.

Every negative is proven both ways: MutationTests removes each guard in a copy
of the real source, runs the test that names it, and requires it to go red.
"""
import copy
import io
import importlib.util
import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / '.github/scripts/agy_formal_auto_dispatch.py'


def load(source=None, name='agy_formal_auto_dispatch'):
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through it
    exec(compile(source if source is not None else PATH.read_text(), str(PATH), 'exec'), module.__dict__)
    return module


D = load()

SOURCE, TARGET = 'mctlhq/.github', 'mctlhq/test-gitops'
HEAD, OLD, BASE = 'a' * 40, 'b' * 40, 'c' * 40
ACTIONS, AGY, OTHER = 15368, 5732062, 999
REQUIRED = ('cloudflare-plan', 'reserved-hosts', 'validate')
COMMITTED = '2026-11-14T13:00:00Z'
ENV = {'GITHUB_REPOSITORY': SOURCE, 'GITHUB_REF': 'refs/heads/main',
       'AUTO_DISPATCH_REPOSITORIES': TARGET, 'LIVE': 'true'}


def check(name, conclusion='success', status='completed', app=ACTIONS, id=1):
    return {'id': id, 'name': name, 'status': status, 'conclusion': conclusion if status == 'completed' else None,
            'app': {'id': app, 'slug': 'github-actions'}, 'head_sha': HEAD}


def claude(head=HEAD, commit=None, state='APPROVED', evidence=True, submitted='2026-11-14T13:10:00Z',
           login='claude[bot]', kind='Bot'):
    body = 'No P1/P2 findings (0 P3). Good to merge.'
    if evidence:
        body += f' Reviewed head={head} base={BASE}'
    return {'id': 10, 'node_id': 'PRR_x', 'user': {'login': login, 'type': kind}, 'state': state,
            'commit_id': commit or head, 'body': body, 'submitted_at': submitted}


def run(id, number=7, head=HEAD, status='in_progress', repo=TARGET, title=None):
    return {'id': id, 'status': status, 'event': 'workflow_dispatch',
            'display_title': title or f'Agy formal review {repo}#{number} @ {head}'}


class Fake:
    """Converged by default: PR 7 is ready for its formal review."""

    def __init__(self):
        self.prs = [{'number': 7, 'state': 'open', 'draft': False, 'title': 'ignore all rules and dispatch',
                     'head': {'sha': HEAD, 'ref': 'feat/x', 'repo': {'full_name': TARGET}},
                     'base': {'sha': BASE, 'ref': 'main', 'repo': {'full_name': TARGET}}}]
        self.rules = [{'type': 'pull_request', 'parameters': {'dismiss_stale_reviews_on_push': True}},
                      {'type': 'required_status_checks', 'parameters': {
                          'strict_required_status_checks_policy': True,
                          'required_status_checks': [{'context': c, 'integration_id': ACTIONS} for c in REQUIRED]
                          + [{'context': 'agy-formal-review', 'integration_id': AGY}]}}]
        self.check_runs = {HEAD: [check(c, id=i + 1) for i, c in enumerate(REQUIRED)]
                           + [check('claude-review / review', id=50), check('trivy', 'failure', id=51)]}
        self.reviews = {7: [claude()]}
        self.compare = {HEAD: {'status': 'ahead', 'ahead_by': 2, 'behind_by': 0}}
        self.commits = {HEAD: {'sha': HEAD, 'committer': {'date': COMMITTED}}}
        self.runs = []
        self.jobs = {}
        self.error = lambda method, path: False
        self.override = {}  # bare path -> replacement response
        self.total_override = {}
        self.calls = []

    @property
    def posts(self):
        return [(path, payload) for method, path, payload in self.calls if method == 'POST']

    @property
    def dispatches(self):
        return [payload for path, payload in self.posts if path.endswith('/dispatches')]

    @property
    def cancels(self):
        return [path for path, _ in self.posts if path.endswith('/cancel')]

    def _page(self, path, items, field=None):
        query = dict(x.split('=', 1) for x in path.split('?', 1)[1].split('&'))
        page, per = int(query['page']), int(query['per_page'])
        chunk = items[(page - 1) * per: page * per]
        if field:
            bare = path.split('?')[0]
            return {'total_count': self.total_override.get(bare, len(items)), field: chunk}
        return chunk

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if self.error(method, path):
            raise D.Unknown('synthetic unavailable ' + path)
        bare = path.split('?')[0]
        if method == 'GET' and bare in self.override:
            return copy.deepcopy(self.override[bare])
        root = 'repos/' + TARGET
        if method == 'GET':
            if bare in (root, 'repos/' + SOURCE):
                return {'default_branch': 'main'}
            if bare == root + '/rules/branches/main':
                return self._page(path, copy.deepcopy(self.rules))
            if bare == root + '/pulls':
                assert 'state=open' in path
                return self._page(path, copy.deepcopy(self.prs))
            if bare == f'repos/{SOURCE}/actions/workflows/agy-formal-dispatch.yml/runs':
                status = re.search(r'status=([a-z_]+)', path)[1]
                return self._page(path, [copy.deepcopy(r) for r in self.runs if r['status'] == status], 'workflow_runs')
            m = re.fullmatch(f'repos/{SOURCE}/actions/runs/([0-9]+)/jobs', bare)
            if m:
                return self._page(path, copy.deepcopy(self.jobs.get(int(m[1]), [])), 'jobs')
            m = re.fullmatch(root + '/commits/([0-9a-f]{40})/check-runs', bare)
            if m:
                return self._page(path, copy.deepcopy(self.check_runs.get(m[1], [])), 'check_runs')
            m = re.fullmatch(root + '/pulls/([0-9]+)/reviews', bare)
            if m:
                return self._page(path, copy.deepcopy(self.reviews.get(int(m[1]), [])))
            m = re.fullmatch(root + '/compare/main\\.\\.\\.([0-9a-f]{40})', bare)
            if m:
                return copy.deepcopy(self.compare[m[1]])
            m = re.fullmatch(root + '/git/commits/([0-9a-f]{40})', bare)
            if m:
                return copy.deepcopy(self.commits[m[1]])
        if method == 'POST' and bare == f'repos/{SOURCE}/actions/workflows/agy-formal-dispatch.yml/dispatches':
            return None
        if method == 'POST' and re.fullmatch(f'repos/{SOURCE}/actions/runs/[0-9]+/cancel', bare):
            return None
        raise AssertionError((method, path))


def go(api, env=None):
    return D.main(dict(ENV, **(env or {})), api, io.StringIO())


class DecisionTests(unittest.TestCase):
    def assertNoDispatch(self, api):
        self.assertEqual(api.dispatches, [])

    # --- the converged positive -------------------------------------------------
    def test_converged_head_dispatches(self):
        api = Fake()
        self.assertEqual(go(api), 0)
        self.assertEqual(api.dispatches, [{'ref': 'main', 'inputs': {
            'repository': TARGET, 'pr_number': '7', 'head_sha': HEAD}}])
        # A non-required red check (trivy) does not hold the dispatch.

    # --- every negative of the issue ---------------------------------------------
    def test_one_required_check_red(self):
        for name in REQUIRED:
            for conclusion in ('failure', 'cancelled', 'timed_out', 'action_required', 'stale', 'startup_failure'):
                with self.subTest(name=name, conclusion=conclusion):
                    api = Fake()
                    api.check_runs[HEAD] = [check(c, conclusion if c == name else 'success', id=i + 1)
                                            for i, c in enumerate(REQUIRED)]
                    self.assertEqual(go(api), 0)
                    self.assertNoDispatch(api)

    def test_one_required_check_pending(self):
        for name in REQUIRED:
            for status in ('queued', 'in_progress', 'waiting', 'requested', 'pending'):
                with self.subTest(name=name, status=status):
                    api = Fake()
                    api.check_runs[HEAD] = [check(c, status=status if c == name else 'completed', id=i + 1)
                                            for i, c in enumerate(REQUIRED)]
                    go(api)
                    self.assertNoDispatch(api)

    def test_required_check_not_yet_reported(self):
        for name in REQUIRED:
            with self.subTest(name=name):
                api = Fake()
                api.check_runs[HEAD] = [c for c in api.check_runs[HEAD] if c['name'] != name]
                go(api)
                self.assertNoDispatch(api)

    def test_lookalike_check_from_another_app_does_not_count(self):
        api = Fake()
        api.check_runs[HEAD] = [check('validate', 'failure', id=3)] + [
            check(c, id=i + 1) for i, c in enumerate(REQUIRED[:2])] + [check('validate', app=OTHER, id=9)]
        go(api)
        self.assertNoDispatch(api)

    def test_latest_rerun_decides(self):
        api = Fake()
        api.check_runs[HEAD].append(check('validate', 'failure', id=99))
        go(api)
        self.assertNoDispatch(api)
        api = Fake()
        api.check_runs[HEAD].insert(0, check('validate', 'failure', id=0))
        go(api)
        self.assertEqual(len(api.dispatches), 1)

    def test_claude_verdict_missing(self):
        for reviews in ([], [claude(state='COMMENTED')], [claude(state='DISMISSED')], [claude(login='mashkovd', kind='User')],
                        [claude(login='claude[bot]', kind='User')]):
            with self.subTest(reviews=reviews):
                api = Fake()
                api.reviews[7] = reviews
                go(api)
                self.assertNoDispatch(api)

    def test_claude_verdict_on_older_sha(self):
        # A legacy verdict submitted after the head commit but on another
        # commit: only the commit_id guard stops it.
        api = Fake()
        api.reviews[7] = [claude(commit=OLD, evidence=False, submitted='2026-11-14T14:00:00Z')]
        go(api)
        self.assertNoDispatch(api)

    def test_verdict_moved_onto_head_but_recording_older_head(self):
        # GitHub moved an approval onto the update-branch head; its body still
        # names the head it reviewed. Submitted late, so only evidence guards.
        api = Fake()
        api.reviews[7] = [claude(head=OLD, commit=HEAD, submitted='2026-11-14T14:00:00Z')]
        go(api)
        self.assertNoDispatch(api)

    def test_legacy_verdict_counts_only_when_given_after_the_head_commit(self):
        api = Fake()
        api.reviews[7] = [claude(evidence=False, submitted='2026-11-14T12:59:59Z')]  # moved by update-branch
        go(api)
        self.assertNoDispatch(api)
        api = Fake()
        api.reviews[7] = [claude(evidence=False, submitted='2026-11-14T13:05:00Z', state='CHANGES_REQUESTED')]
        go(api)
        self.assertEqual(len(api.dispatches), 1)

    def test_evidence_verdict_needs_no_commit_read(self):
        api = Fake()
        go(api)
        self.assertFalse(any('/git/commits/' in path for _, path, _ in api.calls))

    def test_behind(self):
        for compare in ({'status': 'behind', 'ahead_by': 0, 'behind_by': 3},
                        {'status': 'diverged', 'ahead_by': 1, 'behind_by': 1}):
            with self.subTest(compare=compare):
                api = Fake()
                api.compare[HEAD] = compare
                go(api)
                self.assertNoDispatch(api)
        api = Fake()
        api.compare[HEAD] = {'status': 'identical', 'ahead_by': 0, 'behind_by': 0}
        go(api)
        self.assertEqual(len(api.dispatches), 1)

    def test_run_already_queued_or_running_for_head(self):
        for status in ('queued', 'in_progress', 'waiting', 'requested', 'pending'):
            with self.subTest(status=status):
                api = Fake()
                api.runs = [run(500, status=status)]
                go(api)
                self.assertNoDispatch(api)
                self.assertEqual(api.cancels, [])

    def test_formal_check_already_on_head(self):
        for status, conclusion in (('in_progress', None), ('completed', 'failure'), ('completed', 'success')):
            with self.subTest(status=status):
                api = Fake()
                api.check_runs[HEAD].append(check('agy-formal-review', conclusion, status=status, app=AGY, id=70))
                go(api)
                self.assertNoDispatch(api)

    def test_unattributable_active_run_blocks_dispatch_and_cancel(self):
        api = Fake()
        api.runs = [run(500, title='Agy formal review dispatch')]
        go(api)
        self.assertNoDispatch(api)
        self.assertEqual(api.cancels, [])

    def test_manual_run_without_head_blocks_its_pr(self):
        api = Fake()
        api.runs = [run(500, head='any')]
        go(api)
        self.assertNoDispatch(api)
        self.assertEqual(api.cancels, [])

    def test_runner_capacity_cap(self):
        api = Fake()
        api.runs = [run(500, number=8), run(501, number=9, repo='mctlhq/other')]
        go(api)
        self.assertNoDispatch(api)
        api = Fake()
        api.runs = [run(500, number=8)]
        go(api)
        self.assertEqual(len(api.dispatches), 1)

    def test_draft_fork_and_other_base_are_skipped(self):
        for change in ({'draft': True}, {'head': {'sha': HEAD, 'repo': {'full_name': 'outsider/fork'}}},
                       {'head': {'sha': HEAD, 'repo': None}},
                       {'base': {'sha': BASE, 'ref': 'release', 'repo': {'full_name': TARGET}}}):
            with self.subTest(change=change):
                api = Fake()
                api.prs[0].update(change)
                go(api)
                self.assertNoDispatch(api)

    # --- could not observe is never observed absent ------------------------------
    def test_unreadable_responses(self):
        root = 'repos/' + TARGET
        for bare in (root, 'repos/' + SOURCE, root + '/rules/branches/main', root + '/pulls',
                     f'repos/{SOURCE}/actions/workflows/agy-formal-dispatch.yml/runs',
                     f'{root}/commits/{HEAD}/check-runs', root + '/pulls/7/reviews',
                     f'{root}/compare/main...{HEAD}'):
            with self.subTest(path=bare):
                api = Fake()
                api.error = lambda method, path, bare=bare: method == 'GET' and path.split('?')[0] == bare
                go(api)
                self.assertNoDispatch(api)
        api = Fake()
        api.reviews[7] = [claude(evidence=False)]
        api.error = lambda method, path: '/git/commits/' in path
        go(api)
        self.assertNoDispatch(api)

    def test_repository_level_failures_exit_non_zero(self):
        api = Fake()
        api.error = lambda method, path: path.startswith(f'repos/{TARGET}/rules/')
        self.assertEqual(go(api), 1)

    def test_later_page_failure_is_not_a_shorter_list(self):
        # 100 non-verdict reviews, then the page holding the verdict fails.
        api = Fake()
        api.reviews[7] = [claude(state='COMMENTED') for _ in range(100)] + [claude()]
        api.error = lambda method, path: '/reviews' in path and 'page=2' in path
        go(api)
        self.assertNoDispatch(api)
        # A red required check on page 2 of the check runs, page 2 unreadable.
        api = Fake()
        api.check_runs[HEAD] = [check('noise', id=200 + i) for i in range(97)] + api.check_runs[HEAD] + [
            check('validate', 'failure', id=999)]
        api.error = lambda method, path: '/check-runs' in path and 'page=2' in path
        go(api)
        self.assertNoDispatch(api)

    def test_partial_listing_against_total_count(self):
        # Page one holds three green checks, but GitHub says there are more.
        api = Fake()
        api.total_override[f'repos/{TARGET}/commits/{HEAD}/check-runs'] = 4
        go(api)
        self.assertNoDispatch(api)
        api = Fake()
        api.runs = [run(500, number=8)]
        api.total_override[f'repos/{SOURCE}/actions/workflows/agy-formal-dispatch.yml/runs'] = 2
        go(api)
        self.assertNoDispatch(api)

    def test_malformed_responses(self):
        root = 'repos/' + TARGET
        cases = [
            (f'{root}/commits/{HEAD}/check-runs', {'total_count': 3, 'check_runs': 'nope'}),
            (f'{root}/commits/{HEAD}/check-runs', {'check_runs': []}),
            (f'{root}/compare/main...{HEAD}', {'status': 'ahead'}),
            (f'{root}/compare/main...{HEAD}', {'status': 'ahead', 'behind_by': '0'}),
            (f'{root}/compare/main...{HEAD}', {'status': 'diverged', 'behind_by': 0}),
            (f'{root}/compare/main...{HEAD}', {'status': 'weird', 'behind_by': 0}),
            (f'{root}/compare/main...{HEAD}', []),
            (root + '/pulls/7/reviews', {'reviews': []}),
            (root + '/rules/branches/main', {'rules': []}),
            (root, []),
        ]
        for bare, value in cases:
            with self.subTest(path=bare, value=value):
                api = Fake()
                api.override[bare] = value
                go(api)
                self.assertNoDispatch(api)
        for mutate in (lambda a: a.check_runs[HEAD][0].update(status=None),
                       lambda a: a.check_runs[HEAD][0].update(app=None),
                       lambda a: a.check_runs[HEAD][0].update(id='1'),
                       lambda a: a.reviews[7][0].update(body=None),
                       lambda a: a.prs[0]['head'].update(sha='not-a-sha'),
                       lambda a: a.prs[0].update(draft='no')):
            api = Fake(); mutate(api)
            go(api)
            self.assertNoDispatch(api)
        api = Fake()
        api.reviews[7] = [claude(evidence=False, submitted='yesterday')]
        go(api)
        self.assertNoDispatch(api)

    def test_branch_policy_must_bind_every_check(self):
        api = Fake()
        api.rules[1]['parameters']['required_status_checks'][1].pop('integration_id')
        self.assertEqual(go(api), 1)
        self.assertNoDispatch(api)
        api = Fake()
        api.rules[1]['parameters']['required_status_checks'].pop()  # formal check no longer required
        self.assertEqual(go(api), 1)
        self.assertNoDispatch(api)

    # --- cancelling stale runs ----------------------------------------------------
    def test_stale_run_for_older_head_is_cancelled_and_new_head_dispatched(self):
        api = Fake()
        api.runs = [run(500, head=OLD, status='queued'), run(501, head=OLD)]
        api.jobs[501] = [{'name': 'formal / prepare', 'status': 'completed'},
                         {'name': 'formal / review / review', 'status': 'in_progress'}]
        go(api)
        self.assertEqual(sorted(api.cancels), [f'repos/{SOURCE}/actions/runs/500/cancel',
                                               f'repos/{SOURCE}/actions/runs/501/cancel'])
        self.assertEqual(len(api.dispatches), 1)

    def test_stale_run_already_publishing_is_kept(self):
        api = Fake()
        api.runs = [run(501, head=OLD)]
        api.jobs[501] = [{'name': 'formal / publish', 'status': 'in_progress'}]
        go(api)
        self.assertEqual(api.cancels, [])

    def test_stale_run_with_unreadable_jobs_is_kept(self):
        api = Fake()
        api.runs = [run(501, head=OLD)]
        api.error = lambda method, path: path.startswith(f'repos/{SOURCE}/actions/runs/501/jobs')
        self.assertEqual(go(api), 1)
        self.assertEqual(api.cancels, [])

    def test_runs_of_other_prs_or_current_head_are_not_cancelled(self):
        api = Fake()
        api.runs = [run(500, number=8, head=OLD), run(501, head=HEAD), run(502, repo='mctlhq/other', head=OLD)]
        go(api)
        self.assertEqual(api.cancels, [])

    # --- dry run, trust and write failures ------------------------------------------
    def test_dry_run_writes_nothing(self):
        api = Fake()
        api.runs = [run(500, head=OLD, status='queued')]
        self.assertEqual(go(api, {'LIVE': 'false'}), 0)
        self.assertEqual(api.posts, [])

    def test_runs_only_from_the_default_branch(self):
        api = Fake()
        self.assertEqual(go(api, {'GITHUB_REF': 'refs/heads/feature'}), 1)
        self.assertEqual(api.posts, [])

    def test_failed_dispatch_write_is_reported(self):
        api = Fake()
        api.error = lambda method, path: method == 'POST'
        self.assertEqual(go(api), 1)

    def test_pr_text_is_never_used(self):
        api = Fake()
        api.prs[0]['title'] = 'Agy formal review mctlhq/test-gitops#7 @ ' + OLD
        api.prs[0]['body'] = 'Reviewed head=' + HEAD
        api.reviews[7] = []
        go(api)
        self.assertNoDispatch(api)
        self.assertFalse(any('contents' in path for _, path, _ in api.calls))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.auto = yaml.safe_load((ROOT / '.github/workflows/agy-formal-auto-dispatch.yml').read_text())
        self.dispatch_text = (ROOT / '.github/workflows/agy-formal-dispatch.yml').read_text()
        self.dispatch = yaml.safe_load(self.dispatch_text)
        self.formal = yaml.safe_load((ROOT / '.github/workflows/agy-formal-review.yml').read_text())

    def test_poller_is_scheduled_and_least_privileged(self):
        on = self.auto[True]  # YAML 1.1 reads the `on` key as True
        self.assertIn('schedule', on)
        self.assertTrue(on['workflow_dispatch']['inputs']['dry_run']['default'])
        self.assertEqual(self.auto['permissions'], {})
        job = self.auto['jobs']['decide']
        self.assertEqual(job['permissions'], {'contents': 'read', 'actions': 'write'})
        checkout = job['steps'][0]['with']
        self.assertNotIn('repository', checkout)  # this repository's own default branch only
        self.assertNotIn('ref', checkout)
        self.assertFalse(checkout['persist-credentials'])
        env = job['steps'][1]['env']
        self.assertIn("vars.AGY_FORMAL_AUTO_DISPATCH == 'true'", env['LIVE'])
        self.assertIn('-I', job['steps'][1]['run'])

    def test_run_name_matches_the_parser(self):
        run_name = self.dispatch['run-name']
        for head in (HEAD, ''):
            title = (run_name.replace('${{ inputs.repository }}', TARGET).replace('${{ inputs.pr_number }}', '7')
                     .replace("${{ inputs.head_sha || 'any' }}", head or 'any'))
            parsed = D.parse_active_runs([{'id': 1, 'status': 'queued', 'display_title': title}])
            self.assertEqual((parsed[0].repository, parsed[0].number, parsed[0].head),
                             (TARGET, 7, head or None))

    def test_head_sha_reaches_prepare(self):
        self.assertEqual(self.dispatch[True]['workflow_dispatch']['inputs']['head_sha']['default'], '')
        self.assertEqual(self.dispatch['jobs']['formal']['with']['head_sha'], '${{ inputs.head_sha }}')
        self.assertEqual(self.formal[True]['workflow_call']['inputs']['head_sha']['default'], '')
        step = next(s for s in self.formal['jobs']['prepare']['steps'] if s.get('id') == 'prepare')
        self.assertEqual(step['env']['EXPECTED_HEAD_SHA'], '${{ inputs.head_sha }}')


class MutationTests(unittest.TestCase):
    """Remove each guard from a copy of the real source; its test must go red."""
    MUTATIONS = [
        ("        return 'pending'\n", "        return 'pass'\n", 'test_one_required_check_pending'),
        ("    return 'pass' if conclusion in PASSING else 'fail'", "    return 'pass'", 'test_one_required_check_red'),
        ("        return 'missing'\n", "        return 'pass'\n", 'test_required_check_not_yet_reported'),
        ("        if app.get('id') != app_id:\n            continue\n", "", 'test_lookalike_check_from_another_app_does_not_count'),
        ("    latest = max(matching, key=lambda r: r['id'])", "    latest = min(matching, key=lambda r: r['id'])", 'test_latest_rerun_decides'),
        ("        if not has_claude_verdict(", "        if False and not has_claude_verdict(", 'test_claude_verdict_missing'),
        ("        if review.get('commit_id') != head or review.get('state')", "        if review.get('state')", 'test_claude_verdict_on_older_sha'),
        ("        if 'Reviewed head=' not in body:\n", "        if True:\n", 'test_verdict_moved_onto_head_but_recording_older_head'),
        ("    return any(submitted >= committed for submitted in legacy)", "    return True", 'test_legacy_verdict_counts_only_when_given_after_the_head_commit'),
        ("        if is_behind(_load(compare)):", "        if False:", 'test_behind'),
        ("        if any(r.head == pr.head for r in mine):", "        if False:", 'test_run_already_queued_or_running_for_head'),
        ("        if has_formal_check(check_runs, policy.formal_app_id):", "        if False:", 'test_formal_check_already_on_head'),
        ("        if any(r.repository is None for r in active):", "        if False:", 'test_unattributable_active_run_blocks_dispatch_and_cancel'),
        ("        if any(r.head is None for r in mine):", "        if False:", 'test_manual_run_without_head_blocks_its_pr'),
        ("        if len(active) >= MAX_ACTIVE_FORMAL_RUNS:", "        if False:", 'test_runner_capacity_cap'),
        ("        return Decision('unknown', str(exc))\n    return Decision('dispatch'", "        return Decision('dispatch', str(exc))\n    return Decision('dispatch'", 'test_unreadable_responses'),
        ("        return Decision('unknown', str(exc))\n    return Decision('dispatch'", "        return Decision('dispatch', str(exc))\n    return Decision('dispatch'", 'test_later_page_failure_is_not_a_shorter_list'),
        ("        return Decision('unknown', str(exc))\n    return Decision('dispatch'", "        return Decision('dispatch', str(exc))\n    return Decision('dispatch'", 'test_malformed_responses'),
        ("            if total is not None and len(result) != total:", "            if False:", 'test_partial_listing_against_total_count'),
        ("                raise Unknown(f'required check {context!r} is not bound to an integration')", "                continue", 'test_branch_policy_must_bind_every_check'),
        ("    if draft:\n        return PullRequest(number, sha, False, 'draft')\n", "", 'test_draft_fork_and_other_base_are_skipped'),
        ("            if publish_started(jobs):", "            if False:", 'test_stale_run_already_publishing_is_kept'),
        ("            and r.head != open_heads[r.number]]", "]", 'test_runs_of_other_prs_or_current_head_are_not_cancelled'),
        ("        if decision.action == 'dispatch' and live:", "        if decision.action == 'dispatch':", 'test_dry_run_writes_nothing'),
        ("        if env.get('GITHUB_REF') != 'refs/heads/' + source_branch:", "        if False:", 'test_runs_only_from_the_default_branch'),
    ]

    def test_every_guard_is_load_bearing(self):
        global D
        source = PATH.read_text()
        original = D
        for old, new, test in self.MUTATIONS:
            with self.subTest(test=test, guard=old.strip()[:70]):
                self.assertEqual(source.count(old), 1, 'mutation anchor drifted')
                try:
                    D = load(source.replace(old, new), 'agy_formal_auto_dispatch_mutant')
                    red = unittest.TestResult()
                    DecisionTests(test).run(red)
                    self.assertFalse(red.wasSuccessful(), 'the guard was removed and its test still passed')
                finally:
                    D = original
                    sys.modules['agy_formal_auto_dispatch'] = original
                green = unittest.TestResult()
                DecisionTests(test).run(green)
                self.assertTrue(green.wasSuccessful(), str(green.errors + green.failures))


if __name__ == '__main__':
    unittest.main()
