"""Exercise the actual publisher, diff digest, and inline result exporter."""
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
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / '.github/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pr_diff_digest  # noqa: E402
import review_trust  # noqa: E402

PATH = SCRIPTS / 'agy_review_publish.py'
SPEC = importlib.util.spec_from_file_location('agy_publisher', PATH)
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)
WF = yaml.safe_load((ROOT / '.github/workflows/agy-review.yml').read_text())
EXPORT = next(x['run'] for x in WF['jobs']['review']['steps'] if x.get('id') == 'formal_record')
PACK = EXPORT.split("python3 - <<'PY_RECORD'\n", 1)[1].rsplit('\nPY_RECORD', 1)[0]
NS = {'__name__': 'record_tests'}
exec(compile(PACK, '<actual workflow record exporter>', 'exec'), NS)

HEAD, BASE, ALT_BASE = 'a' * 40, 'b' * 40, 'f' * 40
DIGEST = 'sha256:' + '9' * 64
REPO = 'mctlhq/test-repo'
ENV = {'GITHUB_REPOSITORY': REPO, 'PR_NUMBER': '7', 'GITHUB_RUN_ID': '100',
       'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_NAME': 'pull_request_target',
       'GITHUB_REF': 'refs/heads/main', 'REVIEW_REPOSITORY': REPO, 'AGY_REVIEW_APP_ID': '123',
       'AGY_REVIEW_LOGIN': 'agy-test[bot]', 'HEAD_SHA': HEAD, 'BASE_SHA': BASE,
       'DIFF_DIGEST': DIGEST, 'AGY_MODEL': 'gemini-4-hydrogen-high'}
PASS = 'No significant issues found.\n\n<!-- VERDICT: PASS -->\n'


