#!/usr/bin/env python3
"""Parse every `run:` block in a workflow and refuse one the shell cannot.

The reconciler's Python is `--selftest`-gated before every pass. The shell
around it — the run-history read, the tracking issue, the commit — had nothing,
and on 2026-09-11 a single missing quote turned the whole Report step into
`unexpected EOF while looking for matching '"'`. Bash parses a script before
running any of it, so nothing in that step executed: no issue, no comment, and
the commit step behind it was skipped, which left the snapshot frozen and the
liveness reference permanently empty. A reconciler that ran on schedule,
observed correctly and said nothing at all.

The gate obeys the same rule as the tool it guards: it may not report a block
clean unless it actually parsed the whole block. `${{ }}` is expanded by
GitHub before the shell ever sees it, so it is replaced here with a
placeholder — and an UNBALANCED `${{` raises rather than silently discarding
the rest of the script, because handing `bash -n` a fragment and printing
"parses cleanly" is a guard that observed nothing and said OK.

Usage:
    check-workflow-shell.py <workflow.yml> [...]
    check-workflow-shell.py --selftest
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile

import yaml

DEFAULT_SHELL = "bash"


class MalformedWorkflow(Exception):
    """The file could not be read as a workflow, or a block could not be parsed."""


def neutralise(script: str) -> str:
    """Replace every `${{ ... }}` with a placeholder, or refuse the script.

    An unbalanced opener used to leave `depth` above zero for the rest of the
    file, dropping every remaining character — so a malformed expression both
    was a defect and disabled the check for every line below it, including a
    missing quote.
    """
    out: list[str] = []
    depth = 0
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
    if depth:
        raise MalformedWorkflow(
            f"unbalanced '${{{{' — {depth} left open, so the rest of the block "
            f"could not be parsed and must not be reported as clean")
    return "".join(out)


HEREDOC = None  # compiled lazily below, after `re` is imported


def unterminated_heredoc(script: str) -> str | None:
    """The delimiter of the first heredoc never closed, if any.

    Checked here rather than left to `bash -n`, which reports this as a
    WARNING and exits 0 — and only on some versions: bash 5 warns, the bash 3.2
    that ships with macOS says nothing at all. A gate whose coverage depends on
    the runner's bash is a gate that reports clean for the wrong reason.
    """
    import re
    global HEREDOC
    if HEREDOC is None:
        # <<WORD, <<-WORD, <<'WORD', <<"WORD"; <<< is a herestring, not a heredoc.
        HEREDOC = re.compile(r"<<(-?)\s*([\"\']?)([A-Za-z_][A-Za-z0-9_]*)\2")
    lines = script.split("\n")
    for index, line in enumerate(lines):
        if "<<<" in line:
            continue
        match = HEREDOC.search(line)
        if not match:
            continue
        dash, _, word = match.groups()
        for candidate in lines[index + 1:]:
            stripped = candidate.lstrip("\t") if dash else candidate
            if stripped.rstrip() == word:
                break
        else:
            return word
    return None


def blocks(path: pathlib.Path):
    try:
        doc = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise MalformedWorkflow(f"{path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise MalformedWorkflow(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise MalformedWorkflow(f"{path}: not a workflow document")
    for job_name, job in (doc.get("jobs") or {}).items():
        job = job or {}
        for index, step in enumerate(job.get("steps") or []):
            script = (step or {}).get("run")
            if not script:
                continue
            shell = (step.get("shell")
                     or (job.get("defaults") or {}).get("run", {}).get("shell")
                     or DEFAULT_SHELL)
            if shell not in ("bash", "sh"):
                continue
            name = step.get("name") or f"step {index}"
            yield f"{path.name} · {job_name} · {name}", shell, script


def check_block(label: str, shell: str, script: str, workdir: pathlib.Path,
                have_shellcheck: bool) -> str | None:
    """None when the block is fine, otherwise the reason it is not."""
    try:
        text = neutralise(script)
    except MalformedWorkflow as exc:
        return str(exc)

    delimiter = unterminated_heredoc(text)
    if delimiter:
        return (f"heredoc opened with <<{delimiter} is never closed — bash -n "
                f"reports this as a warning and exits 0, and only on some "
                f"versions, so it is checked here instead")

    tmp = workdir / "block.sh"
    tmp.write_text(text)
    result = subprocess.run([shell, "-n", str(tmp)], capture_output=True, text=True)
    # stderr is consulted as well as the exit code: an unterminated heredoc is
    # a `bash -n` warning with status 0, which would otherwise pass a block
    # that cannot run.
    if result.returncode != 0 or result.stderr.strip():
        return result.stderr.strip() or f"{shell} -n exited {result.returncode}"
    if have_shellcheck:
        sc = subprocess.run(
            ["shellcheck", "--shell", shell, "--severity", "warning", str(tmp)],
            capture_output=True, text=True)
        if sc.returncode != 0:
            return sc.stdout.strip() or sc.stderr.strip()
    return None


def check_files(paths: list[pathlib.Path]) -> int:
    have_shellcheck = shutil.which("shellcheck") is not None
    failures = 0
    checked = 0
    with tempfile.TemporaryDirectory() as d:
        workdir = pathlib.Path(d)
        for path in paths:
            try:
                found = list(blocks(path))
            except MalformedWorkflow as exc:
                print(f"FAIL {exc}", file=sys.stderr)
                failures += 1
                continue
            for label, shell, script in found:
                checked += 1
                problem = check_block(label, shell, script, workdir, have_shellcheck)
                if problem:
                    failures += 1
                    print(f"FAIL {label}\n{problem}", file=sys.stderr)
    if failures:
        return 1
    if not checked:
        print("no shell blocks found — this gate would pass on anything",
              file=sys.stderr)
        return 2
    note = "" if have_shellcheck else " (shellcheck not installed, syntax only)"
    print(f"workflow shell: {checked} block(s) parse cleanly{note}")
    return 0


def selftest() -> int:
    """The gate's own gate. Its whole value is that it cannot say OK blindly."""
    failures: list[str] = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    check(neutralise('echo "${{ github.sha }}"') == 'echo "GHA_EXPRESSION"',
          f"expression not replaced: {neutralise('echo ${{ github.sha }}')!r}")
    check(neutralise("echo plain") == "echo plain", "untouched script changed")
    try:
        neutralise('echo "${{ github.sha "')
        failures.append("an unbalanced ${{ must raise, not silently truncate")
    except MalformedWorkflow:
        pass

    with tempfile.TemporaryDirectory() as d:
        workdir = pathlib.Path(d)
        have = shutil.which("shellcheck") is not None
        check(check_block("ok", "bash", 'echo "closed"', workdir, False) is None,
              "a valid block was rejected")
        check(check_block("bad", "bash", 'echo "open', workdir, False) is not None,
              "a missing quote was accepted")
        # An unterminated heredoc: `bash -n` warns and exits 0, so the exit
        # code alone would pass a block that cannot run.
        # Deliberately not delegated to `bash -n`: it warns and exits 0 here,
        # and only on some versions — bash 3.2 on macOS is silent.
        check(unterminated_heredoc("cat <<EOF\nbody\n") == "EOF",
              "an unterminated heredoc was not detected")
        check(unterminated_heredoc("cat <<EOF\nbody\nEOF\n") is None,
              "a closed heredoc was reported as open")
        check(unterminated_heredoc("cat <<-EOF\n\tbody\n\tEOF\n") is None,
              "a tab-indented <<- delimiter was not recognised")
        check(unterminated_heredoc("grep x <<<\"$var\"\n") is None,
              "a herestring was mistaken for a heredoc")
        check(check_block("heredoc", "bash", "cat <<EOF\nbody\n", workdir, False)
              is not None, "an unterminated heredoc was accepted")
        check(check_block("expr", "bash", 'echo "${{ x "', workdir, have) is not None,
              "an unbalanced expression was accepted")

        missing = workdir / "nope.yml"
        try:
            list(blocks(missing))
            failures.append("a missing file must be reported, not traced back")
        except MalformedWorkflow:
            pass

    for message in failures:
        print(f"FAIL: {message}", file=sys.stderr)
    if failures:
        return 1
    print("workflow-shell gate selftest: it cannot report a block it did not parse")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--selftest":
        return selftest()
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    return check_files([pathlib.Path(a) for a in argv])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
