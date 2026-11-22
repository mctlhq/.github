#!/usr/bin/env python3
"""[SCHEMA] Independent fixture check. Run: python -I checks/validate_fixtures.py

No import from the Go model. Needs jsonschema (pinned 4.25.1 in
roadmap/requirements.txt). Every positive fixture must validate; every
negative fixture must fail, with a recorded (validator, path) among the errors.
"""
import json
import pathlib
import sys

from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schemas" / "tasks-doc-derived.schema.json"

# negative fixture -> (validator keyword, instance path, message fragment) that
# must be among the errors, so a negative cannot pass for an unrelated reason
EXPECT = {
    "input_required_missing_inputrequests": ("required", "result", "'inputRequests' is a required property"),
    "input_required_empty_inputrequests": ("minProperties", "result/inputRequests", "non-empty"),
    "completed_without_result": ("required", "result", "'result' is a required property"),
    "failed_without_error": ("required", "result", "'error' is a required property"),
    "get_with_resulttype_task": ("const", "result/resultType", "'complete'"),
    "update_without_resulttype": ("required", "result", "'resultType' is a required property"),
    "cancel_without_resulttype": ("required", "result", "'resultType' is a required property"),
    "working_with_inputrequests": ("not", "result", ""),
    "sampling_entry": ("const", "result/inputRequests/hir-01/method", "elicitation/create"),
    "approval_shaped_entry": ("pattern", "result/inputRequests", "aar_1"),
}


def all_errors(validator, instance):
    """Flatten errors, including those nested in oneOf/anyOf contexts."""
    stack = list(validator.iter_errors(instance))
    while stack:
        e = stack.pop()
        yield e
        stack.extend(e.context or [])


def main():
    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator.check_schema(schema)
    v = Draft202012Validator(schema)
    failures = []
    pos = sorted((ROOT / "fixtures" / "responses").glob("*.json"))
    if not pos:
        failures.append("no positive fixtures")
    for p in pos:
        errs = list(v.iter_errors(json.loads(p.read_text())))
        if errs:
            failures.append(f"positive {p.name} fails: {errs[0].message}")
    neg = sorted((ROOT / "fixtures" / "negative").glob("*.json"))
    if {p.stem for p in neg} != set(EXPECT):
        failures.append("negative fixtures and EXPECT differ")
    for p in neg:
        errs = list(all_errors(v, json.loads(p.read_text())))
        if not errs:
            failures.append(f"negative {p.name} validated")
            continue
        want = EXPECT.get(p.stem)
        ok = any((e.validator, "/".join(str(x) for x in e.absolute_path)) == want[:2] and want[2] in e.message
                 for e in errs)
        if not ok:
            failures.append(f"negative {p.name}: expected {want}")
    for f in failures:
        print("FAIL", f)
    print(f"{len(pos)} positive, {len(neg)} negative fixtures checked")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
