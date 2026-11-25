"""Canonical git-tree diff digest (pr_diff_digest_v1) shared across review workflows."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
import tempfile

SHA = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
REPO = re.compile(r'mctlhq/[A-Za-z0-9_.-]+')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
_META = re.compile(r'^:([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40,64}) ([0-9a-f]{40,64}) ([AMDTU])$')


class EmptyDiffError(ValueError):
    """Raised when git diff-tree between merge_base and head produces no changed files."""


def digest_from_diff_tree(raw: bytes) -> str:
    """Compute canonical pr_diff_digest_v1 from `git diff-tree -r --no-renames --full-index -z` bytes."""
    if not raw:
        raise EmptyDiffError('diff-tree output empty')
    if not raw.endswith(b'\x00'):
        raise ValueError('diff-tree output unterminated')
    parts = raw[:-1].split(b'\x00')
    if len(parts) % 2 != 0:
        raise ValueError('diff-tree record count is odd')
    entries: dict[bytes, tuple[str, str, str, str, str]] = {}
    for i in range(0, len(parts), 2):
        try:
            meta = parts[i].decode('ascii')
        except UnicodeDecodeError as exc:
            raise ValueError('diff-tree metadata is not ASCII') from exc
        m = _META.fullmatch(meta)
        if not m:
            raise ValueError(f'malformed diff-tree metadata entry: {meta!r}')
        path_bytes = parts[i + 1]
        if not path_bytes:
            raise ValueError('empty path in diff-tree output')
        if path_bytes in entries:
            raise ValueError('duplicate path in diff-tree output')
        entries[path_bytes] = m.groups()
    if not entries:
        raise ValueError('empty diff-tree entry set')
    buf = bytearray(b'pr_diff_digest_v1\n')
    for path_bytes in sorted(entries):
        old_mode, new_mode, old_blob, new_blob, status = entries[path_bytes]
        buf.extend(f'{old_mode} {new_mode} {old_blob} {new_blob} {status}\t{len(path_bytes)}\n'.encode('ascii'))
        buf.extend(path_bytes)
        buf.extend(b'\n')
    buf.extend(f'{len(entries)}\n'.encode('ascii'))
    return 'sha256:' + hashlib.sha256(buf).hexdigest()


def compute_local_diff_digest(repo_dir: str, merge_base: str, head_sha: str) -> str:
    """Run git diff-tree locally inside repo_dir and return canonical pr_diff_digest_v1."""
    if not (SHA.fullmatch(merge_base) and SHA.fullmatch(head_sha)):
        raise ValueError('invalid commit SHA for local diff digest')
    raw = subprocess.check_output(
        ['git', '-c', 'core.quotePath=false', 'diff-tree', '-r', '--no-renames', '--full-index', '-z',
         merge_base, head_sha],
        cwd=repo_dir,
        stderr=subprocess.PIPE,
    )
    return digest_from_diff_tree(raw)


def compute_remote_diff_digest(repo: str, merge_base: str, head_sha: str, token: str | None = None) -> str:
    """Fetch only tree objects for merge_base and head_sha into an empty repo and return pr_diff_digest_v1."""
    if not REPO.fullmatch(repo):
        raise ValueError('invalid repository for remote diff digest')
    if not (SHA.fullmatch(merge_base) and SHA.fullmatch(head_sha)):
        raise ValueError('invalid commit SHA for remote diff digest')
    env = {
        'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': '/dev/null',
        'GIT_TERMINAL_PROMPT': '0',
    }
    if token:
        basic = base64.b64encode(f'x-access-token:{token}'.encode('utf-8')).decode('ascii')
        env['GIT_CONFIG_COUNT'] = '1'
        env['GIT_CONFIG_KEY_0'] = 'http.https://github.com/.extraheader'
        env['GIT_CONFIG_VALUE_0'] = f'AUTHORIZATION: basic {basic}'
    url = f'https://github.com/{repo}.git'
    with tempfile.TemporaryDirectory(prefix='diff-digest-') as tmp:
        def run_git(*args: str) -> bytes:
            try:
                return subprocess.check_output(
                    ['git', '-c', 'core.fsmonitor=false', *args],
                    cwd=tmp,
                    env=env,
                    stderr=subprocess.PIPE,
                )
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f'git {args[0]} failed (exit {exc.returncode})') from None

        run_git('init', '--quiet')
        run_git('remote', 'add', 'origin', url)
        run_git('fetch', '--no-tags', '--filter=blob:none', '--depth=1', 'origin', merge_base, head_sha)
        raw = run_git('-c', 'core.quotePath=false', 'diff-tree', '-r', '--no-renames', '--full-index', '-z',
                      merge_base, head_sha)
    return digest_from_diff_tree(raw)
