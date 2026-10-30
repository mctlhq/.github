# mctl-support runtime spike (S-1, .github#249)

Three prototypes of one identical vertical slice, one per candidate runtime,
measured by `gate/gate.py` against two replicas on a shared Postgres. The
report is `../mctl-support-runtime.md`; the manifest is
`roadmap/spikes/mctl-support-runtime.yaml`.

| Candidate | Directory | A2A SDK | Anthropic SDK |
|---|---|---|---|
| Python | `python/` | `a2a-sdk` 1.2.3 | `anthropic` 2.3.0 |
| Go | `go/` | `github.com/a2aproject/a2a-go/v2` 2.7.0 | `anthropic-sdk-go` 1.84.0 |
| TypeScript (Node 24; Bun as a compatibility gate) | `typescript/` | `@a2a-js/sdk` 1.3.0 | `@anthropic-ai/sdk` 1.3.0 |

## The vertical slice (identical in every candidate)

- Agent card at `/.well-known/agent-card.json`; A2A 1.0 HTTP+JSON (REST)
  binding: `POST {base}/message:send`, `GET {base}/tasks/{id}`,
  `POST {base}/tasks/{id}:cancel`. `{base}` is whatever the SDK mounts
  (`/v1` or `/`); the candidate's README states it.
- Bearer auth in the SDK's own middleware/auth hook: the header is required;
  a token must start with `spike-`; the principal is the rest
  (`spike-namax66` → principal `namax66`). A missing or foreign token is
  refused with 401/403 **before** the executor runs. The executor must see
  the principal through the SDK's call context (not a global).
- Task store on Postgres through the SDK's own store (`DATABASE_URL`), one
  database per candidate. Two replicas (`PORT`, `REPLICA` env) share it.
- Tool loop on a **deterministic fake runner** (no network, no model): the
  loop has one tool, `get_service_status(service)`, returning
  `{service, status: "degraded", replicas: 1}`.
  - text `status of <svc>` → the fake model requests the tool once, then
    answers `"<svc> is degraded (1 replica); principal=<principal>; turn <n>"`,
    where `<n>` is the number of user turns in this context, counted from
    the transcript **restored from the store** (so turn 2 on replica B after
    turn 1 on replica A proves shared state).
  - text `and again?` → no tool call, answer `"turn <n>; principal=<principal>"`.
  - text `slow <svc>` → the tool sleeps 20 s but must honour cancellation;
    on cancel the loop stops within the turn and writes
    `metadata.loop_stopped = true` on the task; the task ends `canceled`.
- A context created under one principal is not continuable by another:
  refuse (403/404) without revealing whether it exists, or fail/reject the
  task without leaking the transcript.
- Duplicate `messageId` (same principal) returns the same task.
- A message addressed to a completed task is refused or opens a new task.
- OTel-style hook: one line per request `trace task=<id> principal=<p>` from
  the SDK's interceptor/middleware (the hook's existence and placement is
  what is measured, not a collector).

## Running the gate

```
docker run -d --name spike-support-pg -e POSTGRES_PASSWORD=spike -e POSTGRES_USER=spike -p 55432:5432 postgres:17-alpine
# per candidate: see its README for the two replica commands, then
python3 gate/gate.py --a http://127.0.0.1:8001/v1 --b http://127.0.0.1:8002/v1
```

## Real-Anthropic smoke (separate, never part of the gate)

Each candidate has `smoke_anthropic.*` using the official Anthropic SDK tool
runner of that runtime: the model asks for `get_service_status`, the fake
tool answers, the final text is printed. Runs only with `ANTHROPIC_API_KEY`
in the environment; the key is never written to disk or committed. An API
error there is recorded on the smoke row only and never affects the choice.
