"""Exercise the actual publisher and inline result exporter with synthetic API responses."""
import copy
import contextlib
import importlib.util
import io
import json
import os
import tempfile
from unittest.mock import patch
from pathlib import Path
import re
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / '.github/scripts/agy_review_publish.py'
SPEC = importlib.util.spec_from_file_location('agy_publisher', PATH)
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)
WF = yaml.safe_load((ROOT / '.github/workflows/agy-review.yml').read_text())
EXPORT = next(x['run'] for x in WF['jobs']['review']['steps'] if x.get('id') == 'formal_record')
PACK = EXPORT.split("python3 - <<'PY_RECORD'\n", 1)[1].rsplit('\nPY_RECORD', 1)[0]
NS = {'__name__': 'record_tests'}
exec(compile(PACK, '<actual workflow record exporter>', 'exec'), NS)
HEAD, BASE = 'a' * 40, 'b' * 40
REPO = 'mctlhq/test-repo'
ENV = {'GITHUB_REPOSITORY': REPO, 'PR_NUMBER': '7', 'GITHUB_RUN_ID': '100',
       'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_NAME': 'pull_request_target',
       'GITHUB_REF': 'refs/heads/main', 'REVIEW_REPOSITORY': REPO, 'AGY_REVIEW_APP_ID': '123',
       'AGY_REVIEW_LOGIN': 'agy-test[bot]', 'HEAD_SHA': HEAD, 'BASE_SHA': BASE,
       'AGY_MODEL': 'gemini-3.8-flash-high'}
PASS = 'No significant issues found.\n\n<!-- VERDICT: PASS -->\n'


class Fake:
    def __init__(self):
        self.source_repo = REPO
        self.pr = {'number': 7, 'state': 'open', 'draft': False,
                   'head': {'sha': HEAD, 'repo': {'full_name': REPO}},
                   'base': {'sha': BASE, 'ref': 'main', 'repo': {'full_name': REPO}}}
        self.rules = [{'type': 'pull_request', 'parameters': {'dismiss_stale_reviews_on_push': True}},
                      {'type': 'required_status_checks', 'parameters': {
                          'strict_required_status_checks_policy': True,
                          'required_status_checks': [{'context': P.CHECK, 'integration_id': 123}]}}]
        self.checks = []
        self.reviews = []
        self.calls = []
        self.attempt = 1
        self.error = None
        self.move_on_post = False
        self.bad_review = False
        self.queue_refs = {}
        self.commits = {}
        self.move_on_queue_read = None
        self.other_checks = []
        self.compare = {}

    def __call__(self, method, path, payload=None, absent=False):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if self.error and self.error(method, path):
            raise P.Refused('synthetic unavailable read')
        root = 'repos/' + REPO
        bare = path.split('?')[0]
        if method == 'GET':
            if bare in (root, 'repos/' + self.source_repo):
                value = {'default_branch': 'main'}
            elif bare == 'repos/' + self.source_repo + '/actions/runs/100':
                value = {'event': 'pull_request_target', 'run_attempt': self.attempt}
            elif bare == root + '/pulls/7':
                value = self.pr
            elif bare == root + '/rules/branches/main':
                value = self.rules
            elif bare == root + '/branches/main/protection':
                value = None
            elif bare.endswith('/check-runs'):
                sha = bare.split('/')[-2]
                own = [c for c in self.other_checks if c['head_sha'] == sha]
                value = {'check_runs': own if own else self.checks}
            elif '/compare/' in bare:
                a, b = bare.rsplit('/compare/', 1)[1].split('...')
                if (a, b) not in self.compare:
                    raise P.Refused('synthetic unconfigured comparison')
                body = copy.deepcopy(self.compare[(a, b)])
                page = int(re.search(r'[?&]page=(\d+)', path).group(1))
                value = dict(body, files=body['files'][(page - 1) * 100:page * 100])
            elif bare.startswith(root + '/git/ref/'):
                ref = 'refs/' + bare[len(root + '/git/ref/'):]
                if ref not in self.queue_refs:
                    if absent:
                        return None
                    raise P.Refused('synthetic 404')
                value = {'ref': ref, 'object': {'sha': self.queue_refs[ref]}}
                if self.move_on_queue_read:
                    self.move_on_queue_read(self)
            elif bare.startswith(root + '/commits/'):
                value = {'parents': [{'sha': x} for x in self.commits[bare.rsplit('/', 1)[1]]]}
            elif bare.endswith('/reviews'):
                value = self.reviews
            else:
                raise AssertionError((method, path))
            return copy.deepcopy(value)
        if method == 'POST' and bare == root + '/check-runs':
            check = dict(payload, id=1, app={'id': 123})
            self.checks.append(check)
            return copy.deepcopy(check)
        if method == 'PATCH' and bare == root + '/check-runs/1':
            self.checks[0].update(payload)
            return copy.deepcopy(self.checks[0])
        if method == 'POST' and bare.endswith('/reviews'):
            review = {'id': len(self.reviews) + 1, 'body': payload['body'],
                      'commit_id': payload['commit_id'],
                      'user': {'login': 'agy-test[bot]', 'type': 'Bot'},
                      'state': 'APPROVED' if payload['event'] == 'APPROVE' else 'CHANGES_REQUESTED'}
            if self.bad_review:
                review['state'] = 'PENDING'
            self.reviews.append(review)
            if self.move_on_post:
                self.pr['head']['sha'] = 'c' * 40
            return copy.deepcopy(review)
        if method == 'PUT' and bare.endswith('/dismissals'):
            self.reviews[-1]['state'] = 'DISMISSED'
            return copy.deepcopy(self.reviews[-1])
        raise AssertionError((method, path))


