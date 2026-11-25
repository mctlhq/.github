"""Comprehensive tests and mutation suite for review_gate_controller (mctlhq/.github#398 T1-T14)."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / '.github/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import review_trust  # noqa: E402

CTRL_PATH = SCRIPTS / 'review_gate_controller.py'
SPEC = importlib.util.spec_from_file_location('review_gate_controller', CTRL_PATH)
C = importlib.util.module_from_spec(SPEC)
sys.modules['review_gate_controller'] = C
SPEC.loader.exec_module(C)

HEAD = 'a' * 40
OLD_HEAD = 'e' * 40
BASE = 'b' * 40
ALT_BASE = 'c' * 40
MB = 'd' * 40
DIGEST = 'sha256:' + '9' * 64
SRC = 'mctlhq/.github'
REPO = 'mctlhq/mctl-gitops'
WF_SHA = '1' * 40


class FakeGitHub:
    def __init__(self, *, mode: str = 'enforce'):
        self.repo = REPO
        self.source_repo = SRC
        self.prs = [
            {
                'number': 42,
                'state': 'open',
                'draft': False,
                'user': {'login': 'pr-author'},
                'head': {'sha': HEAD, 'repo': {'full_name': REPO}},
                'base': {'sha': BASE, 'ref': 'main', 'repo': {'full_name': REPO}},
            }
        ]
        self.rules = [
            {'type': 'pull_request', 'parameters': {'dismiss_stale_reviews_on_push': True}},
            {
                'type': 'required_status_checks',
                'parameters': {
                    'strict_required_status_checks_policy': True,
                    'required_status_checks': [
                        {'context': 'independent-review' if mode == 'enforce' else 'other', 'integration_id': 5732062}
                    ],
                },
            },
        ]
        self.reviews = []
        self.checks = []
        self.claude_runs = []
        self.run_details = {}
        self.run_jobs = {}
        self.contents = {
            ('.github/workflows/claude-review.yml', HEAD): WF_SHA,
            ('.github/workflows/claude-review.yml', MB): WF_SHA,
            ('.github/workflows/claude-review.yml', 'main'): WF_SHA,
        }
        self.branch_commits = []
        self.active_dispatches = []
        self.completed_dispatches = []
        self.dispatch_jobs = {}
        self.edited_nodes = set()
        self.graphql_error = False
        self.error_fn = None
        self.compares = {
            f'{BASE}...{HEAD}': {'merge_base_commit': {'sha': MB}},
            f'{ALT_BASE}...{HEAD}': {'merge_base_commit': {'sha': MB}},
        }
        self.dispatches = []
        self.cancelled = []
        self.written_checks = []
        self.calls = []

    def add_claude_run(self, run_id=101, *, event='pull_request', head_sha=HEAD, status='completed',
                       conclusion='failure', steps=None, pull_requests=None, display_title='Claude review',
                       wf_path='.github/workflows/claude-review.yml',
                       referenced_path=review_trust.TRUSTED_CLAUDE_REUSABLE + 'main', referenced_sha='f' * 40):
        run = {
            'id': run_id,
            'path': wf_path,
            'event': event,
            'head_sha': head_sha,
            'status': status,
            'display_title': display_title,
            'pull_requests': pull_requests if pull_requests is not None else [{'number': 42}],
        }
        self.claude_runs.append(run)
        self.run_details[run_id] = {
            'id': run_id,
            'referenced_workflows': [] if not referenced_path else [{'path': referenced_path, 'sha': referenced_sha}],
        }
        self.run_jobs[run_id] = [
            {
                'id': run_id * 10,
                'name': 'review',
                'conclusion': conclusion,
                'steps': steps or [],
            }
        ]
        return run

    @staticmethod
    def outcome_step(name: str, conclusion: str = 'success') -> dict:
        return {'name': name, 'status': 'completed', 'conclusion': conclusion}

    def add_claude_pass(self, run_id=101, *, base=BASE, edited=False, event='pull_request', head_sha=HEAD):
        self.add_claude_run(
            run_id,
            event=event,
            head_sha=head_sha,
            conclusion='success',
            steps=[self.outcome_step('Classify outcome: PASS')],
        )
        nid = f'PRR_{len(self.reviews) + 1}'
        self.reviews.append({
            'id': len(self.reviews) + 1,
            'node_id': nid,
            'commit_id': HEAD,
            'user': {'login': 'claude[bot]', 'type': 'Bot'},
            'state': 'APPROVED',
            'body': f'LGTM\nReviewed head={HEAD} base={base}',
        })
        if edited:
            self.edited_nodes.add(nid)

    def api(self, method: str, path: str, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if self.error_fn and self.error_fn(method, path):
            raise C.Unknown('synthetic API error')
        bare = path.split('?')[0]
        if method == 'POST' and bare == 'graphql':
            if self.graphql_error:
                return {'errors': [{'message': 'graphql error'}]}
            nodes = []
            for nid in payload['variables']['ids']:
                if nid in self.edited_nodes:
                    nodes.append({'id': nid, 'lastEditedAt': '2026-01-02T00:00:00Z',
                                  'updatedAt': '2026-01-02T00:00:00Z', 'submittedAt': '2026-01-01T00:00:00Z'})
                else:
                    nodes.append({'id': nid, 'lastEditedAt': None,
                                  'updatedAt': '2026-01-01T00:00:00Z', 'submittedAt': '2026-01-01T00:00:00Z'})
            return {'data': {'nodes': nodes}}
        if method == 'GET':
            if bare in (f'repos/{self.repo}', f'repos/{self.source_repo}'):
                return {'default_branch': 'main'}
            if bare == f'repos/{self.repo}/rules/branches/main':
                return copy.deepcopy(self.rules)
            if bare == f'repos/{self.repo}/branches/main/protection':
                raise C.Unknown(f'GET {bare}: HTTP 404')
            if bare == f'repos/{self.repo}/pulls':
                return copy.deepcopy(self.prs)
            if f'/actions/workflows/{C.DISPATCH_WORKFLOW}/runs' in bare:
                if 'status=completed' in path:
                    return {'total_count': len(self.completed_dispatches), 'workflow_runs': copy.deepcopy(self.completed_dispatches)}
                st = path.split('status=')[1].split('&')[0]
                matched = [r for r in self.active_dispatches if r.get('status') == st]
                return {'total_count': len(matched), 'workflow_runs': copy.deepcopy(matched)}
            if '/actions/workflows/' in bare and bare.endswith('/runs'):
                return {'total_count': len(self.claude_runs), 'workflow_runs': copy.deepcopy(self.claude_runs)}
            if bare.startswith(f'repos/{self.repo}/actions/runs/') and bare.endswith('/jobs'):
                rid = int(bare.split('/')[-2])
                jobs = self.run_jobs.get(rid, [])
                return {'total_count': len(jobs), 'jobs': copy.deepcopy(jobs)}
            if bare.startswith(f'repos/{self.source_repo}/actions/runs/') and bare.endswith('/jobs'):
                rid = int(bare.split('/')[-2])
                jobs = self.dispatch_jobs.get(rid, [])
                return {'total_count': len(jobs), 'jobs': copy.deepcopy(jobs)}
            if '/actions/runs/' in bare:
                rid = int(bare.rsplit('/', 1)[1])
                return copy.deepcopy(self.run_details[rid])
            if '/compare/' in bare:
                key = bare.rsplit('/compare/', 1)[1]
                return copy.deepcopy(self.compares[key])
            if bare == f'repos/{self.repo}/pulls/42/reviews':
                return copy.deepcopy(self.reviews)
            if bare == f'repos/{self.repo}/commits/{HEAD}/check-runs':
                return {'total_count': len(self.checks), 'check_runs': copy.deepcopy(self.checks)}
            if bare.startswith(f'repos/{self.repo}/contents/'):
                cpath = bare[len(f'repos/{self.repo}/contents/'):]
                ref = path.split('ref=')[1].split('&')[0]
                return {'sha': self.contents[(cpath, ref)]}
            if bare == f'repos/{self.repo}/commits':
                return copy.deepcopy(self.branch_commits)
            raise AssertionError((method, path))
        if method == 'POST' and bare == f'repos/{self.repo}/check-runs':
            chk = dict(payload, id=900 + len(self.checks), app={'id': 5732062})
            self.checks.append(chk)
            self.written_checks.append(('POST', copy.deepcopy(chk)))
            return chk
        if method == 'PATCH' and bare.startswith(f'repos/{self.repo}/check-runs/'):
            cid = int(bare.rsplit('/', 1)[1])
            for chk in self.checks:
                if chk['id'] == cid:
                    chk.update(payload)
                    self.written_checks.append(('PATCH', copy.deepcopy(chk)))
                    return chk
            raise AssertionError(('PATCH missing check', cid))
        if method == 'POST' and bare.endswith('/dispatches'):
            self.dispatches.append(copy.deepcopy(payload))
            return None
        if method == 'POST' and bare.endswith('/cancel'):
            self.cancelled.append(bare)
            return None
        raise AssertionError((method, path))


def run_controller(gh: FakeGitHub, *, gate_repos: str = f'{REPO}:enforce', live: str = 'true',
                   force_rerun: str = 'false', digest_fn=None, extra_env=None):
    with tempfile.TemporaryDirectory() as tmp:
        metrics_file = Path(tmp) / 'metrics.json'
        env = {
            'GITHUB_REPOSITORY': SRC,
            'GITHUB_REF': 'refs/heads/main',
            'LIVE': live,
            'FORCE_RERUN': force_rerun,
            'AGY_REVIEW_APP_ID': '5732062',
            'AGY_REVIEW_LOGIN': 'mctl-agy-reviewer[bot]',
            'GATE_REPOSITORIES': gate_repos,
            'METRICS_PATH': str(metrics_file),
            'AGY_REVIEW_TOKEN': 'tok-app',
            'GITHUB_TOKEN': 'tok-gh',
        }
        if extra_env:
            env.update(extra_env)
        rc = C.main(env=env, api=gh.api, dispatch_api=gh.api,
                    compute_digest_fn=digest_fn or (lambda r, m, h: DIGEST))
        metrics = json.loads(metrics_file.read_text(encoding='utf-8'))
        return rc, metrics


class ControllerAcceptanceTests(unittest.TestCase):
    def test_t1_claude_valid_unedited_pass_approves_check_without_agy_dispatch(self):
        gh = FakeGitHub()
        # With merge_queue present, strict=false tolerates non-conflicting base advance when diff_digest matches.
        gh.rules[1]['parameters']['strict_required_status_checks_policy'] = False
        gh.rules.append({'type': 'merge_queue', 'parameters': {'merge_method': 'MERGE'}})
        gh.add_claude_pass(base=ALT_BASE)
        rc, m = run_controller(gh)
        self.assertEqual(rc, 0, m)
        self.assertEqual(m['decisions'][0]['outcome'], 'PASS')
        self.assertEqual(m['decisions'][0]['action'], 'pass_check')
        self.assertEqual(gh.checks[0]['name'], 'independent-review')
        self.assertEqual(gh.checks[0]['conclusion'], 'success')
        self.assertEqual(gh.dispatches, [])
        self.assertEqual(m['fallback_rate'], 0.0)

    def test_t2_changes_requested_takes_precedence_over_any_pass(self):
        for author in ('claude[bot]', 'mctl-agy-reviewer[bot]'):
            for edited in (False, True):
                with self.subTest(author=author, edited=edited):
                    gh = FakeGitHub()
                    gh.add_claude_pass()
                    nid = 'PRR_BLOCK'
                    gh.reviews.append({
                        'id': 99, 'node_id': nid, 'commit_id': HEAD,
                        'user': {'login': author, 'type': 'Bot'},
                        'state': 'CHANGES_REQUESTED', 'body': 'blocking bug',
                    })
                    if edited:
                        gh.edited_nodes.add(nid)
                    rc, m = run_controller(gh)
                    self.assertEqual(rc, 0)
                    self.assertEqual(m['decisions'][0]['outcome'], 'BLOCKING_FINDINGS')
                    self.assertEqual(m['decisions'][0]['action'], 'fail_check')
                    self.assertEqual(gh.checks[0]['conclusion'], 'failure')
                    self.assertEqual(gh.dispatches, [])

    def test_t3_transient_failure_dispatches_agy_then_reuses_formal_agy_pass(self):
        gh = FakeGitHub()
        gh.add_claude_run(102, conclusion='failure', steps=[gh.outcome_step('Classify outcome: NO_VERDICT_TRANSIENT')])
        rc, m = run_controller(gh)
        self.assertEqual(rc, 0, m)
        self.assertEqual(m['decisions'][0]['outcome'], 'NO_VERDICT_TRANSIENT')
        self.assertEqual(m['decisions'][0]['action'], 'dispatch')
        self.assertEqual(len(gh.dispatches), 1)
        self.assertEqual(gh.dispatches[0]['inputs']['head_sha'], HEAD)
        self.assertEqual(gh.checks[0]['status'], 'in_progress')

        # Subsequent tick: formal Agy PASS (v2 check + unedited APPROVED review) reuses without re-dispatching.
        gh.dispatches.clear()
        gh.checks = [{
            'id': 300, 'name': 'independent-review', 'app': {'id': 5732062}, 'head_sha': HEAD,
            'status': 'completed', 'conclusion': 'success',
            'external_id': f'agy/v2:{SRC}:42:555:1:{DIGEST}',
            'output': {'title': 'Agy formal review: success', 'summary': 'ok'},
        }]
        gh.run_details[555] = {'referenced_workflows': [{'path': review_trust.TRUSTED_AGY_REUSABLE + 'main', 'sha': 'f' * 40}]}
        gh.reviews = [{
            'id': 301, 'node_id': 'PRR_AGY', 'commit_id': HEAD,
            'user': {'login': 'mctl-agy-reviewer[bot]', 'type': 'Bot'},
            'state': 'APPROVED',
            'body': f'<!-- agy-formal:555:1:{HEAD}:PASS -->\n\nok',
        }]
        rc2, m2 = run_controller(gh)
        self.assertEqual(rc2, 0)
        self.assertEqual(m2['decisions'][0]['outcome'], 'PASS')
        self.assertEqual(m2['decisions'][0]['action'], 'done')
        self.assertEqual(gh.dispatches, [])
        # Existing agy/v2 external_id MUST be preserved untouched (never overwritten by gate/v1:...:0*64).
        self.assertEqual(gh.checks[0]['external_id'], f'agy/v2:{SRC}:42:555:1:{DIGEST}')

    def test_t4_timeout_or_startup_failure_triggers_agy_only_with_unmodified_caller_blob(self):
        for conc in ('timed_out', 'startup_failure'):
            gh = FakeGitHub()
            gh.add_claude_run(103, conclusion=conc, steps=[])
            rc, m = run_controller(gh)
            self.assertEqual(rc, 0)
            self.assertEqual(m['decisions'][0]['action'], 'dispatch')
            self.assertEqual(len(gh.dispatches), 1)

        # Edited/modified caller workflow in PR head is refused for timeout fallback!
        gh_bad = FakeGitHub()
        gh_bad.add_claude_run(104, conclusion='timed_out', steps=[])
        gh_bad.contents[('.github/workflows/claude-review.yml', HEAD)] = '2' * 40
        rc_bad, m_bad = run_controller(gh_bad)
        self.assertEqual(rc_bad, 0)
        self.assertEqual(m_bad['decisions'][0]['action'], 'fail_check')
        self.assertEqual(gh_bad.dispatches, [])

    def test_t5_non_transient_no_verdict_fails_without_fallback_and_self_edit_uses_trusted_agy(self):
        gh = FakeGitHub()
        gh.add_claude_run(105, conclusion='failure', steps=[gh.outcome_step('Classify outcome: NO_VERDICT_OTHER')])
        rc, m = run_controller(gh)
        self.assertEqual(rc, 0)
        self.assertEqual(m['decisions'][0]['outcome'], 'NO_VERDICT_OTHER')
        self.assertEqual(m['decisions'][0]['action'], 'fail_check')
        self.assertEqual(gh.checks[0]['conclusion'], 'failure')
        self.assertEqual(gh.dispatches, [])

        # SELF_EDIT with modified caller blob still dispatches trusted Agy fallback if referenced_workflows is trusted!
        gh_se = FakeGitHub()
        gh_se.add_claude_run(106, conclusion='success', steps=[gh_se.outcome_step('Classify outcome: SELF_EDIT')])
        gh_se.contents[('.github/workflows/claude-review.yml', HEAD)] = '2' * 40
        rc_se, m_se = run_controller(gh_se)
        self.assertEqual(rc_se, 0)
        self.assertEqual(m_se['decisions'][0]['outcome'], 'SELF_EDIT')
        self.assertEqual(m_se['decisions'][0]['action'], 'dispatch')
        self.assertEqual(len(gh_se.dispatches), 1)

    def test_t6_edited_pass_review_and_carried_or_untrusted_agy_never_pass(self):
        # Edited claude[bot] PASS is ignored -> no dispatch unless transient, fails check.
        gh = FakeGitHub()
        gh.add_claude_pass(edited=True)
        rc, m = run_controller(gh)
        self.assertEqual(rc, 0)
        self.assertEqual(m['decisions'][0]['action'], 'fail_check')
        self.assertEqual(gh.checks[0]['conclusion'], 'failure')

        # Carried Agy check or Agy v1 check never satisfies reuse.
        for bad_check in (
            {'id': 300, 'name': 'independent-review', 'app': {'id': 5732062}, 'head_sha': HEAD,
             'status': 'completed', 'conclusion': 'success', 'external_id': f'agy/v1:{SRC}:42:555:1',
             'output': {'title': 'Agy formal review: success', 'summary': 'ok'}},
            {'id': 301, 'name': 'independent-review', 'app': {'id': 5732062}, 'head_sha': HEAD,
             'status': 'completed', 'conclusion': 'success', 'external_id': f'agy/v2:{SRC}:42:555:1:{DIGEST}',
             'output': {'title': 'Agy formal review: success (carried from PR head)', 'summary': 'ok'}},
        ):
            gh2 = FakeGitHub()
            gh2.add_claude_run(107, conclusion='failure', steps=[gh2.outcome_step('Classify outcome: NO_VERDICT_TRANSIENT')])
            gh2.checks = [bad_check]
            gh2.run_details[555] = {'referenced_workflows': [{'path': review_trust.TRUSTED_AGY_REUSABLE + 'main', 'sha': 'f' * 40}]}
            rc2, m2 = run_controller(gh2)
            self.assertEqual(m2['decisions'][0]['action'], 'dispatch')

    def test_t7_terminal_fallback_failure_suppresses_infinite_redispatch_until_force_rerun(self):
        gh = FakeGitHub()
        gh.add_claude_run(108, conclusion='failure', steps=[gh.outcome_step('Classify outcome: NO_VERDICT_TRANSIENT')])
        gh.completed_dispatches = [{
            'id': 800, 'status': 'completed', 'conclusion': 'failure',
            'display_title': f'Agy formal review {REPO}#42 @ {HEAD} [independent-review:enforce]',
        }]
        gh.dispatch_jobs[800] = [{'id': 8001, 'name': 'formal / review', 'conclusion': 'failure'}]
        rc, m = run_controller(gh)
        self.assertEqual(rc, 0)
        self.assertEqual(m['decisions'][0]['action'], 'fail_check')
        self.assertEqual(gh.checks[0]['conclusion'], 'failure')
        self.assertEqual(gh.dispatches, [])

        # force_rerun=true overrides terminal failure suppression.
        rc_fr, m_fr = run_controller(gh, force_rerun='true')
        self.assertEqual(m_fr['decisions'][0]['action'], 'dispatch')
        self.assertEqual(len(gh.dispatches), 1)

    def test_t8_t9_informational_comment_and_pr_modified_workflow_rejected(self):
        # Informational comment (PR_REVIEW/ISSUE_COMMENT style, no APPROVED review) + conclusion=success -> never PASS.
        gh = FakeGitHub()
        gh.add_claude_run(109, conclusion='success', steps=[gh.outcome_step('Classify outcome: PASS')])
        rc, m = run_controller(gh)
        self.assertEqual(rc, 0)
        self.assertEqual(m['decisions'][0]['action'], 'fail_check')
        self.assertEqual(gh.dispatches, [])

        # Even if claude[bot] APPROVED exists, PR-modified .github/workflows/claude-review.yml blocks Claude PASS.
        gh2 = FakeGitHub()
        gh2.add_claude_pass()
        gh2.contents[('.github/workflows/claude-review.yml', HEAD)] = '2' * 40
        rc2, m2 = run_controller(gh2)
        self.assertEqual(rc2, 0)
        self.assertEqual(m2['decisions'][0]['action'], 'fail_check')

    def test_t10_issue_comment_claude_run_matches_pr_number(self):
        gh = FakeGitHub()
        gh.add_claude_pass(event='issue_comment', head_sha='9' * 40)
        rc, m = run_controller(gh)
        self.assertEqual(rc, 0)
        self.assertEqual(m['decisions'][0]['action'], 'pass_check')

    def test_issue_comment_without_pr_links_preserves_trusted_pass(self):
        gh = FakeGitHub()
        gh.add_claude_pass(event='issue_comment', head_sha='9' * 40)
        gh.claude_runs[-1].update(pull_requests=[], display_title='Claude review on #42')
        rc, metrics = run_controller(gh)
        self.assertEqual(rc, 0)
        self.assertEqual(metrics['decisions'][0]['action'], 'pass_check')
        self.assertEqual(gh.dispatches, [])

    def test_reviewed_base_digest_failure_is_isolated_as_unknown(self):
        gh = FakeGitHub()
        gh.rules[1]['parameters']['strict_required_status_checks_policy'] = False
        gh.rules.append({'type': 'merge_queue', 'parameters': {'merge_method': 'MERGE'}})
        gh.compares[f'{ALT_BASE}...{HEAD}'] = {'merge_base_commit': {'sha': '9' * 40}}
        gh.add_claude_pass(base=ALT_BASE)

        def compute_digest(repo, merge_base, head):
            if merge_base != MB:
                raise RuntimeError('synthetic git stderr')
            return DIGEST

        rc, metrics = run_controller(gh, digest_fn=compute_digest)
        self.assertEqual(rc, 1)
        self.assertIn('cannot compute reviewed-base diff digest', metrics['errors'][0])
        self.assertNotIn('synthetic git stderr', metrics['errors'][0])
        self.assertEqual(gh.checks, [])
        self.assertEqual(gh.dispatches, [])

    def test_t11_t12_unreadable_api_fails_closed_as_unknown_and_stale_run_cancellation(self):
        gh = FakeGitHub()
        gh.add_claude_pass()
        gh.graphql_error = True
        rc, m = run_controller(gh)
        self.assertEqual(rc, 1)
        self.assertTrue(m['errors'])
        self.assertEqual(gh.checks, [])
        self.assertEqual(gh.dispatches, [])

        # Stale dispatch run on OLD_HEAD gets cancelled when publish hasn't started; run with started publish stays.
        gh_c = FakeGitHub()
        gh_c.add_claude_pass()
        gh_c.active_dispatches = [
            {'id': 901, 'status': 'queued', 'display_title': f'Agy formal review {REPO}#42 @ {OLD_HEAD} [independent-review:enforce]'},
            {'id': 902, 'status': 'in_progress', 'display_title': f'Agy formal review {REPO}#42 @ {OLD_HEAD} [independent-review:enforce]'},
        ]
        gh_c.dispatch_jobs[901] = [{'id': 1, 'name': 'prepare', 'status': 'completed'}]
        gh_c.dispatch_jobs[902] = [{'id': 2, 'name': 'formal / publish', 'status': 'in_progress'}]
        rc_c, m_c = run_controller(gh_c)
        self.assertEqual(rc_c, 0)
        self.assertEqual(gh_c.cancelled, [f'repos/{SRC}/actions/runs/901/cancel'])

    def test_t13_shadow_mode_and_downgrade_only_override(self):
        gh = FakeGitHub(mode='shadow')
        gh.add_claude_pass()
        rc, m = run_controller(gh, gate_repos=f'{REPO}:shadow')
        self.assertEqual(rc, 0, m)
        self.assertEqual(gh.checks[0]['name'], 'independent-review-shadow')
        self.assertEqual(gh.checks[0]['conclusion'], 'success')

        # Shadow-configured repo cannot be promoted to enforce via dispatch override.
        rc_bad, m_bad = run_controller(
            FakeGitHub(mode='shadow'),
            gate_repos=f'{REPO}:shadow',
            extra_env={'OVERRIDE_GATE_MODE': 'enforce'},
        )
        self.assertEqual(rc_bad, 1)
        self.assertIn('cannot promote shadow-configured repository', m_bad['errors'][0])

        # Shadow mode also requires stale_dismissal and (strict or merge_queue) to match agy_review_publish.
        for bad_name, mutate_rules in (
            ('missing stale dismissal', lambda ghx: ghx.rules[0]['parameters'].clear()),
            ('missing strict and merge queue', lambda ghx: ghx.rules[1]['parameters'].update(strict_required_status_checks_policy=False)),
        ):
            with self.subTest(bad_name=bad_name):
                gh_pol = FakeGitHub(mode='shadow')
                mutate_rules(gh_pol)
                rc_pol, m_pol = run_controller(gh_pol, gate_repos=f'{REPO}:shadow')
                self.assertEqual(rc_pol, 1)
                self.assertIn('lacks baseline branch protection', m_pol['errors'][0])
                self.assertEqual(gh_pol.dispatches, [])

        # CrossRepoActionsAPI falls back on HTTP 403 from dispatch_api without breaking evaluation.
        def failing_dispatch(method, path, payload=None):
            if f'repos/{REPO}/actions/' in path:
                raise C.Unknown(f'GET {path}: HTTP 403')
            return gh.api(method, path, payload)

        fallback_api = C.CrossRepoActionsAPI(failing_dispatch, gh.api)
        gh.checks.clear()
        res_runs = fallback_api('GET', f'repos/{REPO}/actions/workflows/claude-review.yml/runs?per_page=100&page=1')
        self.assertEqual(res_runs['total_count'], len(gh.claude_runs))

        # issue_comment without pull_requests matches #pr cleanly via jobs_fn boundary (never matching #420 for #42).
        gh_bound = FakeGitHub()
        gh_bound.add_claude_run(115, event='issue_comment', head_sha='9' * 40, conclusion='success', pull_requests=[], display_title='Claude PR #420')
        matched_42 = review_trust.find_matching_claude_runs(
            gh_bound.claude_runs, 42, HEAD,
            jobs_fn=lambda rid: gh_bound.run_jobs[rid],
        )
        self.assertEqual(matched_42, [])

        # Non-main/non-SHA branch ref in referenced_workflows must be refused.
        self.assertFalse(review_trust.is_trusted_reusable_ref(
            {'path': review_trust.TRUSTED_AGY_REUSABLE + 'evil-branch', 'sha': 'f' * 40},
            review_trust.TRUSTED_AGY_REUSABLE,
        ))
        self.assertTrue(review_trust.is_trusted_reusable_ref(
            {'path': review_trust.TRUSTED_AGY_REUSABLE + 'main', 'sha': 'f' * 40},
            review_trust.TRUSTED_AGY_REUSABLE,
        ))

        # Shadow reuses completed independent-review-shadow check cleanly without re-dispatching.
        gh_sr = FakeGitHub(mode='shadow')
        gh_sr.add_claude_run(110, conclusion='failure', steps=[gh_sr.outcome_step('Classify outcome: NO_VERDICT_TRANSIENT')])
        gh_sr.checks = [{
            'id': 333, 'name': 'independent-review-shadow', 'app': {'id': 5732062}, 'head_sha': HEAD,
            'status': 'completed', 'conclusion': 'success',
            'external_id': f'agy/v2:{SRC}:42:556:1:{DIGEST}',
            'output': {'title': 'Agy formal review: success', 'summary': 'ok'},
        }]
        gh_sr.run_details[556] = {'referenced_workflows': [{'path': review_trust.TRUSTED_AGY_REUSABLE + 'main', 'sha': 'f' * 40}]}
        rc_sr, m_sr = run_controller(gh_sr, gate_repos=f'{REPO}:shadow')
        self.assertEqual(rc_sr, 0)
        self.assertEqual(m_sr['decisions'][0]['action'], 'done')
        self.assertEqual(gh_sr.dispatches, [])
        self.assertEqual(gh_sr.checks[0]['external_id'], f'agy/v2:{SRC}:42:556:1:{DIGEST}')

    def test_stale_issue_comment_run_before_newer_pr_push_is_ignored(self):
        gh = FakeGitHub()
        # Earlier push on OLD_HEAD + older issue_comment failure on OLD_HEAD must NOT trigger fallback for HEAD.
        gh.add_claude_run(
            90, event='pull_request', head_sha=OLD_HEAD, conclusion='success',
            steps=[], pull_requests=[{'number': 42}],
        )
        gh.claude_runs[-1]['created_at'] = '2026-11-25T10:05:00Z'
        gh.add_claude_run(
            91, event='issue_comment', head_sha='9' * 40, conclusion='failure',
            steps=[gh.outcome_step('Classify outcome: NO_VERDICT_OTHER')],
        )
        gh.claude_runs[-1]['created_at'] = '2026-11-25T10:04:00Z'
        rc, m = run_controller(gh)
        self.assertEqual(rc, 0)
        self.assertEqual(m['decisions'][0]['action'], 'wait')
        self.assertEqual(gh.dispatches, [])

    def test_claude_success_with_stale_base_returns_wait_and_preserves_digest_on_fail_check(self):
        gh = FakeGitHub(mode='enforce')
        gh.rules[1]['parameters']['strict_required_status_checks_policy'] = False
        gh.rules.append({'type': 'merge_queue', 'parameters': {'merge_method': 'MERGE'}})
        # Claude ran and posted unedited APPROVED against ALT_BASE whose compare diff differs from live BASE.
        gh.compares[f'{ALT_BASE}...{HEAD}'] = {'merge_base_commit': {'sha': '9' * 40}}
        gh.add_claude_pass(base=ALT_BASE)
        rc, m = run_controller(
            gh,
            digest_fn=lambda r, m_sha, h: DIGEST if m_sha == MB else ('sha256:' + '7' * 64),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(m['decisions'][0]['outcome'], 'STALE')
        self.assertEqual(m['decisions'][0]['action'], 'wait')
        self.assertEqual(gh.dispatches, [])

        # When fail_check IS written, external_id carries live DIGEST (not sha256:0000...).
        gh_fail = FakeGitHub()
        gh_fail.add_claude_run(112, conclusion='failure', steps=[gh_fail.outcome_step('Classify outcome: NO_VERDICT_OTHER')])
        run_controller(gh_fail)
        self.assertTrue(gh_fail.checks[0]['external_id'].endswith(':' + DIGEST))

    def test_dry_run_writes_zero_checks_or_dispatches(self):
        gh = FakeGitHub()
        gh.add_claude_run(111, conclusion='failure', steps=[gh.outcome_step('Classify outcome: NO_VERDICT_TRANSIENT')])
        rc, m = run_controller(gh, live='false')
        self.assertEqual(rc, 0)
        self.assertEqual(m['decisions'][0]['action'], 'dispatch')
        self.assertEqual(gh.checks, [])
        self.assertEqual(gh.dispatches, [])


class ControllerMutationTests(unittest.TestCase):
    def test_t14_critical_controller_mutations_are_killed(self):
        source = CTRL_PATH.read_text(encoding='utf-8')
        mutations = [
            (
                "if review_trust.has_blocking_changes_requested(reviews, pr.head, (review_trust.CLAUDE_LOGIN, agy_login)):",
                "if False:",
                'test_t2_changes_requested_takes_precedence_over_any_pass',
            ),
            (
                "if claude_ok:\n        metrics.claude_evaluated += 1\n        return Decision(OUTCOME_PASS, 'pass_check', f'trusted claude[bot] PASS for {pr.head} (base {rev_base})', diff_digest=live_digest)",
                "if False:\n        pass",
                'test_t1_claude_valid_unedited_pass_approves_check_without_agy_dispatch',
            ),
            (
                "elif claude_outcome == OUTCOME_OTHER:\n        metrics.no_verdict_by_provider['claude'][OUTCOME_OTHER] += 1\n        return Decision(OUTCOME_OTHER, 'fail_check', claude_reason, diff_digest=live_digest)",
                "elif False:\n        pass",
                'test_t5_non_transient_no_verdict_fails_without_fallback_and_self_edit_uses_trusted_agy',
            ),
            (
                "try:\n            trusted, reason = review_trust.verify_run_caller_trust(\n                lambda p: api('GET', p), repo, run, default_branch, head_sha, merge_base_sha,\n                require_unchanged_blob=True,\n            )\n        except review_trust.TrustError as exc:\n            raise Unknown(str(exc)) from None\n        if not trusted:\n            return OUTCOME_OTHER, 'untrusted Claude transient run: ' + reason",
                "trusted = True",
                'test_t4_timeout_or_startup_failure_triggers_agy_only_with_unmodified_caller_blob',
            ),
            (
                "if term_fail:\n            metrics.no_verdict_by_provider['agy'][OUTCOME_OTHER] += 1\n            return Decision(OUTCOME_OTHER, 'fail_check', term_reason, diff_digest=live_digest)",
                "if False:\n            pass",
                'test_t7_terminal_fallback_failure_suppresses_infinite_redispatch_until_force_rerun',
            ),
            (
                "action = 'done' if reused_chk is not None and reused_chk.get('name') == target_check else 'pass_check'",
                "action = 'pass_check'",
                'test_t3_transient_failure_dispatches_agy_then_reuses_formal_agy_pass',
            ),
            (
                "return OUTCOME_STALE, 'Claude PASS became stale after base/diff move'",
                "return OUTCOME_OTHER, 'broken'",
                'test_claude_success_with_stale_base_returns_wait_and_preserves_digest_on_fail_check',
            ),
            (
                "if override_mode == 'enforce' and configured == 'shadow':\n            raise Unknown(f'cannot promote shadow-configured repository {repo} to enforce via dispatch')",
                "if False:\n            pass",
                'test_t13_shadow_mode_and_downgrade_only_override',
            ),
            (
                "if not (policy.stale_dismissal and (policy.strict or policy.queued)):",
                "if False:",
                'test_t13_shadow_mode_and_downgrade_only_override',
            ),
        ]
        orig_decide = C.decide_pr
        orig_classify = C.classify_claude_run
        orig_parse = C.parse_repo_configs
        orig_eval = C.evaluate_repo
        for old, new, test_name in mutations:
            with self.subTest(test=test_name):
                self.assertEqual(source.count(old), 1, f'expected single occurrence of {old!r}')
                import types
                mod = types.ModuleType('ctrl_mutant')
                mod.__file__ = str(CTRL_PATH)
                sys.modules['ctrl_mutant'] = mod
                try:
                    exec(compile(source.replace(old, new), '<mutated controller>', 'exec'), mod.__dict__)
                    C.decide_pr = mod.__dict__['decide_pr']
                    C.classify_claude_run = mod.__dict__['classify_claude_run']
                    C.parse_repo_configs = mod.__dict__['parse_repo_configs']
                    C.evaluate_repo = mod.__dict__['evaluate_repo']
                    red = unittest.TestResult()
                    ControllerAcceptanceTests(test_name).run(red)
                    self.assertFalse(red.wasSuccessful(), 'mutation survived acceptance test')
                finally:
                    sys.modules.pop('ctrl_mutant', None)
                    C.decide_pr = orig_decide
                    C.classify_claude_run = orig_classify
                    C.parse_repo_configs = orig_parse
                    C.evaluate_repo = orig_eval
                green = unittest.TestResult()
                ControllerAcceptanceTests(test_name).run(green)
                self.assertTrue(green.wasSuccessful(), str(green.errors + green.failures))


if __name__ == '__main__':
    unittest.main()
