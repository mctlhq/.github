"""Exercise the actual publisher and inline result exporter with synthetic API responses."""
import copy
import importlib.util
import json
from pathlib import Path
import re
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
                value = {'check_runs': self.checks}
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


class ExportTests(unittest.TestCase):
    def test_exact_verdicts_only(self):
        self.assertEqual(NS['make_record'](PASS, ENV)['verdict'], 'PASS')
        for body in ('', 'not a review', 'No issues.\n<!-- VERDICT: PASS --> extra',
                     'No issues.\n <!-- VERDICT: PASS -->', '[P1] bug\n<!-- VERDICT: PASS -->'):
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


class MutationTests(unittest.TestCase):
    def test_critical_mutations_are_killed(self):
        source = PATH.read_text()
        mutations = [
            ("raise Refused('head/base moved; reviewed result is stale')", 'pass', 'test_move_head_or_base_before_publication_refuses'),
            ("raise Refused('invalid verdict')", 'pass', 'test_no_result_malformed_or_wrong_scope_never_approves'),
            ("raise Refused('formal approval needs stale-review dismissal, strict checks and the App-bound agy-formal-review check')", 'pass', 'test_every_protection_is_required'),
            ("raise Refused('run event or attempt mismatch')", 'pass', 'test_changed_run_attempt_never_publishes'),
        ]
        original = P.Publisher
        for old, new, test in mutations:
            with self.subTest(test=test):
                self.assertEqual(source.count(old), 1)
                scope = {'__name__': 'mutant'}
                exec(compile(source.replace(old, new), '<mutated publisher>', 'exec'), scope)
                scope['Refused'] = P.Refused
                try:
                    P.Publisher = scope['Publisher']
                    red = unittest.TestResult()
                    PublisherTests(test).run(red)
                    self.assertFalse(red.wasSuccessful(), 'the mutation survived the real contract test')
                finally:
                    P.Publisher = original
                green = unittest.TestResult()
                PublisherTests(test).run(green)
                self.assertTrue(green.wasSuccessful(), str(green.errors + green.failures))


if __name__ == '__main__':
    unittest.main()