class PublisherTests(unittest.TestCase):
    def setup_pub(self, env=None):
        api = Fake()
        pub = P.Publisher(api, api, env or ENV)
        pub.prepare()
        return api, pub

    def raw(self, review_text=PASS, **changes):
        record = NS['make_record'](review_text, ENV)
        record.update(changes)
        return json.dumps(record)

    def test_complete_current_head_pass_approves_once(self):
        api, pub = self.setup_pub()
        self.assertEqual(pub.publish(self.raw()), 'APPROVE')
        self.assertEqual(pub.publish(self.raw()), 'APPROVE')
        self.assertEqual(len(api.reviews), 1)
        self.assertEqual(api.reviews[0]['commit_id'], HEAD)
        self.assertEqual(api.checks[0]['conclusion'], 'success')

    def test_lost_write_response_is_unknown_and_cannot_authorize_merge(self):
        class LostResponse(Fake):
            def __call__(self, method, path, payload=None, absent=False):
                result = super().__call__(method, path, payload, absent)
                if method == 'POST' and path.endswith('/reviews'):
                    raise P.Refused('synthetic lost response after provider accepted review')
                return result

        api = LostResponse()
        pub = P.Publisher(api, api, ENV)
        pub.prepare()
        with self.assertRaises(P.Refused):
            pub.publish(self.raw())
        pub.fail(HEAD)
        self.assertEqual(api.reviews[0]['state'], 'APPROVED')
        self.assertEqual(api.checks[0]['conclusion'], 'failure')
        summary = api.checks[0]['output']['summary']
        self.assertIn('Inspect reviews', summary)
        self.assertNotIn('no approval published', summary)

    def test_central_dispatch_reviews_target_without_borrowing_its_run_identity(self):
        api = Fake(); api.source_repo = 'mctlhq/.github'
        env = dict(ENV, GITHUB_REPOSITORY=api.source_repo, TARGET_REPOSITORY=REPO)
        pub = P.Publisher(api, api, env); pub.prepare()
        record = NS['make_record'](PASS, env)
        self.assertEqual(pub.publish(json.dumps(record)), 'APPROVE')
        self.assertEqual(api.reviews[0]['commit_id'], HEAD)
        self.assertIn('/mctlhq/.github/actions/runs/', api.checks[0]['details_url'])

    def test_findings_request_changes_and_fail_the_app_check(self):
        for verdict in ('FAIL:P1', 'FAIL:P2', 'FAIL:P1,P2'):
            with self.subTest(verdict=verdict):
                api, pub = self.setup_pub()
                self.assertEqual(pub.publish(self.raw('Finding.\n<!-- VERDICT: ' + verdict + ' -->')), 'REQUEST_CHANGES')
                self.assertEqual(api.checks[0]['conclusion'], 'failure')
                self.assertEqual(api.reviews[0]['state'], 'CHANGES_REQUESTED')

    def test_no_result_malformed_or_wrong_scope_never_approves(self):
        values = ['', '{}', '{', self.raw(repository='other/repo'), self.raw(pr_number=8),
                  self.raw(run_id=101), self.raw(run_attempt=2), self.raw(run_id=True),
                  self.raw(schema_version='other'), self.raw(verdict='UNKNOWN'),
                  self.raw(head_sha='x'), self.raw(model='bad\nmodel'),
                  self.raw(body='[P2] bug\n<!-- VERDICT: PASS -->'),
                  self.raw(body='### Finding 1 (P1)\nBug.\n<!-- VERDICT: PASS -->'),
                  self.raw(body='Severity: P2\nBug.\n<!-- VERDICT: PASS -->'),
                  self.raw(body='severity p1\nBug.\n<!-- VERDICT: PASS -->'),
                  self.raw(body='<!-- VERDICT: PASS -->\nextra'),
                  self.raw(body='x' * 48001 + '\n<!-- VERDICT: PASS -->')]
        for raw in values:
            with self.subTest(raw=raw[:100]):
                api, pub = self.setup_pub()
                with self.assertRaises((P.Refused, ValueError)):
                    pub.publish(raw)
                self.assertEqual(api.reviews, [])
                self.assertNotEqual(api.checks[0].get('conclusion'), 'success')

    def test_move_head_or_base_before_publication_refuses(self):
        for side in ('head', 'base'):
            api, pub = self.setup_pub()
            api.pr[side]['sha'] = 'c' * 40
            with self.assertRaises(P.Refused):
                pub.publish(self.raw())
            self.assertEqual(api.reviews, [])

    def test_move_during_post_dismisses_and_never_marks_new_head_green(self):
        api, pub = self.setup_pub()
        api.move_on_post = True
        with self.assertRaises(P.Refused):
            pub.publish(self.raw())
        self.assertEqual(api.reviews[-1]['state'], 'DISMISSED')
        self.assertEqual(api.checks[0]['head_sha'], HEAD)
        self.assertNotEqual(api.checks[0].get('conclusion'), 'success')

    def test_every_protection_is_required(self):
        for mutate in (lambda a: a.rules[0]['parameters'].clear(),
                       lambda a: a.rules[1]['parameters'].update(strict_required_status_checks_policy=False),
                       lambda a: a.rules[1]['parameters']['required_status_checks'][0].update(integration_id=15368)):
            api = Fake(); mutate(api)
            pub = P.Publisher(api, api, ENV)
            with self.assertRaises(P.Refused):
                pub.prepare()
            self.assertEqual(api.checks, [])
            self.assertEqual(api.reviews, [])

    def test_untrusted_event_ref_fork_and_target_refuse(self):
        for changes in ({'GITHUB_EVENT_NAME': 'pull_request'}, {'GITHUB_REF': 'refs/pull/7/merge'},
                        {'GITHUB_REPOSITORY': 'outsider/repo'}):
            api = Fake()
            with self.assertRaises(P.Refused):
                P.Publisher(api, api, dict(ENV, **changes)).prepare()
        for path, value in (('head_repo', 'outsider/repo'), ('base_ref', 'other')):
            api = Fake()
            if path == 'head_repo':
                api.pr['head']['repo']['full_name'] = value
            else:
                api.pr['base']['ref'] = value
            with self.assertRaises(P.Refused):
                P.Publisher(api, api, ENV).prepare()

    def test_closed_or_draft_is_not_reviewed(self):
        for mutate in (lambda a: a.pr.update(draft=True), lambda a: a.pr.update(state='closed')):
            api = Fake(); mutate(api)
            self.assertIsNone(P.Publisher(api, api, ENV).prepare())
            self.assertEqual(api.reviews, [])

    def test_unavailable_reads_do_not_create_an_approval(self):
        for suffix in ('/pulls/7', '/rules/branches/main', '/protection', '/reviews?per_page=100&page=1'):
            api, pub = self.setup_pub()
            api.error = lambda method, path: method == 'GET' and (path.endswith(suffix) if '?' in suffix else path.split('?')[0].endswith(suffix))
            with self.assertRaises(P.Refused):
                pub.publish(self.raw())
            self.assertEqual(api.reviews, [])

    def test_later_page_failure_is_not_partial_success(self):
        api, pub = self.setup_pub()
        api.reviews = [{'id': i} for i in range(100)]
        api.error = lambda method, path: method == 'GET' and '/reviews?' in path and 'page=2' in path
        with self.assertRaises(P.Refused):
            pub.publish(self.raw())
        self.assertFalse(any(method == 'POST' and path.endswith('/reviews') for method, path, _ in api.calls))

    def test_unknown_check_or_newer_run_cannot_be_overwritten(self):
        api, pub = self.setup_pub()
        api.checks[0]['external_id'] = 'agy/v1:mctlhq/test-repo:7:101:1'
        with self.assertRaises(P.Refused):
            pub.publish(self.raw())
        with self.assertRaises(P.Refused):
            pub.prepare()
        self.assertEqual(api.reviews, [])

    def test_changed_run_attempt_never_publishes(self):
        api, pub = self.setup_pub()
        api.attempt = 2
        with self.assertRaises(P.Refused):
            pub.publish(self.raw())
        self.assertEqual(api.reviews, [])

    def test_missing_model_result_fails_the_owned_check(self):
        api, pub = self.setup_pub()
        pub.fail(HEAD)
        self.assertEqual(api.checks[0]['conclusion'], 'failure')
        self.assertEqual(api.reviews, [])

    def test_review_api_must_return_the_expected_state(self):
        api, pub = self.setup_pub(); api.bad_review = True
        with self.assertRaises(P.Refused):
            pub.publish(self.raw())
        self.assertNotEqual(api.checks[0].get('conclusion'), 'success')

    def test_duplicate_json_rejected(self):
        with self.assertRaises(P.Refused):
            P.loads('{"x":1,"x":2}')

    def test_cli_cleanup_failure_keeps_original_error_and_newer_check(self):
        for head in ('', HEAD):
            with self.subTest(head=head):
                api, pub = self.setup_pub()
                api.checks[0]['external_id'] = 'agy/v1:mctlhq/test-repo:7:101:1'
                original_check = copy.deepcopy(api.checks)
                env = dict(ENV, PUBLISH_MODE='publish', REVIEW_RECORD='{',
                           REVIEWED_HEAD=head, AGY_REVIEW_TOKEN='fixture', GITHUB_TOKEN='fixture')
                diagnostic = io.StringIO()
                with patch.dict(os.environ, env, clear=True), patch.object(P, 'API', return_value=api):
                    with contextlib.redirect_stderr(diagnostic), self.assertRaises(json.JSONDecodeError):
                        P.main()
                self.assertIn('cleanup could not complete', diagnostic.getvalue())
                self.assertEqual(api.checks, original_check)
                self.assertEqual(api.reviews, [])


