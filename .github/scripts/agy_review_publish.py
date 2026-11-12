"""Trusted formal-review publisher. Never execute this from a PR checkout."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

CHECK = 'agy-formal-review'
VERDICTS = {'<!-- VERDICT: PASS -->': 'PASS', '<!-- VERDICT: FAIL:P1 -->': 'FAIL:P1',
            '<!-- VERDICT: FAIL:P2 -->': 'FAIL:P2', '<!-- VERDICT: FAIL:P1,P2 -->': 'FAIL:P1,P2'}
SHA = re.compile('[0-9a-f]{40}')


class Refused(RuntimeError):
    pass


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
        req = urllib.request.Request('https://api.github.com/' + path,
              data=None if payload is None else json.dumps(payload).encode(), method=method,
              headers={'Authorization': 'Bearer ' + self.token,
                       'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                       'X-GitHub-Api-Version': '2022-11-28'})
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
    def __init__(self, api, run_api, env):
        self.api, self.run_api, self.env = api, run_api, env
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
        self.root = 'repos/' + self.repo
        self.pr_path = self.root + f'/pulls/{self.number}'
        self.external_id = f'agy/v1:{self.source_repo}:{self.number}:{self.run}:{self.attempt}'

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
        for rule in rules:
            params = rule.get('parameters', {})
            if rule.get('type') == 'pull_request':
                stale |= params.get('dismiss_stale_reviews_on_push') is True
            if rule.get('type') == 'required_status_checks':
                strict |= params.get('strict_required_status_checks_policy') is True
                for check in params.get('required_status_checks', []):
                    bound |= check.get('context') == CHECK and check.get('integration_id') == self.app
        if classic is not None:
            if not isinstance(classic, dict):
                raise Refused('classic protection response malformed')
            stale |= (classic.get('required_pull_request_reviews') or {}).get('dismiss_stale_reviews') is True
            checks = classic.get('required_status_checks') or {}
            strict |= checks.get('strict') is True
            bound |= any(x.get('context') == CHECK and x.get('app_id') == self.app for x in checks.get('checks', []))
        if not (stale and strict and bound):
            raise Refused('formal approval needs stale-review dismissal, strict checks and the App-bound agy-formal-review check')

    def checks(self, head):
        found = pages(self.api, self.root + f'/commits/{head}/check-runs', field='check_runs')
        return [c for c in found if c.get('name') == CHECK and c.get('app', {}).get('id') == self.app]

    def owned_check(self, head):
        checks = self.checks(head)
        if not checks:
            raise Refused('formal check missing')
        if len(checks) != 1 or checks[0].get('external_id') != self.external_id:
            raise Refused('check belongs to another run; do not overwrite it')
        return checks[0]

    def prepare(self):
        pr, branch = self.context()
        if pr['state'] != 'open' or pr['draft']:
            return None
        self.policy(branch)
        old = self.checks(pr['head']['sha'])
        if len(old) > 1:
            raise Refused('ambiguous reviewer checks')
        if old:
            match = re.fullmatch(r'agy/v1:' + re.escape(self.source_repo) + r':([0-9]+):([0-9]+):([0-9]+)', old[0].get('external_id', ''))
            if not match or int(match[1]) != self.number or (int(match[2]), int(match[3])) > (self.run, self.attempt):
                raise Refused('unknown or newer formal review owns this head')
        data = {'name': CHECK, 'head_sha': pr['head']['sha'], 'status': 'in_progress',
                'external_id': self.external_id,
                'details_url': f'https://github.com/{self.source_repo}/actions/runs/{self.run}',
                'output': {'title': 'Agy review running', 'summary': 'No approval until a complete current-head verdict is published.'}}
        if old:
            data.pop('head_sha')
            self.api('PATCH', self.root + f'/check-runs/{integer(old[0]["id"])}', data)
        else:
            self.api('POST', self.root + '/check-runs', data)
        return {'number': self.number, 'head': pr['head']['sha'], 'base': pr['base']['sha']}

    def record(self, raw):
        if len(raw.encode()) > 300_000:
            raise Refused('oversized result')
        result = loads(raw)
        required = {'schema_version', 'repository', 'source_repository', 'pr_number', 'head_sha', 'base_sha',
                    'run_id', 'run_attempt', 'model', 'verdict', 'body'}
        if not isinstance(result, dict) or set(result) != required or result['schema_version'] != 'agy-review/v1':
            raise Refused('invalid result schema')
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

    def finish_check(self, check, conclusion, summary):
        self.api('PATCH', self.root + f'/check-runs/{integer(check["id"])}',
                 {'status': 'completed', 'conclusion': conclusion,
                  'output': {'title': 'Agy formal review: ' + conclusion, 'summary': summary}})

    def publish(self, raw):
        # Unusable/no-verdict outcomes do not become an approving review.
        result = self.record(raw)
        pr, branch = self.context()
        self.policy(branch)
        if pr['state'] != 'open' or pr['draft']:
            raise Refused('PR no longer eligible')
        if (pr['head']['sha'], pr['base']['sha']) != (result['head_sha'], result['base_sha']):
            raise Refused('head/base moved; reviewed result is stale')
        check = self.owned_check(result['head_sha'])
        reviews = pages(self.api, self.pr_path + '/reviews')
        event = 'APPROVE' if result['verdict'] == 'PASS' else 'REQUEST_CHANGES'
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
            review = self.api('POST', self.pr_path + '/reviews',
                 {'commit_id': result['head_sha'], 'event': event,
                  'body': marker + '\n\n_Model: ' + result['model'] + '._\n\n' + result['body']})
        integer(review.get('id'))
        if (review.get('user', {}).get('login') != self.login
                or review.get('user', {}).get('type') != 'Bot'
                or review.get('commit_id') != result['head_sha']
                or review.get('state') != ('APPROVED' if event == 'APPROVE' else 'CHANGES_REQUESTED')):
            raise Refused('published review identity/head/state mismatch')
        # Commit_id is always explicit. Never mark a different/new head green.
        current, branch = self.context()
        if current['head']['sha'] != result['head_sha'] or current['base']['sha'] != result['base_sha']:
            if event == 'APPROVE':
                self.api('PUT', self.pr_path + f'/reviews/{integer(review["id"])}/dismissals',
                         {'message': 'Agy result became stale during publication; a fresh review is required.'})
            raise Refused('head/base moved during publication')
        self.owned_check(result['head_sha'])
        self.policy(branch)
        self.finish_check(check, 'success' if event == 'APPROVE' else 'failure',
                          'Complete review for ' + result['head_sha'] + ': ' + result['verdict'])
        return event

    def fail(self, head):
        if not SHA.fullmatch(head):
            raise Refused('no valid reviewed head for failure status')
        check = self.owned_check(head)
        self.finish_check(check, 'failure', 'Formal publication incomplete or failed; merge is not authorized by this check. Inspect reviews before retrying.')


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
            # If a newer run owns the check, fail() refuses to overwrite it.
            pub.fail(env.get('REVIEWED_HEAD', ''))
            raise
    else:
        raise Refused('unknown mode')


if __name__ == '__main__':
    main()
