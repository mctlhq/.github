#!/usr/bin/env python3
"""Offline 2-replica gate for the mctl-support runtime spike (.github#249).

Runs the same A2A 1.0 HTTP+JSON conversation against two replicas of one
candidate that share one Postgres. No model, no network beyond the two
replicas: the candidate runs its tool loop on the deterministic fake runner
described in README.md. Every step prints PASS/FAIL with the evidence; the
exit code is the number of failed steps.

    python3 gate/gate.py --a http://127.0.0.1:8001/v1 --b http://127.0.0.1:8002/v1 \
        --token spike-namax66

Only the standard library is used so the gate runs the same way for every
candidate.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

FAILS = []


def say(ok, step, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {step}" + (f"  -- {detail}" if detail else ""), flush=True)
    if not ok:
        FAILS.append(step)
    return ok


def http(method, url, body=None, token=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("A2A-Version", "1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="replace")


def state(task):
    s = (task or {}).get("status", {}).get("state", "")
    return str(s).lower().replace("task_state_", "")


def texts(task):
    out = []
    for a in (task or {}).get("artifacts", []) or []:
        for p in a.get("parts", []) or []:
            if "text" in p:
                out.append(p["text"])
    m = (task or {}).get("status", {}).get("message") or {}
    for p in m.get("parts", []) or []:
        if "text" in p:
            out.append(p["text"])
    return "\n".join(out)


def send(base, token, text, context_id=None, message_id=None, blocking=False):
    msg = {"messageId": message_id or str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": text}]}
    if context_id:
        msg["contextId"] = context_id
    body = {"message": msg, "configuration": {"returnImmediately": not blocking}}
    return http("POST", f"{base}/message:send", body, token)


def get(base, token, task_id):
    return http("GET", f"{base}/tasks/{task_id}", None, token)


def wait_for(base, token, task_id, wanted, seconds):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        code, last = get(base, token, task_id)
        if code == 200 and state(last) in wanted:
            return last
        time.sleep(0.5)
    return last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="replica A REST base, e.g. http://127.0.0.1:8001/v1")
    ap.add_argument("--b", required=True, help="replica B REST base")
    ap.add_argument("--token", default="spike-namax66", help="bearer the fake auth accepts (prefix spike-)")
    args = ap.parse_args()
    A, B, tok = args.a.rstrip("/"), args.b.rstrip("/"), args.token
    origin_a = A.split("/v1")[0] if "/v1" in A else A

    # 1. agent card
    code, card = http("GET", f"{origin_a}/.well-known/agent-card.json")
    ok = code == 200 and isinstance(card, dict) and card.get("name") and card.get("skills")
    say(ok, "1 agent card served", f"HTTP {code}, name={card.get('name') if isinstance(card, dict) else card!r}, "
        f"interfaces={[i.get('protocolBinding') for i in (card.get('supportedInterfaces') or [])] if isinstance(card, dict) else '?'}")

    # 2. auth: no bearer / wrong bearer are refused before the executor runs
    code, body = send(A, None, "status of svc-x")
    say(code in (401, 403), "2a message:send without bearer refused", f"HTTP {code}")
    code, body = send(A, "not-a-spike-token", "status of svc-x")
    say(code in (401, 403), "2b message:send with foreign bearer refused", f"HTTP {code}")

    # 3. send to A, non-blocking
    code, resp = send(A, tok, "status of svc-x")
    task = (resp or {}).get("task") if isinstance(resp, dict) else None
    ok = code == 200 and task and task.get("id")
    say(ok, "3 send to A returns a task", f"HTTP {code}, state={state(task)}, id={task.get('id') if task else resp!r}")
    if not ok:
        # Nothing after this step can run without a task; exit with the
        # failures so far rather than returning into a clean exit 0.
        print(f"\n{len(FAILS)} failed: {FAILS}", flush=True)
        sys.exit(len(FAILS))
    tid, ctx = task["id"], task.get("contextId")

    # 4. poll via B until completed; answer carries tool evidence and the principal
    done = wait_for(B, tok, tid, {"completed"}, 30)
    t = texts(done)
    say(state(done) == "completed", "4a task completes, read via B", f"state={state(done)}")
    say("svc-x is degraded" in t, "4b answer carries the fake tool's evidence", t.replace("\n", " | ")[:200])
    say("principal=namax66" in t, "4c executor saw the bearer's principal", "")
    say(bool(ctx) and done.get("contextId") == ctx, "4d contextId stable across replicas", f"ctx={ctx}")

    # 5. continue the context via B, then via A
    code, resp = send(B, tok, "and again?", context_id=ctx)
    t2 = (resp or {}).get("task") if isinstance(resp, dict) else None
    d2 = wait_for(A, tok, t2["id"], {"completed"}, 30) if t2 else None
    say(d2 is not None and "turn 2" in texts(d2), "5a context continued on B sees turn 1 (written by A)", texts(d2).replace("\n", " | ")[:160])
    code, resp = send(A, tok, "and again?", context_id=ctx)
    t3 = (resp or {}).get("task") if isinstance(resp, dict) else None
    d3 = wait_for(B, tok, t3["id"], {"completed"}, 30) if t3 else None
    say(d3 is not None and "turn 3" in texts(d3), "5b context continued on A sees turns 1-2 (one written by B)", texts(d3).replace("\n", " | ")[:160])

    # 6. a context of another tenant is not readable: 404/403, no existence leak
    code, resp = send(B, "spike-othertenant", "and again?", context_id=ctx)
    t6 = (resp or {}).get("task") if isinstance(resp, dict) else None
    d6 = wait_for(B, "spike-othertenant", t6["id"], {"completed", "failed", "rejected"}, 20) if t6 else None
    leaked = d6 is not None and ("turn" in texts(d6))
    say(code in (403, 404) or (d6 is not None and state(d6) in {"failed", "rejected"} and not leaked),
        "6 other tenant cannot continue the context", f"HTTP {code}, state={state(d6) if d6 else '-'}, text={texts(d6)[:80] if d6 else ''}")

    # 7. cancel an in-flight tool loop from the other replica
    sent7 = time.time()
    code, resp = send(A, tok, "slow svc-y")
    t7 = (resp or {}).get("task") if isinstance(resp, dict) else None
    w = wait_for(B, tok, t7["id"], {"working"}, 10) if t7 else None
    say(w is not None and state(w) == "working", "7a slow task is working, seen via B", f"state={state(w) if w else '-'}")
    t0 = time.time()
    code, resp = http("POST", f"{B}/tasks/{t7['id']}:cancel", {}, tok) if t7 else (0, None)
    c = wait_for(A, tok, t7["id"], {"canceled"}, 10) if t7 else None
    say(code == 200 and c is not None and state(c) == "canceled", "7b cancel via B, canceled seen via A within 10s",
        f"HTTP {code}, state={state(c) if c else '-'}, after {time.time() - t0:.1f}s")
    # The fake slow tool runs 20s from the send. Wait past that window, so a
    # loop that ignored the cancel has had its chance to overwrite the state.
    time.sleep(max(0.0, 22 - (time.time() - sent7)))
    code, late = get(A, tok, t7["id"])
    say(state(late) == "canceled", "7c state still canceled after the slow tool would have finished", f"state={state(late)}")
    say("loop_stopped" in json.dumps(late.get("metadata") or {}) if isinstance(late, dict) else False,
        "7d executor recorded that the loop observed the cancel (metadata.loop_stopped)", json.dumps((late or {}).get("metadata"))[:120])

    # 8. duplicate messageId is one task, not two
    mid = str(uuid.uuid4())
    code1, r1 = send(A, tok, "status of svc-x", message_id=mid)
    code2, r2 = send(B, tok, "status of svc-x", message_id=mid)
    id1 = ((r1 or {}).get("task") or {}).get("id") if isinstance(r1, dict) else None
    id2 = ((r2 or {}).get("task") or {}).get("id") if isinstance(r2, dict) else None
    say(id1 and id2 and id1 == id2, "8 duplicate messageId across replicas returns the same task (idempotency)", f"{id1} vs {id2} (HTTP {code1}/{code2})")

    # 9. terminal task is not resumable
    msg = {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": "resume?"}], "taskId": tid, "contextId": ctx}
    code, resp = http("POST", f"{A}/message:send", {"message": msg, "configuration": {"returnImmediately": True}}, tok)
    say(code >= 400 or (isinstance(resp, dict) and resp.get("task", {}).get("id") != tid),
        "9 a message addressed to a completed task is refused or opens a new task", f"HTTP {code}, body={json.dumps(resp)[:120]}")

    print(f"\n{len(FAILS)} failed: {FAILS}" if FAILS else "\nall steps passed", flush=True)
    sys.exit(len(FAILS))


if __name__ == "__main__":
    main()