class Fake:
    def __init__(self):
        self.source_repo = REPO
        self.diff_digest = DIGEST
        self.pr = {'number': 7, 'state': 'open', 'draft': False,
                   'user': {'login': 'pr-author'},
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
        self.workflow_runs = []
        self.run_details = {}
        self.run_jobs = {}
        self.contents = {}
        self.branch_commits = []
        self.edited_nodes = set()
        self.graphql_error = False

    def __call__(self, method, path, payload=None, absent=False):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if self.error and self.error(method, path):
            raise P.Refused('synthetic unavailable read')
        root = 'repos/' + REPO
        bare = path.split('?')[0]
        if method == 'POST' and bare == 'graphql':
            if self.graphql_error:
                return {'errors': [{'message': 'synthetic graphql failure'}]}
            ids = payload['variables']['ids']
            nodes = []
            for nid in ids:
                if nid in self.edited_nodes:
                    nodes.append({'id': nid, 'lastEditedAt': '2026-01-02T00:00:00Z',
                                  'updatedAt': '2026-01-02T00:00:00Z', 'submittedAt': '2026-01-01T00:00:00Z'})
                else:
                    nodes.append({'id': nid, 'lastEditedAt': None,
                                  'updatedAt': '2026-01-01T00:00:00Z', 'submittedAt': '2026-01-01T00:00:00Z'})
            return {'data': {'nodes': nodes}}
        if method == 'GET':
            if bare in (root, 'repos/' + self.source_repo):
                value = {'default_branch': 'main'}
            elif bare == 'repos/' + self.source_repo + '/actions/runs/100':
                value = {'event': 'pull_request_target', 'run_attempt': self.attempt}
            elif bare.startswith(root + '/actions/runs/') and bare.endswith('/jobs'):
                rid = int(bare.split('/')[-2])
                value = {'jobs': self.run_jobs.get(rid, [])}
            elif bare.startswith(root + '/actions/runs/'):
                rid = int(bare.rsplit('/', 1)[1])
                if rid not in self.run_details:
                    raise P.Refused(f'unknown run {rid}')
                value = self.run_details[rid]
            elif bare == root + '/actions/runs' or (bare.startswith(root + '/actions/workflows/') and bare.endswith('/runs')):
                value = {'workflow_runs': self.workflow_runs}
            elif bare.startswith(root + '/contents/'):
                cpath = bare[len(root + '/contents/'):]
                m_ref = re.search(r'[?&]ref=([^&]+)', path)
                ref = m_ref.group(1) if m_ref else 'main'
                if (cpath, ref) not in self.contents:
                    raise P.Refused(f'unconfigured content {cpath}@{ref}')
                value = {'sha': self.contents[(cpath, ref)]}
            elif bare == root + '/commits':
                value = self.branch_commits
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
                if (a, b) in self.compare:
                    body = copy.deepcopy(self.compare[(a, b)])
                    m_page = re.search(r'[?&]page=(\d+)', path)
                    page = int(m_page.group(1)) if m_page else 1
                    files = body.get('files', [])
                    value = dict(body, files=files[(page - 1) * 100:page * 100] if isinstance(files, list) else files)
                elif 'per_page=1' in path and 'per_page=100' not in path:
                    value = {'merge_base_commit': {'sha': BASE}, 'files': []}
                else:
                    raise P.Refused('synthetic unconfigured comparison')
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
            review = {'id': len(self.reviews) + 1, 'node_id': f'PRR_{len(self.reviews) + 1}',
                      'body': payload['body'],
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


def make_pub(api, env=None):
    return P.Publisher(api, api, env or ENV, diff_digest_fn=lambda r, m, h: api.diff_digest)


class PublisherTests(unittest.TestCase):
    def setup_pub(self, env=None):
        api = Fake()
        pub = make_pub(api, env or ENV)
        pub.prepare()
        return api, pub

    def raw(self, review_text=PASS, env=None, **changes):
        record = NS['make_record'](review_text, env or ENV)
        record.update(changes)
        return json.dumps(record)

    def test_complete_current_head_pass_approves_once(self):
        api, pub = self.setup_pub()
        self.assertEqual(pub.publish(self.raw()), 'APPROVE')
        self.assertEqual(pub.publish(self.raw()), 'APPROVE')
        self.assertEqual(len(api.reviews), 1)
        self.assertEqual(api.reviews[0]['commit_id'], HEAD)
        self.assertEqual(api.checks[0]['conclusion'], 'success')
        self.assertTrue(api.checks[0]['external_id'].startswith('agy/v2:'))
        self.assertTrue(api.checks[0]['external_id'].endswith(':' + DIGEST))

    def test_lost_write_response_is_unknown_and_cannot_authorize_merge(self):
        class LostResponse(Fake):
            def __call__(self, method, path, payload=None, absent=False):
                result = super().__call__(method, path, payload, absent)
                if method == 'POST' and path.endswith('/reviews'):
                    raise P.Refused('synthetic lost response after provider accepted review')
                return result

        api = LostResponse()
        pub = make_pub(api)
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
        pub = make_pub(api, env); pub.prepare()
        record = NS['make_record'](PASS, env)
        self.assertEqual(pub.publish(json.dumps(record)), 'APPROVE')
        self.assertEqual(api.reviews[0]['commit_id'], HEAD)
        self.assertIn('/mctlhq/.github/actions/runs/', api.checks[0]['details_url'])

    def test_independent_review_and_shadow_gate_modes(self):
        # Enforce mode under check_name='independent-review' publishes approval + independent-review check.
        api = Fake()
        api.rules[1]['parameters']['required_status_checks'] = [{'context': 'independent-review', 'integration_id': 123}]
        env = dict(ENV, CHECK_NAME='independent-review', GATE_MODE='enforce')
        pub = make_pub(api, env); pub.prepare()
        self.assertEqual(pub.publish(self.raw()), 'APPROVE')
        self.assertEqual(api.checks[0]['name'], 'independent-review')
        self.assertEqual(len(api.reviews), 1)

        # Shadow mode skips PR review creation and uses independent-review-shadow even without independent-review required.
        sapi = Fake()
        sapi.rules[1]['parameters']['required_status_checks'] = [{'context': 'some-other-check', 'integration_id': 999}]
        senv = dict(ENV, CHECK_NAME='independent-review', GATE_MODE='shadow')
        spub = make_pub(sapi, senv); spub.prepare()
        self.assertEqual(spub.publish(self.raw()), 'APPROVE')
        self.assertEqual(sapi.checks[0]['name'], 'independent-review-shadow')
        self.assertEqual(sapi.checks[0]['conclusion'], 'success')
        self.assertEqual(sapi.reviews, [])

        # Invalid check_name or gate_mode refuses immediately.
        for bad in ({'CHECK_NAME': 'claude-review'}, {'GATE_MODE': 'bypass'}):
            with self.assertRaises(P.Refused):
                make_pub(Fake(), dict(ENV, **bad))

    def test_author_matching_reviewer_identity_refuses(self):
        api = Fake()
        api.pr['user']['login'] = 'agy-test[bot]'
        with self.assertRaises(P.Refused):
            make_pub(api).prepare()

    def test_existing_changes_requested_blocks_prepare_and_publish(self):
        # Blocking CHANGES_REQUESTED by claude[bot] on HEAD fails prepare and blocks publish.
        api = Fake()
        api.reviews = [{'id': 9, 'node_id': 'PRR_9', 'commit_id': HEAD,
                        'user': {'login': 'claude[bot]', 'type': 'Bot'},
                        'state': 'CHANGES_REQUESTED', 'body': 'fix P1'}]
        with self.assertRaises(P.Refused):
            make_pub(api).prepare()
        self.assertEqual(api.checks[0]['conclusion'], 'failure')

        # If a blocking CHANGES_REQUESTED lands AFTER prepare (including on publish re-read), publish refuses and dismisses.
        api2, pub2 = self.setup_pub()
        api2.reviews = [{'id': 9, 'node_id': 'PRR_9', 'commit_id': HEAD,
                         'user': {'login': 'claude[bot]', 'type': 'Bot'},
                         'state': 'CHANGES_REQUESTED', 'body': 'edited or not'}]
        with self.assertRaises(P.Refused):
            pub2.publish(self.raw())
        self.assertEqual(len(api2.reviews), 1)
        self.assertNotEqual(api2.checks[0].get('conclusion'), 'success')

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
                  self.raw(schema_version='agy-review/v1'), self.raw(provider='claude'),
                  self.raw(diff_digest='sha256:bad'),
                  self.raw(verdict='UNKNOWN'),
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

    def test_non_strict_base_advance_allowed_only_when_diff_digest_matches(self):
        # strict=false (with merge queue rule): base_sha advanced during review, same diff_digest -> allowed.
        api = Fake()
        api.rules[1]['parameters']['strict_required_status_checks_policy'] = False
        api.rules.append({'type': 'merge_queue', 'parameters': {'merge_method': 'MERGE'}})
        pub = make_pub(api); pub.prepare()
        api.pr['base']['sha'] = ALT_BASE
        self.assertEqual(pub.publish(self.raw()), 'APPROVE')
        self.assertEqual(api.checks[0]['conclusion'], 'success')

        # strict=false: base_sha advanced AND live diff_digest changed -> refused!
        api2 = Fake()
        api2.rules[1]['parameters']['strict_required_status_checks_policy'] = False
        api2.rules.append({'type': 'merge_queue', 'parameters': {'merge_method': 'MERGE'}})
        pub2 = make_pub(api2); pub2.prepare()
        api2.pr['base']['sha'] = ALT_BASE
        api2.diff_digest = 'sha256:' + '1' * 64
        with self.assertRaises(P.Refused):
            pub2.publish(self.raw())
        self.assertEqual(api2.reviews, [])

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
            pub = make_pub(api)
            with self.assertRaises(P.Refused):
                pub.prepare()
            self.assertEqual(api.checks, [])
            self.assertEqual(api.reviews, [])

    def test_untrusted_event_ref_fork_and_target_refuse(self):
        for changes in ({'GITHUB_EVENT_NAME': 'pull_request'}, {'GITHUB_REF': 'refs/pull/7/merge'},
                        {'GITHUB_REPOSITORY': 'outsider/repo'}):
            api = Fake()
            with self.assertRaises(P.Refused):
                make_pub(api, dict(ENV, **changes)).prepare()
        for path, value in (('head_repo', 'outsider/repo'), ('base_ref', 'other')):
            api = Fake()
            if path == 'head_repo':
                api.pr['head']['repo']['full_name'] = value
            else:
                api.pr['base']['ref'] = value
            with self.assertRaises(P.Refused):
                make_pub(api).prepare()

    def test_closed_or_draft_is_not_reviewed(self):
        for mutate in (lambda a: a.pr.update(draft=True), lambda a: a.pr.update(state='closed')):
            api = Fake(); mutate(api)
            self.assertIsNone(make_pub(api).prepare())
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
        api.reviews = [{'id': i, 'user': {'login': 'x', 'type': 'User'}, 'state': 'COMMENTED'} for i in range(100)]
        api.error = lambda method, path: method == 'GET' and '/reviews?' in path and 'page=2' in path
        with self.assertRaises(P.Refused):
            pub.publish(self.raw())
        self.assertFalse(any(method == 'POST' and path.endswith('/reviews') for method, path, _ in api.calls))

    def test_unknown_check_or_newer_run_cannot_be_overwritten(self):
        api, pub = self.setup_pub()
        api.checks[0]['external_id'] = 'agy/v2:mctlhq/test-repo:7:101:1'
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

    def test_controller_handoff_check_can_be_reclaimed_and_mismatched_refused(self):
        for gate_mode in ('enforce', 'shadow'):
            with self.subTest(gate_mode=gate_mode):
                api = Fake()
                if gate_mode == 'enforce':
                    api.rules[1]['parameters']['required_status_checks'] = [
                        {'context': 'independent-review', 'integration_id': 123}
                    ]
                check_name = 'independent-review-shadow' if gate_mode == 'shadow' else 'independent-review'
                api.checks = [{
                    'id': 1, 'name': check_name, 'app': {'id': 123}, 'head_sha': HEAD,
                    'status': 'in_progress',
                    'external_id': f'gate/v1:{REPO}:7:{HEAD}:{gate_mode}:{DIGEST}',
                }]
                env = dict(ENV, CHECK_NAME='independent-review', GATE_MODE=gate_mode)
                pub = make_pub(api, env)
                self.assertEqual(pub.prepare()['head'], HEAD)
                self.assertEqual(pub.publish(self.raw(env=env)), 'APPROVE')
                self.assertTrue(api.checks[0]['external_id'].startswith('agy/v2:'))

        # Mismatched PR/head/mode in gate/v1 external_id must still refuse!
        for bad_eid in (
            f'gate/v1:{REPO}:8:{HEAD}:enforce:{DIGEST}',
            f'gate/v1:{REPO}:7:{"c" * 40}:enforce:{DIGEST}',
            f'gate/v1:{REPO}:7:{HEAD}:shadow:{DIGEST}',
            f'gate/v1:mctlhq/other:7:{HEAD}:enforce:{DIGEST}',
        ):
            with self.subTest(bad_eid=bad_eid):
                api = Fake()
                api.rules[1]['parameters']['required_status_checks'] = [
                    {'context': 'independent-review', 'integration_id': 123}
                ]
                api.checks = [{'id': 1, 'name': 'independent-review', 'app': {'id': 123},
                               'head_sha': HEAD, 'status': 'in_progress', 'external_id': bad_eid}]
                env = dict(ENV, CHECK_NAME='independent-review', GATE_MODE='enforce')
                with self.assertRaises(P.Refused):
                    make_pub(api, env).prepare()

    def test_cross_repo_carry_uses_target_token_and_falls_back_on_actions_403(self):
        api = carry_fake(via_claude=True)
        chk = copy.deepcopy(PRHEAD_CHECK)
        chk['external_id'] = 'agy/v2:mctlhq/.github:7:50:1:' + DIGEST
        api.other_checks = [chk]
        api.reviews.append(copy.deepcopy(PRHEAD_REVIEW))
        api.source_repo = 'mctlhq/.github'

        class SourceOnlyRunAPI:
            def __init__(self, backing):
                self.backing = backing

            def __call__(self, method, path, payload=None, absent=False):
                # Simulates source_repo GITHUB_TOKEN lacking target actions read or failing on target repo actions.
                if path.startswith('repos/' + REPO + '/actions/'):
                    raise P.Refused('GitHub read/write failed: HTTP 403')
                return self.backing(method, path, payload, absent)

        env = dict(QENV, GITHUB_REPOSITORY='mctlhq/.github', TARGET_REPOSITORY=REPO)
        pub = P.Publisher(api, SourceOnlyRunAPI(api), env, diff_digest_fn=lambda r, m, h: api.diff_digest)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(pub.prepare())
        # Option 1 gracefully yielded to Option 2 (Agy PRHEAD_CHECK + PRHEAD_REVIEW), NOT failure!
        self.assertEqual(api.checks[0]['conclusion'], 'success')
        self.assertEqual(api.checks[0]['output']['title'], P.CARRY_TITLE)
        # /contents/ reads happened on `api` (AGY_REVIEW_TOKEN), never failing SourceOnlyRunAPI.
        self.assertFalse(any('/contents/' in p for _, p, _ in api.calls if 'mctlhq/.github' in p))

    def test_cli_cleanup_failure_keeps_original_error_and_newer_check(self):
        for head in ('', HEAD):
            with self.subTest(head=head):
                api, pub = self.setup_pub()
                api.checks[0]['external_id'] = 'agy/v2:mctlhq/test-repo:7:101:1'
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
    rows = [file_row('values.yaml', '1' * 40)]
    api.compare[(BASE, HEAD)] = {'merge_base_commit': {'sha': ALT_BASE}, 'files': copy.deepcopy(rows)}
    api.compare[(BASE, QHEAD)] = {'merge_base_commit': {'sha': BASE}, 'files': copy.deepcopy(rows)}
    return api


PRHEAD_CHECK = {'id': 50, 'name': 'independent-review', 'app': {'id': 123}, 'head_sha': HEAD,
                'status': 'completed', 'conclusion': 'success',
                'external_id': 'agy/v2:' + REPO + ':7:50:1:' + DIGEST,
                'output': {'title': 'Agy formal review: success', 'summary': 'Complete review'}}
PRHEAD_REVIEW = {'id': 500, 'node_id': 'PRR_500', 'commit_id': HEAD,
                 'user': {'login': 'agy-test[bot]', 'type': 'Bot'},
                 'state': 'APPROVED',
                 'body': f'<!-- agy-formal:50:1:{HEAD}:PASS -->\n\nLooks good.'}


def file_row(name, blob, status='modified', changes=3, **extra):
    return dict({'filename': name, 'sha': blob, 'status': status, 'changes': changes}, **extra)


def carry_fake(*, via_claude=False):
    """A queue entry whose commit is the reviewed PR head's exact diff on the base."""
    api = queue_fake()
    rows = [file_row('values.yaml', '1' * 40), file_row('new.yaml', '2' * 40, 'added', 9)]
    api.compare[(BASE, HEAD)] = {'merge_base_commit': {'sha': ALT_BASE}, 'files': copy.deepcopy(rows)}
    api.compare[(BASE, QHEAD)] = {'merge_base_commit': {'sha': BASE}, 'files': copy.deepcopy(rows)}
    if via_claude:
        api.reviews = [{'id': 600, 'node_id': 'PRR_600', 'commit_id': HEAD,
                        'user': {'login': 'claude[bot]', 'type': 'Bot'},
                        'state': 'APPROVED',
                        'body': f'Reviewed head={HEAD} base={BASE}\nLGTM.'}]
        api.workflow_runs = [{'id': 700, 'path': '.github/workflows/claude-review.yml',
                              'event': 'pull_request', 'head_sha': HEAD, 'status': 'completed',
                              'pull_requests': [{'number': 7}]}]
        api.run_details[700] = {'referenced_workflows': [
            {'path': review_trust.TRUSTED_CLAUDE_REUSABLE + 'main', 'sha': 'e' * 40}
        ]}
        wf_sha = '1' * 40
        api.contents[('.github/workflows/claude-review.yml', HEAD)] = wf_sha
        api.contents[('.github/workflows/claude-review.yml', ALT_BASE)] = wf_sha
        api.contents[('.github/workflows/claude-review.yml', 'main')] = wf_sha
    else:
        api.other_checks = [copy.deepcopy(PRHEAD_CHECK)]
        api.reviews = [copy.deepcopy(PRHEAD_REVIEW)]
    return api


class CarryTests(unittest.TestCase):
    """The queue commit is marked from the PR-head verdict only on proof it adds nothing."""

    def run_prepare(self, api, env=None):
        pub = make_pub(api, env or QENV)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            prepared = pub.prepare()
        return prepared, out.getvalue()

    def assert_model_runs(self, api, prepared):
        self.assertEqual(prepared, {'number': 7, 'head': QHEAD, 'base': BASE})
        self.assertEqual(api.checks[0]['status'], 'in_progress')
        self.assertNotEqual(api.checks[0].get('conclusion'), 'success')

    def test_carries_claude_pass_from_comment_run_without_pr_links(self):
        api = carry_fake(via_claude=True)
        api.workflow_runs[0].update(event='issue_comment', head_sha='9' * 40,
                                    pull_requests=[], display_title='Claude review on #7')
        api.run_jobs[700] = [{'name': 'claude-review / review'}]
        prepared, _ = self.run_prepare(api)
        self.assertIsNone(prepared)
        self.assertEqual(api.checks[0]['conclusion'], 'success')
        self.assertIn('trusted claude[bot] APPROVED', api.checks[0]['output']['summary'])

    def test_identical_diff_on_a_reviewed_head_is_carried_from_agy_or_claude_without_model(self):
        for via_claude in (False, True):
            with self.subTest(via_claude=via_claude):
                api = carry_fake(via_claude=via_claude)
                prepared, out = self.run_prepare(api)
                self.assertIsNone(prepared)
                self.assertEqual(len(api.checks), 1)
                check = api.checks[0]
                self.assertEqual((check['head_sha'], check['status'], check['conclusion']), (QHEAD, 'completed', 'success'))
                self.assertEqual(check['output']['title'], P.CARRY_TITLE)
                self.assertIn(HEAD, check['output']['summary'])
                self.assertIn('carried from PR head', out)
                if via_claude:
                    self.assertIn('trusted claude[bot] APPROVED verdict', check['output']['summary'])
                else:
                    self.assertIn('complete agy review', check['output']['summary'])
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

    def test_head_verdict_must_be_complete_unedited_and_bound_to_exact_head_pr_and_digest(self):
        def drop(a): a.other_checks.clear()
        def failed(a): a.other_checks[0]['conclusion'] = 'failure'
        def neutral(a): a.other_checks[0]['conclusion'] = 'neutral'
        def cancelled(a): a.other_checks[0]['conclusion'] = 'cancelled'
        def running(a): a.other_checks[0].update(status='in_progress', conclusion=None)
        def queued_success(a): a.other_checks[0]['status'] = 'queued'
        def stale_head(a): a.other_checks[0]['head_sha'] = 'c' * 40
        def other_pr(a): a.other_checks[0]['external_id'] = 'agy/v2:' + REPO + ':8:50:1:' + DIGEST
        def other_source(a): a.other_checks[0]['external_id'] = 'agy/v2:mctlhq/elsewhere:7:50:1:' + DIGEST
        def v1_external_id(a): a.other_checks[0]['external_id'] = 'agy/v1:' + REPO + ':7:50:1'
        def wrong_digest(a): a.other_checks[0]['external_id'] = 'agy/v2:' + REPO + ':7:50:1:sha256:' + ('1' * 64)
        def no_external_id(a): a.other_checks[0].pop('external_id')
        def carried_itself(a): a.other_checks[0]['output']['title'] = P.CARRY_TITLE
        def no_output(a): a.other_checks[0].pop('output')
        def duplicate(a): a.other_checks.append(copy.deepcopy(a.other_checks[0]))
        def foreign_app(a): a.other_checks[0]['app']['id'] = 999
        def dismissed_agy_review(a): a.reviews[0]['state'] = 'DISMISSED'
        def edited_agy_review(a): a.edited_nodes.add('PRR_500')
        for mutate in (drop, failed, neutral, cancelled, running, queued_success, stale_head, other_pr,
                       other_source, v1_external_id, wrong_digest, no_external_id, carried_itself,
                       no_output, duplicate, foreign_app, dismissed_agy_review, edited_agy_review):
            with self.subTest(mutate=mutate.__name__):
                api = carry_fake(); mutate(api)
                prepared, out = self.run_prepare(api)
                self.assert_model_runs(api, prepared)

    def test_blocking_changes_requested_or_unreadable_api_in_carry_refuses_without_model(self):
        # Blocking CHANGES_REQUESTED on live_pr_head fails closed immediately in prepare (no model run).
        for login in ('claude[bot]', 'agy-test[bot]'):
            with self.subTest(login=login):
                api = carry_fake()
                api.reviews.append({'id': 601, 'node_id': 'PRR_601', 'commit_id': HEAD,
                                    'user': {'login': login, 'type': 'Bot'},
                                    'state': 'CHANGES_REQUESTED', 'body': 'edited or not'})
                with self.assertRaises(P.Refused):
                    self.run_prepare(api)
                self.assertEqual(api.checks[0]['conclusion'], 'failure')

        # Unreadable API / GraphQL fails closed without falling back to the model.
        def fail_on(marker):
            return lambda a: setattr(a, 'error', lambda m, p: marker in p)
        def malformed(key, value):
            return lambda a: a.compare[(BASE, QHEAD)].update({key: value})
        refusal_cases = {
            'compare pr head': fail_on('/compare/' + BASE + '...' + HEAD),
            'compare queue commit': fail_on('/compare/' + BASE + '...' + QHEAD),
            'graphql failure': lambda a: setattr(a, 'graphql_error', True),
            'files not a list': malformed('files', 'oops'),
            'no merge base': malformed('merge_base_commit', None),
            'bad merge base': malformed('merge_base_commit', {'sha': 'zz'}),
            'unconfigured': lambda a: a.compare.clear(),
        }
        for name, mutate in refusal_cases.items():
            with self.subTest(case=name):
                api = carry_fake(); mutate(api)
                with self.assertRaises(P.Refused):
                    self.run_prepare(api)
                self.assertEqual(api.checks[0]['conclusion'], 'failure')

    def test_non_comparable_or_oversized_comparisons_fall_back_to_model(self):
        def many(a):
            rows = [file_row(f'f{i}.yaml', '5' * 40) for i in range(300)]
            for key in a.compare: a.compare[key]['files'] = copy.deepcopy(rows)
        fallback_cases = {
            'zero changes': lambda a: [r.update(changes=0) for k in a.compare for r in a.compare[k]['files']],
            'bad blob': lambda a: [r.update(sha='x') for k in a.compare for r in a.compare[k]['files']],
            'unknown status': lambda a: [r.update(status='changed') for k in a.compare for r in a.compare[k]['files']],
            'too many files': many,
        }
        for name, mutate in fallback_cases.items():
            with self.subTest(case=name):
                api = carry_fake(); mutate(api)
                prepared, out = self.run_prepare(api)
                self.assert_model_runs(api, prepared)
                self.assertIn('not carried', out)

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
                    continue
                self.assertNotEqual(api.checks and api.checks[0].get('conclusion'), 'success')

    def test_a_listing_of_exactly_one_full_page_and_a_complete_second_page_still_works(self):
        api = carry_fake()
        rows = [file_row(f'f{i:03}.yaml', '5' * 40) for i in range(150)]
        for key in api.compare: api.compare[key]['files'] = copy.deepcopy(rows)
        self.assertIsNone(self.run_prepare(api)[0])
        self.assertEqual(api.checks[0]['conclusion'], 'success')

    def test_queue_moving_after_the_comparison_is_not_carried(self):
        api = carry_fake()
        seen = []
        def move(fake):
            seen.append(1)
            if len(seen) == 2:
                fake.queue_refs[QREF] = 'e' * 40
        api.move_on_queue_read = move
        with self.assertRaises(P.Refused):
            self.run_prepare(api)
        self.assertEqual(len(seen), 3)
        self.assertEqual(api.checks[0]['conclusion'], 'failure')

    def test_ordinary_pr_head_reviews_are_never_carried(self):
        api = carry_fake(); api.other_checks.clear()
        pub = make_pub(api)
        self.assertEqual(pub.prepare(), {'number': 7, 'head': HEAD, 'base': BASE})
        self.assertEqual(api.checks[0]['status'], 'in_progress')
        self.assertFalse(any('/compare/' in p for _, p, _ in api.calls))

    def test_an_owned_in_progress_check_on_the_queue_commit_is_completed_not_duplicated(self):
        api = carry_fake()
        api.checks.append({'id': 1, 'name': 'agy-formal-review', 'app': {'id': 123}, 'head_sha': QHEAD,
                           'status': 'in_progress', 'external_id': 'agy/v2:' + REPO + ':7:99:1'})
        self.assertIsNone(self.run_prepare(api)[0])
        self.assertEqual(len(api.checks), 1)
        self.assertEqual(api.checks[0]['conclusion'], 'success')

    def test_a_newer_run_owning_the_queue_check_is_not_overwritten_by_a_carry(self):
        api = carry_fake()
        api.checks.append({'id': 1, 'name': 'agy-formal-review', 'app': {'id': 123}, 'head_sha': QHEAD,
                           'status': 'in_progress', 'external_id': 'agy/v2:' + REPO + ':7:101:1'})
        with self.assertRaises(P.Refused):
            self.run_prepare(api)
        self.assertNotIn('conclusion', api.checks[0])

    def test_carried_check_cannot_itself_be_carried_and_model_path_still_publishes(self):
        api = carry_fake(); api.other_checks.clear(); api.reviews.clear()
        pub = make_pub(api, QENV)
        self.assertEqual(pub.prepare(), {'number': 7, 'head': QHEAD, 'base': BASE})
        record = NS['make_record'](PASS, dict(ENV, HEAD_SHA=QHEAD, BASE_SHA=BASE))
        self.assertEqual(pub.publish(json.dumps(record)), 'APPROVE')
        self.assertEqual(api.checks[0]['conclusion'], 'success')
        self.assertNotEqual(api.checks[0]['output']['title'], P.CARRY_TITLE)


class QueueTests(unittest.TestCase):
    """Merge queue mode (mctlhq/.github#351): mark the queued commit only."""

    def setup_pub(self, api=None, env=None):
        api = api or queue_fake()
        pub = make_pub(api, env or QENV)
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
            make_pub(api, QENV).prepare()
        self.assertEqual(api.checks, [])

    def test_queue_needs_the_merge_method(self):
        for method in ('SQUASH', 'REBASE', None):
            with self.subTest(method=method):
                api = queue_fake(); api.rules[-1]['parameters']['merge_method'] = method
                with self.assertRaises(P.Refused) as raised:
                    make_pub(api, QENV).prepare()
                self.assertIn('MERGE', str(raised.exception))
                self.assertEqual(api.checks, [])

    def test_queue_entry_must_still_exist_and_point_at_the_commit(self):
        for mutate in (lambda a: a.queue_refs.clear(), lambda a: a.queue_refs.update({QREF: 'e' * 40})):
            with self.subTest():
                api = queue_fake(); mutate(api)
                with self.assertRaises(P.Refused):
                    make_pub(api, QENV).prepare()
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
                    make_pub(api, QENV).prepare()
                self.assertEqual(api.checks, [])

    def test_queued_pr_must_be_open_at_the_queued_head(self):
        for mutate in (lambda a: a.pr['head'].update(sha='e' * 40), lambda a: a.pr.update(state='closed'),
                       lambda a: a.pr.update(draft=True)):
            with self.subTest():
                api = queue_fake(); mutate(api)
                with self.assertRaises(P.Refused):
                    make_pub(api, QENV).prepare()
                self.assertEqual(api.checks, [])

    def test_queue_move_during_publication_never_marks_green(self):
        api, pub, _ = self.setup_pub()
        reads = []

        def move(fake):
            reads.append(1)
            if len(reads) == 1:
                fake.queue_refs[QREF] = 'e' * 40
        api.move_on_queue_read = move
        with self.assertRaises(P.Refused):
            pub.publish(self.raw())
        self.assertNotEqual(api.checks[0].get('conclusion'), 'success')

    def test_queue_unavailable_reads_refuse(self):
        for marker in ('/git/ref/', '/commits/' + QHEAD):
            with self.subTest(marker=marker):
                api, pub, _ = self.setup_pub()
                api.error = lambda method, path, m=marker: method == 'GET' and m in path and 'check-runs' not in path
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
                for ref in ('refs/heads/gh-readonly-queue/other/pr-7-' + BASE,
                            'refs/heads/gh-readonly-queue/main/pr-8-' + BASE):
                    api.queue_refs[ref] = QHEAD
                with self.assertRaises(P.Refused):
                    make_pub(api, dict(QENV, **changes)).prepare()
                self.assertEqual(api.checks, [])

    def test_queue_ref_must_name_the_queue_base(self):
        for named in (HEAD, 'e' * 40):
            with self.subTest(named=named):
                api = queue_fake()
                ref = 'refs/heads/gh-readonly-queue/main/pr-7-' + named
                api.queue_refs[ref] = QHEAD
                with self.assertRaises(P.Refused) as raised:
                    make_pub(api, dict(QENV, QUEUE_HEAD_REF=ref)).prepare()
                self.assertIn('another base', str(raised.exception))
                self.assertEqual(api.checks, [])

    def test_pr_head_review_accepts_a_merge_queue_instead_of_strict(self):
        api = queue_fake()
        pub = make_pub(api)
        self.assertEqual(pub.prepare()['head'], HEAD)
        api.rules.pop()
        with self.assertRaises(P.Refused):
            make_pub(api).prepare()


class ExportTests(unittest.TestCase):
    def test_dispatch_passes_only_the_named_publisher_secret(self):
        dispatch = yaml.safe_load((ROOT / '.github/workflows/agy-formal-dispatch.yml').read_text())
        self.assertEqual(dispatch['jobs']['formal']['secrets'], {
            'AGY_REVIEW_APP_PRIVATE_KEY': '${{ secrets.AGY_REVIEW_APP_PRIVATE_KEY }}'})
        formal = yaml.safe_load((ROOT / '.github/workflows/agy-formal-review.yml').read_text())
        self.assertTrue(formal[True]['workflow_call']['secrets']['AGY_REVIEW_APP_PRIVATE_KEY']['required'])
        self.assertNotIn('secrets', formal['jobs']['review'])

    def test_exact_verdicts_only(self):
        rec = NS['make_record'](PASS, ENV)
        self.assertEqual(rec['verdict'], 'PASS')
        self.assertEqual(rec['schema_version'], 'agy-review/v2')
        self.assertEqual(rec['provider'], 'agy')
        self.assertEqual(rec['diff_digest'], DIGEST)
        for body in ('', 'not a review', 'No issues.\n<!-- VERDICT: PASS --> extra',
                     'No issues.\n <!-- VERDICT: PASS -->', '[P1] bug\n<!-- VERDICT: PASS -->',
                     'Finding 1 (P1)\n<!-- VERDICT: PASS -->', 'severity: p2\n<!-- VERDICT: PASS -->'):
            with self.assertRaises(ValueError):
                NS['make_record'](body, ENV)
        with self.assertRaises(ValueError):
            NS['make_record'](PASS, dict(ENV, DIFF_DIGEST='bad'))

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


class PromptGateAndDigestTests(unittest.TestCase):
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
                    scratch = Path(directory) / 'agy-review-fixture'
                    prompt = (scratch / 'prompt.md').read_text()
                    self.assertIn('+complete text change', prompt)
                    inline_digest = (scratch / 'diff_digest.txt').read_text().strip()
                    mb = git('merge-base', base, head)
                    local_digest = pr_diff_digest.compute_local_diff_digest(directory, mb, head)
                    self.assertEqual(inline_digest, local_digest)
                    self.assertRegex(local_digest, r'^sha256:[0-9a-f]{64}$')
                else:
                    self.assertNotEqual(actual.returncode, 0, actual.stdout)
                    mutated = self.run_prompt(directory, base, head, mutant)
                    self.assertEqual(mutated.returncode, 0, mutated.stderr)
                    restored = self.run_prompt(directory, base, head, script)
                    self.assertNotEqual(restored.returncode, 0)

    def test_digest_handles_spaces_utf8_and_non_conflicting_base_advance_parity(self):
        script = next(x['run'] for x in WF['jobs']['review']['steps']
                      if x.get('name') == 'Prepare review prompt')
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=directory,
                                               stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '--initial-branch=main', '--object-format=sha1')
            (Path(directory) / 'unrelated.txt').write_text('v1\n')
            sub = Path(directory) / 'dir with spaces'
            sub.mkdir()
            target = sub / 'café.txt'
            target.write_text('hello\n', encoding='utf-8')
            git('add', '.')
            git('-c', 'user.name=F', '-c', 'user.email=f@e.i', '-c', 'commit.gpgsign=false',
                'commit', '-m', 'base', '-m', 'Agent: agy')
            base1 = git('rev-parse', 'HEAD')

            git('checkout', '-b', 'feat')
            target.write_text('hello world\n', encoding='utf-8')
            git('add', '.')
            git('-c', 'user.name=F', '-c', 'user.email=f@e.i', '-c', 'commit.gpgsign=false',
                'commit', '-m', 'feat', '-m', 'Agent: agy')
            head = git('rev-parse', 'HEAD')

            # Advance main with an unrelated file; merge-base with head stays base1.
            git('checkout', 'main')
            (Path(directory) / 'unrelated.txt').write_text('v2\n')
            git('add', '.')
            git('-c', 'user.name=F', '-c', 'user.email=f@e.i', '-c', 'commit.gpgsign=false',
                'commit', '-m', 'advance', '-m', 'Agent: agy')
            base2 = git('rev-parse', 'HEAD')

            self.run_prompt(directory, base1, head, script)
            d1 = (Path(directory) / 'agy-review-fixture/diff_digest.txt').read_text().strip()
            mb2 = git('merge-base', base2, head)
            d2 = pr_diff_digest.compute_local_diff_digest(directory, mb2, head)
            self.assertEqual(d1, d2)


class MutationTests(unittest.TestCase):
    def test_critical_mutations_are_killed(self):
        source = PATH.read_text()
        mutations = [
            (r"r'\bP[12]\b'", r"r'\[P[12]\]|\bP[12]\s*[:—-]'", 'test_no_result_malformed_or_wrong_scope_never_approves'),
            ("raise Refused('head/base moved; reviewed result is stale')", 'pass', 'test_move_head_or_base_before_publication_refuses'),
            ("raise Refused('live diff digest does not match reviewed digest')", 'pass', 'test_non_strict_base_advance_allowed_only_when_diff_digest_matches'),
            ("raise Refused('author and reviewer cannot share the same login')", 'pass', 'test_author_matching_reviewer_identity_refuses'),
            ("raise Refused('invalid verdict')", 'pass', 'test_no_result_malformed_or_wrong_scope_never_approves'),
            ("raise Refused('invalid reviewer provider')", 'pass', 'test_no_result_malformed_or_wrong_scope_never_approves'),
            ("if blocked:\n                raise Refused(f'blocking CHANGES_REQUESTED review present on {sha}')",
             'if False:\n                pass', 'test_existing_changes_requested_blocks_prepare_and_publish'),
            ("if not (stale and (strict or queued) and bound):", 'if False:', 'test_every_protection_is_required'),
            ("raise Refused('merge queue review needs a merge queue rule on the branch')", 'pass', 'test_queue_needs_a_merge_queue_rule'),
            ("raise Refused('merge queue ref does not point at the reviewed commit')", 'pass', 'test_queue_entry_must_still_exist_and_point_at_the_commit'),
            ("raise Refused('merge queue commit is not the base plus this PR')", 'pass', 'test_queue_commit_must_be_base_plus_the_queued_pr'),
            ("raise Refused('queued PR is not open at the queued head')", 'pass', 'test_queued_pr_must_be_open_at_the_queued_head'),
            ("raise Refused('merge queue ref names another PR')", 'pass', 'test_queue_inputs_are_validated'),
            ("raise Refused('merge queue ref names another base')", 'pass', 'test_queue_ref_must_name_the_queue_base'),
            ("raise Refused('merge queue review needs merge_method MERGE')", 'pass', 'test_queue_needs_the_merge_method'),
            ("raise Refused('run event or attempt mismatch')", 'pass', 'test_changed_run_attempt_never_publishes'),
            ("raise CarryMiss('queue commit differs from the reviewed PR diff')", 'pass', 'test_a_different_tree_is_never_carried'),
            ("raise CarryMiss('queue commit does not descend from its base')", 'pass', 'test_a_different_tree_is_never_carried'),
            ("or match[6] != live_digest", "or False", 'test_head_verdict_must_be_complete_unedited_and_bound_to_exact_head_pr_and_digest'),
            ("if not unedited:\n            return None", "if False:\n            return None", 'test_head_verdict_must_be_complete_unedited_and_bound_to_exact_head_pr_and_digest'),
            ("raise Refused('unknown or newer formal review owns this head')", 'pass', 'test_controller_handoff_check_can_be_reclaimed_and_mismatched_refused'),
            ("if not source_desc:\n            raise CarryMiss('PR head has no qualifying unedited formal verdict')",
             "if False:\n            raise CarryMiss('x')", 'test_head_verdict_must_be_complete_unedited_and_bound_to_exact_head_pr_and_digest'),
            ("raise CarryMiss(str(exc)) from None", "raise", 'test_non_comparable_or_oversized_comparisons_fall_back_to_model'),
            ("self.queue_state(self.context()[0], branch)\n        return {'description': source_desc, 'diff_digest': live_digest}",
             "return {'description': source_desc, 'diff_digest': live_digest}", 'test_queue_moving_after_the_comparison_is_not_carried'),
        ]
        original = P.Publisher
        for old, new, test in mutations:
            suite = next(c for c in (PublisherTests, QueueTests, CarryTests) if hasattr(c, test))
            with self.subTest(test=test, old=old[:40]):
                self.assertEqual(source.count(old), 1, f'expected unique occurrence for {old!r}')
                scope = {'__name__': 'mutant', '__file__': str(PATH)}
                exec(compile(source.replace(old, new), '<mutated publisher>', 'exec'), scope)
                scope['Refused'] = P.Refused
                scope['CarryMiss'] = P.CarryMiss
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