QHEAD = 'd' * 40
# The ref names the entry's base commit, not the PR head (see gitops#2665).
QREF = 'refs/heads/gh-readonly-queue/main/pr-7-' + BASE
QENV = dict(ENV, QUEUE_HEAD_REF=QREF, QUEUE_HEAD_SHA=QHEAD, QUEUE_BASE_SHA=BASE)


def queue_fake():
    api = Fake()
    api.rules[1]['parameters']['strict_required_status_checks_policy'] = False
    api.rules.append({'type': 'merge_queue', 'parameters': {'merge_method': 'MERGE'}})
    api.queue_refs[QREF] = QHEAD
    api.commits[QHEAD] = [BASE, HEAD]
    return api


PRHEAD_CHECK = {'id': 50, 'name': 'agy-formal-review', 'app': {'id': 123}, 'head_sha': HEAD,
                'status': 'completed', 'conclusion': 'success',
                'external_id': 'agy/v1:' + REPO + ':7:50:1',
                'output': {'title': 'Agy formal review: success', 'summary': 'Complete review'}}


def file_row(name, blob, status='modified', changes=3, **extra):
    return dict({'filename': name, 'sha': blob, 'status': status, 'changes': changes}, **extra)


def carry_fake():
    """A queue entry whose commit is exactly the reviewed PR head's diff on the base."""
    api = queue_fake()
    api.other_checks = [copy.deepcopy(PRHEAD_CHECK)]
    rows = [file_row('values.yaml', '1' * 40), file_row('new.yaml', '2' * 40, 'added', 9)]
    # The queue commit is on a newer base than the PR was branched from, but
    # carries the same changes; both comparisons are against the queue base.
    api.compare[(BASE, HEAD)] = {'merge_base_commit': {'sha': 'f' * 40}, 'files': copy.deepcopy(rows)}
    api.compare[(BASE, QHEAD)] = {'merge_base_commit': {'sha': BASE}, 'files': copy.deepcopy(rows)}
    return api


