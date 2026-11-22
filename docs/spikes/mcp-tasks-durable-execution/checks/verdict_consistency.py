#!/usr/bin/env python3
"""[SCHEMA] Verdict vocabulary consistency (C1-C3). Run: python -I checks/verdict_consistency.py"""
import pathlib
import re
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
REPORT = HERE.parent.parent / "mcp-tasks-durable-execution.md"
MANIFEST = HERE.parent.parent.parent.parent / "roadmap" / "spikes" / "mcp-tasks-durable-execution.yaml"


def issue_outcomes(text):
    m = re.search(r"^Issue outcomes \(pinned\): (.+)$", text, re.M)
    return re.findall(r"[A-Z][A-Z0-9_]{2,}", m.group(1)) if m else []


def mapping_rows(text):
    """Rows of the table under '## Verdict vocabulary' -> [(issue, target)]."""
    sec = text.split("## Verdict vocabulary", 1)[-1].split("###", 1)[0]
    rows = []
    for line in sec.splitlines():
        m = re.match(r"\|\s*`([A-Z][A-Z0-9_]*)`\s*\|\s*`([A-Z][A-Z0-9_]*)`", line)
        if m:
            rows.append(m.groups())
    return rows


def check(text, manifest):
    errs = []
    spec = manifest["spec"]
    issue, rows = issue_outcomes(text), mapping_rows(text)
    if not issue:
        errs.append("C1: pinned issue outcome line missing")
    for o in issue:  # C1
        if sum(1 for r in rows if r[0] == o) != 1:
            errs.append(f"C1: issue outcome {o} needs exactly one mapping row")
    for i, t in rows:  # C2
        if t not in spec.get("outcomes", []):
            errs.append(f"C2: mapping target {t} (from {i}) is not a manifest outcome")
    if re.search(r"^\s*Decision:", text, re.M) and "decision" not in spec:  # C3
        errs.append("C3: report names a Decision: line but the manifest has no spec.decision")
    return errs


def main():
    errs = check(REPORT.read_text(), yaml.safe_load(MANIFEST.read_text()))
    for e in errs:
        print("FAIL", e)
    print("verdict consistency:", "FAIL" if errs else "ok")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
