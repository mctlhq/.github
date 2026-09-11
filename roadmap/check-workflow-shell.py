#!/usr/bin/env python3
"""Parse every `run:` block in a workflow and refuse one bash cannot.

The reconciler's Python is `--selftest`-gated before every pass. The shell
around it — the run-history read, the tracking issue, the commit — had nothing,
and on 2026-09-11 a single missing quote turned the whole Report step into
`unexpected EOF while looking for matching '"'`. Bash parses a script before
running any of it, so nothing in that step executed: no issue, no comment, and
the commit step behind it was skipped, which left the snapshot frozen and the
liveness reference permanently empty. A reconciler that ran on schedule,
observed correctly and said nothing at all.

`bash -n` costs milliseconds and catches that whole class. shellcheck is run
too when present, which adds the semantic findings, but is not required so the
gate never depends on a tool the runner may lack.

Usage: check-workflow-shell.py <workflow.yml> [...]
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile

import yaml

DEFAULT_SHELL = "bash"


def blocks(path: pathlib.Path):
    doc = yaml.safe_load(path.read_text())
    for job_name, job in (doc.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            script = step.get("run")
            if not script:
                continue
            shell = step.get("shell") or job.get("defaults", {}).get(
                "run", {}).get("shell") or DEFAULT_SHELL
            if shell not in ("bash", "sh"):
                continue
            name = step.get("name") or f"step {index}"
            yield f"{path.name} · {job_name} · {name}", shell, script


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    failures = 0
    checked = 0
    have_shellcheck = shutil.which("shellcheck") is not None
    for arg in argv:
        path = pathlib.Path(arg)
        for label, shell, script in blocks(path):
            checked += 1
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
                # GitHub expands ${{ }} before bash ever sees it; a literal
                # here would be a syntax error of our own making, so it is
                # replaced with a placeholder rather than parsed.
                fh.write(_neutralise(script))
                tmp = fh.name
            result = subprocess.run([shell, "-n", tmp], capture_output=True, text=True)
            if result.returncode != 0:
                failures += 1
                print(f"FAIL {label}\n{result.stderr.strip()}", file=sys.stderr)
                continue
            if have_shellcheck:
                sc = subprocess.run(
                    ["shellcheck", "--shell", shell, "--severity", "error", tmp],
                    capture_output=True, text=True)
                if sc.returncode != 0:
                    failures += 1
                    print(f"FAIL {label}\n{sc.stdout.strip()}", file=sys.stderr)
    if not checked:
        print("no shell blocks found — this gate would pass on anything",
              file=sys.stderr)
        return 2
    if failures:
        return 1
    note = "" if have_shellcheck else " (shellcheck not installed, syntax only)"
    print(f"workflow shell: {checked} block(s) parse cleanly{note}")
    return 0


def _neutralise(script: str) -> str:
    out, depth, buf = [], 0, ""
    i = 0
    while i < len(script):
        if script.startswith("${{", i):
            depth += 1
            i += 3
            continue
        if depth and script.startswith("}}", i):
            depth -= 1
            i += 2
            if not depth:
                out.append("GHA_EXPRESSION")
            continue
        if not depth:
            out.append(script[i])
        i += 1
    return "".join(out)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