class CarryTests(unittest.TestCase):
    """The queue commit is marked from the PR-head verdict only on proof it adds nothing."""

    def run_prepare(self, api, env=None):
        pub = P.Publisher(api, api, env or QENV)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            prepared = pub.prepare()
        return prepared, out.getvalue()

    def assert_model_runs(self, api, prepared):
        # Fallback is the pre-existing behaviour: in_progress check, model job.
        self.assertEqual(prepared, {'number': 7, 'head': QHEAD, 'base': BASE})
        self.assertEqual(api.checks[0]['status'], 'in_progress')
        self.assertNotEqual(api.checks[0].get('conclusion'), 'success')

    def test_identical_diff_on_a_reviewed_head_is_carried_without_the_model(self):
        api = carry_fake()
        prepared, out = self.run_prepare(api)
        self.assertIsNone(prepared)  # eligible=false: review and publish jobs are skipped
        self.assertEqual(len(api.checks), 1)
        check = api.checks[0]
        self.assertEqual((check['head_sha'], check['status'], check['conclusion']), (QHEAD, 'completed', 'success'))
        self.assertEqual(check['output']['title'], P.CARRY_TITLE)
        self.assertIn(HEAD, check['output']['summary'])
        self.assertEqual(api.reviews, [])
        self.assertIn('carried from PR head', out)
        self.assertFalse(any(m == 'POST' and p.endswith('/reviews') for m, p, _ in api.calls))

    def test_order_of_listed_files_does_not_matter(self):
        api = carry_fake(); api.compare[(BASE, QHEAD)]['files'].reverse()
        self.assertIsNone(self.run_prepare(api)[0])
        self.assertEqual(api.checks[0]['conclusion'], 'success')

    def test_a_different_tree_is_never_carried(self):
        def other_blob(a): a.compare[(BASE, QHEAD)]['files'][0]['sha'] = '3' * 40
        def extra_file(a): a.compare[(BASE, QHEAD)]['files'].append(file_row('other-pr.yaml', '4' * 40))
        def missing_file(a): a.compare[(BASE, QHEAD)]['files'].pop()
        def other_status(a): a.compare[(BASE, QHEAD)]['files'][1]['status'] = 'modified'
        def other_rename(a): a.compare[(BASE, QHEAD)]['files'][0]['previous_filename'] = 'old.yaml'
        def empty_both(a):
            for key in a.compare: a.compare[key]['files'] = []
        def queue_not_from_base(a): a.compare[(BASE, QHEAD)]['merge_base_commit']['sha'] = 'e' * 40
        for mutate in (other_blob, extra_file, missing_file, other_status, other_rename, empty_both, queue_not_from_base):
            with self.subTest(mutate=mutate.__name__):
                api = carry_fake(); mutate(api)
                prepared, out = self.run_prepare(api)
                self.assert_model_runs(api, prepared)
                self.assertIn('not carried', out)

    def test_head_verdict_must_be_a_complete_success_bound_to_this_exact_head_and_pr(self):
        def drop(a): a.other_checks.clear()
        def failed(a): a.other_checks[0]['conclusion'] = 'failure'
        def neutral(a): a.other_checks[0]['conclusion'] = 'neutral'
        def cancelled(a): a.other_checks[0]['conclusion'] = 'cancelled'
        def running(a): a.other_checks[0].update(status='in_progress', conclusion=None)
        def queued_success(a): a.other_checks[0]['status'] = 'queued'
        def stale_head(a): a.other_checks[0]['head_sha'] = 'c' * 40
        def other_pr(a): a.other_checks[0]['external_id'] = 'agy/v1:' + REPO + ':8:50:1'
        def other_source(a): a.other_checks[0]['external_id'] = 'agy/v1:mctlhq/elsewhere:7:50:1'
        def no_external_id(a): a.other_checks[0].pop('external_id')
        def carried_itself(a): a.other_checks[0]['output']['title'] = P.CARRY_TITLE
        def no_output(a): a.other_checks[0].pop('output')
        def duplicate(a): a.other_checks.append(copy.deepcopy(a.other_checks[0]))
        def foreign_app(a): a.other_checks[0]['app']['id'] = 999
        for mutate in (drop, failed, neutral, cancelled, running, queued_success, stale_head, other_pr,
                       other_source, no_external_id, carried_itself, no_output, duplicate, foreign_app):
            with self.subTest(mutate=mutate.__name__):
                api = carry_fake(); mutate(api)
                prepared, out = self.run_prepare(api)
                self.assert_model_runs(api, prepared)

    def test_the_head_must_be_the_live_pr_head_the_queue_commit_was_built_from(self):
        for mutate in (lambda a: a.pr['head'].update(sha='c' * 40),
                       lambda a: a.commits.update({QHEAD: [BASE, 'c' * 40]}),
                       lambda a: a.commits.update({QHEAD: [HEAD, BASE]}),
                       lambda a: a.commits.update({QHEAD: [BASE, 'c' * 40, HEAD]})):
            with self.subTest():
                api = carry_fake(); mutate(api)
                try:
                    prepared, _ = self.run_prepare(api)
                except P.Refused:
                    continue  # the unchanged queue_state refusal, no check is green
                self.assertNotEqual(api.checks and api.checks[0].get('conclusion'), 'success')

    def test_unreadable_or_partial_reads_fall_back_to_the_model(self):
        def fail_on(marker):
            return lambda a: setattr(a, 'error', lambda m, p: marker in p)
        def malformed(key, value):
            def go(a):
                a.compare[(BASE, QHEAD)][key] = value
            return go
        def many(a):
            rows = [file_row(f'f{i}.yaml', '5' * 40) for i in range(150)]
            for key in a.compare: a.compare[key]['files'] = copy.deepcopy(rows)
        cases = {
            'compare pr head': fail_on('/compare/' + BASE + '...' + HEAD),
            'compare queue commit': fail_on('/compare/' + BASE + '...' + QHEAD),
            'later page': lambda a: (many(a), setattr(a, 'error', lambda m, p: 'page=2' in p)),
            'check listing': lambda a: setattr(a, 'error', lambda m, p: '/commits/' + HEAD + '/check-runs' in p),
            'files not a list': malformed('files', 'oops'),
            'no merge base': malformed('merge_base_commit', None),
            'bad merge base': malformed('merge_base_commit', {'sha': 'zz'}),
            'zero changes': lambda a: [r.update(changes=0) for k in a.compare for r in a.compare[k]['files']],
            'bad blob': lambda a: [r.update(sha='x') for k in a.compare for r in a.compare[k]['files']],
            'unknown status': lambda a: [r.update(status='changed') for k in a.compare for r in a.compare[k]['files']],
            'entry not an object': lambda a: [a.compare[k]['files'].insert(0, 'x') for k in a.compare],
            'duplicate path': lambda a: [a.compare[k]['files'].append(dict(a.compare[k]['files'][0])) for k in a.compare],
            'unconfigured': lambda a: a.compare.clear(),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                api = carry_fake(); mutate(api)
                prepared, out = self.run_prepare(api)
                self.assert_model_runs(api, prepared)
                self.assertIn('not carried', out)

    def test_a_listing_of_exactly_one_full_page_and_a_complete_second_page_still_works(self):
        api = carry_fake()
        rows = [file_row(f'f{i:03}.yaml', '5' * 40) for i in range(150)]
        for key in api.compare: api.compare[key]['files'] = copy.deepcopy(rows)
        self.assertIsNone(self.run_prepare(api)[0])
        self.assertEqual(api.checks[0]['conclusion'], 'success')

    def test_too_many_files_are_never_carried(self):
        api = carry_fake()
        rows = [file_row(f'f{i:03}.yaml', '5' * 40) for i in range(300)]
        for key in api.compare: api.compare[key]['files'] = copy.deepcopy(rows)
        prepared, _ = self.run_prepare(api)
        self.assert_model_runs(api, prepared)

    def test_queue_moving_after_the_comparison_is_not_carried(self):
        api = carry_fake()
        seen = []
        def move(fake):
            seen.append(1)
            if len(seen) == 2:  # moves after target() and carry_proof()'s entry: the re-read after the comparisons sees it
                fake.queue_refs[QREF] = 'e' * 40
        api.move_on_queue_read = move
        prepared, _ = self.run_prepare(api)
        self.assertEqual(len(seen), 3)
        self.assertNotEqual(api.checks[0].get('conclusion'), 'success')

    def test_ordinary_pr_head_reviews_are_never_carried(self):
        api = carry_fake(); api.other_checks.clear()
        pub = P.Publisher(api, api, ENV)
        self.assertEqual(pub.prepare(), {'number': 7, 'head': HEAD, 'base': BASE})
        self.assertEqual(api.checks[0]['status'], 'in_progress')
        self.assertFalse(any('/compare/' in p for _, p, _ in api.calls))

    def test_an_owned_in_progress_check_on_the_queue_commit_is_completed_not_duplicated(self):
        api = carry_fake()
        api.checks.append({'id': 1, 'name': 'agy-formal-review', 'app': {'id': 123}, 'head_sha': QHEAD,
                           'status': 'in_progress', 'external_id': 'agy/v1:' + REPO + ':7:99:1'})
        self.assertIsNone(self.run_prepare(api)[0])
        self.assertEqual(len(api.checks), 1)
        self.assertEqual(api.checks[0]['conclusion'], 'success')

    def test_a_newer_run_owning_the_queue_check_is_not_overwritten_by_a_carry(self):
        api = carry_fake()
        api.checks.append({'id': 1, 'name': 'agy-formal-review', 'app': {'id': 123}, 'head_sha': QHEAD,
                           'status': 'in_progress', 'external_id': 'agy/v1:' + REPO + ':7:101:1'})
        with self.assertRaises(P.Refused):
            self.run_prepare(api)
        self.assertNotIn('conclusion', api.checks[0])

    def test_carried_check_cannot_itself_be_carried_and_model_path_still_publishes(self):
        # The fallback path is unchanged: prepare, then a model verdict marks the commit.
        api = carry_fake(); api.other_checks.clear()
        pub = P.Publisher(api, api, QENV)
        self.assertEqual(pub.prepare(), {'number': 7, 'head': QHEAD, 'base': BASE})
        record = NS['make_record'](PASS, dict(ENV, HEAD_SHA=QHEAD, BASE_SHA=BASE))
        self.assertEqual(pub.publish(json.dumps(record)), 'APPROVE')
        self.assertEqual(api.checks[0]['conclusion'], 'success')
        self.assertNotEqual(api.checks[0]['output']['title'], P.CARRY_TITLE)


class QueueTests(unittest.TestCase):
    """Merge queue mode (mctlhq/.github#351): mark the queued commit only."""

    def setup_pub(self, api=None, env=None):
        api = api or queue_fake()
        pub = P.Publisher(api, api, env or QENV)
        prepared = pub.prepare()
        return api, pub, prepared

    def raw(self, review_text=PASS, **changes):
        record = NS['make_record'](review_text, dict(ENV, HEAD_SHA=QHEAD, BASE_SHA=BASE))
        record.update(changes)
        return json.dumps(record)

    def test_queue_pass_marks_only_the_queued_commit_and_posts_no_review(self):
        api, pub, prepared = self.setup_pub()
        self.assertEqual(prepared, {'number': 7, 'head': QHEAD, 'base': BASE})
        self.assertEqual(api.checks[0]['head_sha'], QHEAD)
        self.assertEqual(pub.publish(self.raw()), 'APPROVE')
        self.assertEqual(api.checks[0]['conclusion'], 'success')
        self.assertEqual(api.reviews, [])
        self.assertFalse(any(m == 'POST' and p.endswith('/reviews') for m, p, _ in api.calls))

    def test_queue_findings_fail_the_check(self):
        api, pub, _ = self.setup_pub()
        self.assertEqual(pub.publish(self.raw('Bug.\n<!-- VERDICT: FAIL:P2 -->')), 'REQUEST_CHANGES')
        self.assertEqual(api.checks[0]['conclusion'], 'failure')
        self.assertEqual(api.reviews, [])

    def test_queue_result_for_the_pr_head_is_stale(self):
        api, pub, _ = self.setup_pub()
        with self.assertRaises(P.Refused):
            pub.publish(self.raw(head_sha=HEAD))
        self.assertNotEqual(api.checks[0].get('conclusion'), 'success')

    def test_queue_needs_a_merge_queue_rule(self):
        api = queue_fake(); api.rules.pop()
        api.rules[1]['parameters']['strict_required_status_checks_policy'] = True
        with self.assertRaises(P.Refused):
            P.Publisher(api, api, QENV).prepare()
        self.assertEqual(api.checks, [])

    def test_queue_needs_the_merge_method(self):
        for method in ('SQUASH', 'REBASE', None):
            with self.subTest(method=method):
                api = queue_fake(); api.rules[-1]['parameters']['merge_method'] = method
                with self.assertRaises(P.Refused) as raised:
                    P.Publisher(api, api, QENV).prepare()
                self.assertIn('MERGE', str(raised.exception))
                self.assertEqual(api.checks, [])

    def test_queue_entry_must_still_exist_and_point_at_the_commit(self):
        for mutate in (lambda a: a.queue_refs.clear(), lambda a: a.queue_refs.update({QREF: 'e' * 40})):
            with self.subTest():
                api = queue_fake(); mutate(api)
                with self.assertRaises(P.Refused):
                    P.Publisher(api, api, QENV).prepare()
                self.assertEqual(api.checks, [])
                api, pub, _ = self.setup_pub()
                mutate(api)
                with self.assertRaises(P.Refused):
                    pub.publish(self.raw())
                self.assertNotEqual(api.checks[0].get('conclusion'), 'success')

    def test_queue_commit_must_be_base_plus_the_queued_pr(self):
        for parents in ([BASE], [BASE, 'e' * 40], ['e' * 40, HEAD], [BASE, HEAD, 'e' * 40], [HEAD, BASE]):
            with self.subTest(parents=parents):
                api = queue_fake(); api.commits[QHEAD] = parents
                with self.assertRaises(P.Refused):
                    P.Publisher(api, api, QENV).prepare()
                self.assertEqual(api.checks, [])

    def test_queued_pr_must_be_open_at_the_queued_head(self):
        for mutate in (lambda a: a.pr['head'].update(sha='e' * 40), lambda a: a.pr.update(state='closed'),
                       lambda a: a.pr.update(draft=True)):
            with self.subTest():
                api = queue_fake(); mutate(api)
                with self.assertRaises(P.Refused):
                    P.Publisher(api, api, QENV).prepare()
                self.assertEqual(api.checks, [])

    def test_queue_move_during_publication_never_marks_green(self):
        api, pub, _ = self.setup_pub()
        reads = []

        def move(fake):
            reads.append(1)
            if len(reads) == 1:  # after the publish-time check, before the re-check
                fake.queue_refs[QREF] = 'e' * 40
        api.move_on_queue_read = move
        with self.assertRaises(P.Refused):
            pub.publish(self.raw())
        self.assertNotEqual(api.checks[0].get('conclusion'), 'success')

    def test_queue_unavailable_reads_refuse(self):
        for marker in ('/git/ref/', '/commits/' + QHEAD):
            with self.subTest(marker=marker):
                api, pub, _ = self.setup_pub()
                api.error = lambda method, path, m=marker: method == 'GET' and m in path and not path.endswith('/check-runs') and 'check-runs' not in path
                with self.assertRaises(P.Refused):
                    pub.publish(self.raw())
                self.assertNotEqual(api.checks[0].get('conclusion'), 'success')

    def test_queue_inputs_are_validated(self):
        for changes in ({'QUEUE_HEAD_REF': 'refs/heads/main'},
                        {'QUEUE_HEAD_REF': 'refs/heads/gh-readonly-queue/main/pr-8-' + BASE},
                        {'QUEUE_HEAD_REF': 'refs/heads/someone/pr-7-' + BASE},
                        {'QUEUE_HEAD_REF': 'refs/heads/gh-readonly-queue/main/pr-7-' + BASE + '/x'},
                        {'QUEUE_HEAD_SHA': 'x'}, {'QUEUE_BASE_SHA': ''},
                        {'QUEUE_HEAD_REF': 'refs/heads/gh-readonly-queue/other/pr-7-' + BASE}):
            with self.subTest(changes=changes):
                api = queue_fake()
                # Every forged ref exists and points at the queued commit, so
                # only the input validation itself can refuse it.
                for ref in ('refs/heads/gh-readonly-queue/other/pr-7-' + BASE,
                            'refs/heads/gh-readonly-queue/main/pr-8-' + BASE):
                    api.queue_refs[ref] = QHEAD
                with self.assertRaises(P.Refused):
                    P.Publisher(api, api, dict(QENV, **changes)).prepare()
                self.assertEqual(api.checks, [])

    def test_queue_ref_must_name_the_queue_base(self):
        # A ref naming any other commit (the PR head included) is refused even
        # when that ref exists and points at the queued commit.
        for named in (HEAD, 'e' * 40):
            with self.subTest(named=named):
                api = queue_fake()
                ref = 'refs/heads/gh-readonly-queue/main/pr-7-' + named
                api.queue_refs[ref] = QHEAD
                with self.assertRaises(P.Refused) as raised:
                    P.Publisher(api, api, dict(QENV, QUEUE_HEAD_REF=ref)).prepare()
                self.assertIn('another base', str(raised.exception))
                self.assertEqual(api.checks, [])

    def test_pr_head_review_accepts_a_merge_queue_instead_of_strict(self):
        api = queue_fake()
        pub = P.Publisher(api, api, ENV)
        self.assertEqual(pub.prepare()['head'], HEAD)
        api.rules.pop()
        with self.assertRaises(P.Refused):
            P.Publisher(api, api, ENV).prepare()


class ExportTests(unittest.TestCase):
    def test_dispatch_passes_only_the_named_publisher_secret(self):
        dispatch = yaml.safe_load((ROOT / '.github/workflows/agy-formal-dispatch.yml').read_text())
        self.assertEqual(dispatch['jobs']['formal']['secrets'], {
            'AGY_REVIEW_APP_PRIVATE_KEY': '${{ secrets.AGY_REVIEW_APP_PRIVATE_KEY }}'})
        formal = yaml.safe_load((ROOT / '.github/workflows/agy-formal-review.yml').read_text())
        self.assertTrue(formal[True]['workflow_call']['secrets']['AGY_REVIEW_APP_PRIVATE_KEY']['required'])
        self.assertNotIn('secrets', formal['jobs']['review'])

    def test_exact_verdicts_only(self):
        self.assertEqual(NS['make_record'](PASS, ENV)['verdict'], 'PASS')
        for body in ('', 'not a review', 'No issues.\n<!-- VERDICT: PASS --> extra',
                     'No issues.\n <!-- VERDICT: PASS -->', '[P1] bug\n<!-- VERDICT: PASS -->',
                     'Finding 1 (P1)\n<!-- VERDICT: PASS -->', 'severity: p2\n<!-- VERDICT: PASS -->'):
            with self.assertRaises(ValueError):
                NS['make_record'](body, ENV)

    def test_ordinary_callers_do_not_get_formal_outputs(self):
        steps = WF['jobs']['review']['steps']
        exporter = next(x for x in steps if x.get('id') == 'formal_record')
        self.assertIn('inputs.formal_review == true', exporter['if'])
        prep = next(x['run'] for x in steps if x.get('name') == 'Prepare review prompt')
        self.assertIn('formal review requires the entire nonempty diff', prep)
        self.assertIn('formal review cannot approve opaque binary changes', prep)
        self.assertIn('EXTRA_CONVENTIONS=""', prep)
        self.assertIn('--no-textconv', prep)

    def test_trusted_jobs_never_checkout_the_pr_and_hold_no_model_step(self):
        formal = yaml.safe_load((ROOT / '.github/workflows/agy-formal-review.yml').read_text())
        for name in ('prepare', 'publish'):
            job = formal['jobs'][name]
            self.assertEqual(job['runs-on'], 'ubuntu-latest')
            self.assertEqual(job['environment'], 'agy-review-publisher')
            checkout = next(x for x in job['steps'] if 'actions/checkout@' in x.get('uses', ''))
            self.assertEqual(checkout['with']['repository'], 'mctlhq/.github')
            self.assertEqual(checkout['with']['ref'], '${{ steps.source.outputs.sha }}')
            source = next(x['run'] for x in job['steps'] if x.get('id') == 'source')
            self.assertIn('referenced_workflows', source)
            self.assertIn('ambiguous/unreadable formal workflow source', source)
        review = formal['jobs']['review']
        self.assertNotIn('secrets', review)
        self.assertEqual(review['with']['conventions'], '')


class SourceResolutionTests(unittest.TestCase):
    def resolve(self, refs, target=REPO):
        formal = yaml.safe_load((ROOT / '.github/workflows/agy-formal-review.yml').read_text())
        scripts = [next(x['run'] for x in formal['jobs'][job]['steps'] if x.get('id') == 'source')
                   for job in ('prepare', 'publish')]
        self.assertEqual(scripts[0], scripts[1])
        script = scripts[0].split("python3 - <<'PY_SOURCE'\n", 1)[1].rsplit('\nPY_SOURCE', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            (Path(directory) / 'source-run.json').write_text(json.dumps({'referenced_workflows': refs}))
            with patch.dict(os.environ, RUNNER_TEMP=directory, GITHUB_OUTPUT=str(output), TARGET_REPOSITORY=target):
                exec(compile(script, '<actual workflow source resolver>', 'exec'), {})
            return output.read_text()

    def test_actual_local_and_external_api_path_forms_resolve_exact_sha(self):
        # Local calls also use canonical owner/repo/path@ref in the REST API.
        # Observed: .github run 43218908988, local claude-review.yml caller.
        prefix = 'mctlhq/.github/.github/workflows/agy-formal-review.yml@'
        for ref in ('main', HEAD):
            with self.subTest(ref=ref):
                self.assertEqual(self.resolve([{'path': prefix + ref, 'sha': HEAD}]),
                                 'sha=' + HEAD + '\ntarget_name=test-repo\n')

    def test_missing_ambiguous_wrong_repository_and_bad_sha_refuse(self):
        valid = {'path': 'mctlhq/.github/.github/workflows/agy-formal-review.yml@main', 'sha': HEAD}
        for refs in (None, [], [valid, valid], [dict(valid, sha='invalid')],
                     [dict(valid, path='evil/.github/.github/workflows/agy-formal-review.yml@main')],
                     [dict(valid, path='./.github/workflows/agy-formal-review.yml')]):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                self.resolve(refs)
        with self.assertRaises(ValueError):
            self.resolve([valid], target='outside/repo')


class PromptGateTests(unittest.TestCase):
    def run_prompt(self, directory, base, head, script):
        script = script.replace('${{ github.run_id }}', 'fixture')
        self.assertNotIn('${{', script)
        env = dict(os.environ, RUNNER_TEMP=directory, GITHUB_ENV=directory + '/env',
                   BASE_SHA=base, HEAD_SHA=head, EXTRA_CONVENTIONS='', FORMAL_REVIEW='true',
                   REVIEW_REPOSITORY=REPO)
        return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script],
                              cwd=directory, env=env, capture_output=True, text=True)

    def test_actual_shell_gate_accepts_complete_text_and_rejects_opaque_or_incomplete_diff(self):
        script = next(x['run'] for x in WF['jobs']['review']['steps']
                      if x.get('name') == 'Prepare review prompt')
        mutant = script.replace('if [ "$FORMAL_REVIEW" = "true" ]; then', 'if false; then')
        self.assertNotEqual(mutant, script)
        for kind in ('text', 'empty', 'large', 'binary'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                def git(*args):
                    return subprocess.check_output(['git', *args], cwd=directory,
                                                   stderr=subprocess.DEVNULL, text=True).strip()

                git('init', '--initial-branch=fixture', '--object-format=sha1')
                file = Path(directory) / 'candidate.txt'
                file.write_text('baseline\n')
                git('add', 'candidate.txt')
                def commit():
                    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        '-c', 'commit.gpgsign=false', 'commit', '-m', 'test: synthetic fixture',
                        '-m', 'Agent: codex')
                    return git('rev-parse', 'HEAD')

                base = commit()
                if kind != 'empty':
                    file.write_bytes({'text': b'complete text change\n',
                                      'large': b'x' * 160000 + b'\n',
                                      'binary': b'\x00opaque\x00binary'}[kind])
                    git('add', 'candidate.txt')
                    head = commit()
                else:
                    head = base
                actual = self.run_prompt(directory, base, head, script)
                if kind == 'text':
                    self.assertEqual(actual.returncode, 0, actual.stderr)
                    prompt = (Path(directory) / 'agy-review-fixture/prompt.md').read_text()
                    self.assertIn('+complete text change', prompt)
                else:
                    self.assertNotEqual(actual.returncode, 0, actual.stdout)
                    # Disable the actual guard and prove the same rejected
                    # fixture becomes incorrectly accepted, then restore it.
                    mutated = self.run_prompt(directory, base, head, mutant)
                    self.assertEqual(mutated.returncode, 0, mutated.stderr)
                    restored = self.run_prompt(directory, base, head, script)
                    self.assertNotEqual(restored.returncode, 0)


class MutationTests(unittest.TestCase):
    def test_critical_mutations_are_killed(self):
        source = PATH.read_text()
        mutations = [
            (r"r'\bP[12]\b'", r"r'\[P[12]\]|\bP[12]\s*[:—-]'", 'test_no_result_malformed_or_wrong_scope_never_approves'),
            ("raise Refused('head/base moved; reviewed result is stale')", 'pass', 'test_move_head_or_base_before_publication_refuses'),
            ("raise Refused('invalid verdict')", 'pass', 'test_no_result_malformed_or_wrong_scope_never_approves'),
            ("raise Refused('formal approval needs stale-review dismissal, strict checks or a merge queue, and the App-bound agy-formal-review check')", 'pass', 'test_every_protection_is_required'),
            ("raise Refused('merge queue review needs a merge queue rule on the branch')", 'pass', 'test_queue_needs_a_merge_queue_rule'),
            ("raise Refused('merge queue ref does not point at the reviewed commit')", 'pass', 'test_queue_entry_must_still_exist_and_point_at_the_commit'),
            ("raise Refused('merge queue commit is not the base plus this PR')", 'pass', 'test_queue_commit_must_be_base_plus_the_queued_pr'),
            ("raise Refused('queued PR is not open at the queued head')", 'pass', 'test_queued_pr_must_be_open_at_the_queued_head'),
            ("raise Refused('merge queue ref names another PR')", 'pass', 'test_queue_inputs_are_validated'),
            ("raise Refused('merge queue ref names another base')", 'pass', 'test_queue_ref_must_name_the_queue_base'),
            ("raise Refused('merge queue review needs merge_method MERGE')", 'pass', 'test_queue_needs_the_merge_method'),
            ("raise Refused('run event or attempt mismatch')", 'pass', 'test_changed_run_attempt_never_publishes'),
            ("raise Refused('queue commit differs from the reviewed PR diff')", 'pass', 'test_a_different_tree_is_never_carried'),
            ("raise Refused('queue commit does not descend from its base')", 'pass', 'test_a_different_tree_is_never_carried'),
            ("raise Refused('PR head has no single formal check')", 'pass', 'test_head_verdict_must_be_a_complete_success_bound_to_this_exact_head_and_pr'),
            ("raise Refused('PR head has no completed success from a formal run for this PR')", 'pass', 'test_head_verdict_must_be_a_complete_success_bound_to_this_exact_head_and_pr'),
            ("raise Refused('PR head verdict is itself carried or unreadable')", 'pass', 'test_head_verdict_must_be_a_complete_success_bound_to_this_exact_head_and_pr'),
            ("raise Refused('comparison entry not comparable')", 'pass', 'test_unreadable_or_partial_reads_fall_back_to_the_model'),
            ("raise Refused('comparison too large or ambiguous')", 'pass', 'test_too_many_files_are_never_carried'),
            ("self.queue_state(self.context()[0], branch)\n        return check", "return check", 'test_queue_moving_after_the_comparison_is_not_carried'),
        ]
        original = P.Publisher
        for old, new, test in mutations:
            suite = next(c for c in (PublisherTests, QueueTests, CarryTests) if hasattr(c, test))
            with self.subTest(test=test):
                self.assertEqual(source.count(old), 1)
                scope = {'__name__': 'mutant'}
                exec(compile(source.replace(old, new), '<mutated publisher>', 'exec'), scope)
                scope['Refused'] = P.Refused
                try:
                    P.Publisher = scope['Publisher']
                    red = unittest.TestResult()
                    suite(test).run(red)
                    self.assertFalse(red.wasSuccessful(), 'the mutation survived the real contract test')
                finally:
                    P.Publisher = original
                green = unittest.TestResult()
                suite(test).run(green)
                self.assertTrue(green.wasSuccessful(), str(green.errors + green.failures))


if __name__ == '__main__':
    unittest.main()
