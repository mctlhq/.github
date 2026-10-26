# Spike: mctl-support runtime (Python / Go / TypeScript)

Manifest: `roadmap/spikes/mctl-support-runtime.yaml` · Issue: mctlhq/.github#249 ·
Epic: `tenant-support-agent`, work item `s-1-runtime` (.github#243) ·
Prototypes and gate: `docs/spikes/mctl-support-runtime/`.

## Question

Which runtime gives mctl-support correct auth hooks, correct A2A task handling
across two replicas on a shared Postgres, a usable Anthropic tool loop and a
mature SDK, measured on one identical vertical slice per candidate, with Bun
checked only as a compatibility gate of the TypeScript prototype?

## Method

- One vertical slice per candidate (`README.md` in the prototype directory
  states it): agent card, A2A 1.0 HTTP+JSON `message:send` / `tasks/{id}` /
  `tasks/{id}:cancel`, bearer auth in the SDK's own hook, principal read from
  the SDK call context, task store on Postgres through the SDK's own store, a
  deterministic fake tool loop with one tool, cancel of an in-flight loop from
  the other replica, context continuation across replicas, other-tenant
  isolation, duplicate-message idempotency, terminal-task rule.
- Gate: `gate/gate.py` (standard library only), run against two replicas of
  each candidate sharing one `postgres:17-alpine`. Output recorded verbatim.
- The gate is offline: no model, no network beyond the two replicas. The real
  Anthropic call is a separate smoke per candidate (`smoke_anthropic.*`) and
  never affects the choice.
- Weights (owner, 2026-10-26): security/auth hooks 35 %, A2A task +
  multi-replica correctness 30 %, tool-loop ergonomics 15 %, SDK maturity /
  upstream risk 10 %, runtime footprint 10 %. HTTP throughput is not a
  criterion. Owner's prior: Python 45 / Go 35 / TS 20.
- Bun: the TypeScript prototype must pass on Node 24 and then run unchanged on
  Bun 1.3.x; any patch, polyfill or Node-only workaround rejects Bun.
- Each candidate was built and measured by a separate agent with the same
  brief; the per-candidate `README.md` carries the SDK hook table with
  file:line citations into the installed SDK, the verbatim gate output, the
  measurements and the gaps. This report condenses them.

## Pinned versions (resolved 2026-10-26, not quoted)

| Runtime | A2A SDK | Anthropic SDK | Toolchain |
|---|---|---|---|
| Python | `a2a-sdk` 1.2.3 | `anthropic` 2.3.0 | Python 3.12 (image), 3.10 (local) |
| Go | `a2a-go/v2` 2.7.0 | `anthropic-sdk-go` 1.84.0 | Go 1.27 |
| TypeScript | `@a2a-js/sdk` 1.3.0 | `@anthropic-ai/sdk` 1.3.0 | Node 24 (image), Bun 1.3.14 (gate) |

## Results

All three candidates pass the gate 17/17 (exit 0). The difference is in what
the SDK carried and what had to be hand-written to get there.

### Summary table

| Metric | Python | Go | TypeScript (Node 24) |
|---|---|---|---|
| Gate | 17/17 | 17/17 | 17/17 (Bun: 17/17 unchanged) |
| LOC of the prototype | 552 | 768 (267 of them a Postgres task store) | 525 |
| Direct deps / installed | 3 / 54 packages | 3 / 84 modules in graph | 5 / 97 packages |
| RSS per replica after the gate | 18 MB | 14-15 MB | 78-81 MB (Bun: 71-77 MB) |
| Docker image (non-root) | 397 MB (`python:3.12-slim`) | 21.3 MB (distroless static) | 398 MB (`node:24-slim`) |
| Cold start to first 200 on the card | 2.18 s | 0.35-0.41 s | 1.00-1.18 s |
| Cross-replica cancel visible after | 0.6 s | 0.0 s | 0.0 s |
| Foreign-tenant `contextId` | task `rejected` by executor policy (ours) | HTTP 404 (guard ours, masking SDK) | HTTP 404 (ours, on SDK owner-scoped list) |
| SDK ships a SQL task store | yes (`a2a.server.cluster`, versioned, CAS) | **no** (hand-written `pgstore.go`) | yes (`DatabaseTaskStore`, Kysely/pg) |
| Auth hook in the SDK | no; Starlette middleware, SDK adapts `request.user` | yes (`CallInterceptor.Before`, sentinel errors map to 401/403/404) | yes (`UserBuilder` on `restHandler`, throw before any handler) |
| Server-side interceptor for tracing | no (ASGI middleware) | yes (`CallInterceptor.After`) | no (Express middleware) |
| Cross-replica cancel of an in-flight loop | SDK forces CANCELED into the store; cooperative stop is ours (marker event + stream poll) | SDK marks the store; executor polls the store every 250 ms (ours) | SDK marks the store; executor polls every 500 ms (ours) |
| `messageId` idempotency in the SDK | no (ours: `uuid5(owner, messageId)`) | no (ours: index + interceptor short-circuit) | no (ours: table) |
| SDK releases (last six) | 1.1.4 → 1.2.3, 2026-09-08 → 10-19 | 2.3.1 → 2.7.0, 2026-05-13 → 10-21 | 1.0.0 → 1.3.0, 2026-07-22 → 09-29 |
| Open issues (issues only) | 86 incl. PRs | 13 (36 incl. PRs) | 21 (55 incl. PRs) |

### Python

Gate output, verbatim (replicas on 8001/8002, database `spike_python`):

```
PASS  1 agent card served  -- HTTP 200, name='mctl-support (python spike)', interfaces=['HTTP+JSON']
PASS  2a message:send without bearer refused  -- HTTP 401
PASS  2b message:send with foreign bearer refused  -- HTTP 401
PASS  3 send to A returns a task  -- HTTP 200, state=submitted, id='082bcb03-8c3e-50ce-99d5-9619bdec9871'
PASS  4a task completes, read via B  -- state=completed
PASS  4b answer carries the fake tool's evidence  -- svc-x is degraded (1 replica); principal=namax66; turn 1
PASS  4c executor saw the bearer's principal
PASS  4d contextId stable across replicas  -- ctx=c9109499-7330-4eab-993c-36020a5526ba
PASS  5a context continued on B sees turn 1 (written by A)  -- turn 2; principal=namax66
PASS  5b context continued on A sees turns 1-2 (one written by B)  -- turn 3; principal=namax66
PASS  6 other tenant cannot continue the context  -- HTTP 200, state=rejected, text=unknown context
PASS  7a slow task is working, seen via B  -- state=working
PASS  7b cancel via B, canceled seen via A within 10s  -- HTTP 200, state=canceled, after 0.6s
PASS  7c state still canceled after the slow tool would have finished  -- state=canceled
PASS  7d executor recorded that the loop observed the cancel (metadata.loop_stopped)  -- {"stopped_on": "a", "loop_stopped": true, "reason": "remote-cancel-requested"}
PASS  8 duplicate messageId across replicas returns the same task (idempotency)  -- 266f1e01-c908-578e-8b15-b3636878a28b vs 266f1e01-c908-578e-8b15-b3636878a28b (HTTP 200/200)
PASS  9 a message addressed to a completed task is refused or opens a new task  -- HTTP 400, body={"error": {"code": 400, "status": "FAILED_PRECONDITION", "message": "Task 082bcb03-8c3e-50ce-99d5-9619bdec9871 is in ter

all steps passed
```

What the SDK carries: the whole multi-replica story. `a2a.server.cluster`
(831 lines) gives a versioned Postgres task store with compare-and-swap on a
`task_versions` side table and an append-only `task_events` log that the other
replica polls (0.25 s here). Every store read, list and write is scoped by an
owner resolved from the call context, so the tenant boundary is enforced in
the store, not in handler code. The principal reaches the executor through
`ServerCallContext`, no globals. The request handler is `@trace_class`, so
OTel spans exist once an SDK is installed.

What had to be written (about 150 lines of policy): the bearer backend (the
SDK has no auth hook, it adapts whatever Starlette puts in `request.user`);
the per-request trace line (no server-side interceptor, only a client one);
`messageId` idempotency; the "unknown context" rejection for a foreign
principal naming someone else's `contextId` (the SDK would silently open a
new owner-scoped task in it); and the cooperative cancel. The SDK's
cross-replica cancel is a forced store write that a loop sitting in a 20 s
tool call never sees, and the owner's eventual `complete()` then dies with
`ConcurrentTaskModificationError`, so the executor polls the event stream
itself. Two more findings from reading the SDK: `DatabaseTaskEventStream.publish`
is a no-op (events persist only through `store.save(event=...)`), and the V2
handler explicitly does not stream across replicas (polling `GET /tasks/{id}`
works from any replica; `message:stream` does not). Cluster mode is documented
only in code, not in the package README. Two sends on one `contextId` on two
replicas can both count the same turn, because the store serialises per task,
not per context.

Footprint: 397 MB image, 18 MB RSS, 2.2 s cold start, 54 packages including
protobuf, SQLAlchemy, asyncpg and pydantic. Cadence: six releases in six
weeks, which is good for fixes and a risk for API stability.

### Go

Gate output, verbatim (replicas on 8011/8012, database `spike_go`):

```
PASS  1 agent card served  -- HTTP 200, name='mctl-support (go spike)', interfaces=['HTTP+JSON']
PASS  2a message:send without bearer refused  -- HTTP 401
PASS  2b message:send with foreign bearer refused  -- HTTP 403
PASS  3 send to A returns a task  -- HTTP 200, state=submitted, id='01a17a60-ea77-7cdc-943a-38d52130807e'
PASS  4a task completes, read via B  -- state=completed
PASS  4b answer carries the fake tool's evidence  -- svc-x is degraded (1 replica); principal=namax66; turn 1
PASS  4c executor saw the bearer's principal
PASS  4d contextId stable across replicas  -- ctx=01a17a60-ea77-7e92-b573-2260872275f6
PASS  5a context continued on B sees turn 1 (written by A)  -- turn 2; principal=namax66
PASS  5b context continued on A sees turns 1-2 (one written by B)  -- turn 3; principal=namax66
PASS  6 other tenant cannot continue the context  -- HTTP 404, state=-, text=
PASS  7a slow task is working, seen via B  -- state=working
PASS  7b cancel via B, canceled seen via A within 10s  -- HTTP 200, state=canceled, after 0.0s
PASS  7c state still canceled after the slow tool would have finished  -- state=canceled
PASS  7d executor recorded that the loop observed the cancel (metadata.loop_stopped)  -- {"loop_stopped": true}
PASS  8 duplicate messageId across replicas returns the same task (idempotency)  -- 01a17a61-21fd-78fc-9c82-11b8d0f2d32a vs 01a17a61-21fd-78fc-9c82-11b8d0f2d32a (HTTP 200/200)
PASS  9 a message addressed to a completed task is refused or opens a new task  -- HTTP 400, body={"error": {"code": 400, "status": "FAILED_PRECONDITION", "message": "executor setup failed: failed to load exec ctx: tas

all steps passed
```

What the SDK carries: the cleanest request edge of the three. `a2asrv.CallInterceptor`
runs before the executor; returning the sentinel errors `a2a.ErrUnauthenticated`,
`ErrUnauthorized` or `ErrTaskNotFound` maps to 401/403/404 inside the REST
layer, and `After` is a real server-side hook for the trace line. The principal
reaches the executor as `ExecutorContext.User`. The task store contract is
per-user (`NewTaskStoreAuthenticator`), and an interceptor can short-circuit a
request with an early response, which is how `messageId` idempotency was done.

What had to be written (about 400 of 768 lines): **the SDK ships no SQL task
store at all** in v2.7.0 (`a2asrv/taskstore/` has only the in-memory store; no
`database/sql` import anywhere in the module). `pgstore.go` is a 267-line port
of the in-memory store's contract, and the SDK's store conformance tests are
package-internal, so it cannot be proven against them. The SDK cancels the
executor context only for a local run; the cross-replica stop is a 250 ms
store poll in the executor. A terminal task rejects all further events, so
`metadata.loop_stopped` is written with a direct `jsonb_set`, the one write
outside the SDK contract. `WithClusterMode` exists but is marked experimental
and ships only in-memory queue adapters; a broker must be user-supplied.
Client-supplied `contextId` is accepted as-is, so the context ownership guard
is hand-written (task ownership masking is the SDK's). The REST surface moved
in 2.7.0 (`a2a+json` content type, `A2A-Version` rejection), so minor
releases are not drop-in.

Footprint: 21.3 MB distroless non-root image, 16.8 MB static binary, 14-15 MB
RSS, 0.4 s cold start, three direct dependencies, no interpreter. This is the
smallest attack and supply-chain surface of the three by a wide margin.
Cadence: monthly minors, 13 open issues, last push on the day of measurement.

### TypeScript (Node 24) and the Bun gate

Environment note: port 8021 on the measuring machine was held by a foreign
loopback listener (EADDRINUSE on bind, invisible to `lsof`), so replica A ran
on 8023 and B on 8022 for both runs. Nothing else deviates from the slice.

Gate output on Node 24.22.1, verbatim (database `spike_ts`):

```
PASS  1 agent card served  -- HTTP 200, name='mctl-support (spike, typescript)', interfaces=['HTTP+JSON']
PASS  2a message:send without bearer refused  -- HTTP 401
PASS  2b message:send with foreign bearer refused  -- HTTP 401
PASS  3 send to A returns a task  -- HTTP 200, state=submitted, id='aa9bc481-01b6-469c-890f-9951218a9b86'
PASS  4a task completes, read via B  -- state=completed
PASS  4b answer carries the fake tool's evidence  -- svc-x is degraded (1 replica); principal=namax66; turn 1
PASS  4c executor saw the bearer's principal
PASS  4d contextId stable across replicas  -- ctx=d7474586-86ca-49fe-b6a9-f71c6e7dc6a9
PASS  5a context continued on B sees turn 1 (written by A)  -- turn 2; principal=namax66
PASS  5b context continued on A sees turns 1-2 (one written by B)  -- turn 3; principal=namax66
PASS  6 other tenant cannot continue the context  -- HTTP 404, state=-, text=
PASS  7a slow task is working, seen via B  -- state=working
PASS  7b cancel via B, canceled seen via A within 10s  -- HTTP 200, state=canceled, after 0.0s
PASS  7c state still canceled after the slow tool would have finished  -- state=canceled
PASS  7d executor recorded that the loop observed the cancel (metadata.loop_stopped)  -- {"replica": "a", "loop_stopped": true, "stopped_on": "a"}
PASS  8 duplicate messageId across replicas returns the same task (idempotency)  -- 191e968d-c689-4937-a5c3-9dabaa9f0858 vs 191e968d-c689-4937-a5c3-9dabaa9f0858 (HTTP 200/200)
PASS  9 a message addressed to a completed task is refused or opens a new task  -- HTTP 400, body={"error": {"code": 400, "status": "FAILED_PRECONDITION", "message": "Task aa9bc481-01b6-469c-890f-9951218a9b86 is in a t

all steps passed
```

Gate output on Bun 1.3.14, verbatim (`bun run src/server.ts`, zero source
changes, no flags, no polyfills, no `bunfig.toml`):

```
PASS  1 agent card served  -- HTTP 200, name='mctl-support (spike, typescript)', interfaces=['HTTP+JSON']
PASS  2a message:send without bearer refused  -- HTTP 401
PASS  2b message:send with foreign bearer refused  -- HTTP 401
PASS  3 send to A returns a task  -- HTTP 200, state=submitted, id='86292661-6cef-498a-845e-d7e162976290'
PASS  4a task completes, read via B  -- state=completed
PASS  4b answer carries the fake tool's evidence  -- svc-x is degraded (1 replica); principal=namax66; turn 1
PASS  4c executor saw the bearer's principal
PASS  4d contextId stable across replicas  -- ctx=05ddec56-2da6-46cd-a23d-d79006ba0aa4
PASS  5a context continued on B sees turn 1 (written by A)  -- turn 2; principal=namax66
PASS  5b context continued on A sees turns 1-2 (one written by B)  -- turn 3; principal=namax66
PASS  6 other tenant cannot continue the context  -- HTTP 404, state=-, text=
PASS  7a slow task is working, seen via B  -- state=working
PASS  7b cancel via B, canceled seen via A within 10s  -- HTTP 200, state=canceled, after 0.0s
PASS  7c state still canceled after the slow tool would have finished  -- state=canceled
PASS  7d executor recorded that the loop observed the cancel (metadata.loop_stopped)  -- {"replica": "a", "loop_stopped": true, "stopped_on": "a"}
PASS  8 duplicate messageId across replicas returns the same task (idempotency)  -- 892fe7e0-f421-4793-a25c-93e5b7d4902d vs 892fe7e0-f421-4793-a25c-93e5b7d4902d (HTTP 200/200)
PASS  9 a message addressed to a completed task is refused or opens a new task  -- HTTP 400, body={"error": {"code": 400, "status": "FAILED_PRECONDITION", "message": "Task 86292661-6cef-498a-845e-d7e162976290 is in a t

all steps passed
```

**Bun verdict: passes unchanged.** The `a2a-db` migration subprocess also ran
under Bun (`process.execPath`), and Express 5, `pg` 8 and Kysely worked on the
compatibility layer. Not tested on Bun: the Docker image (Node-only) and the
Anthropic smoke.

What the SDK carries: a pre-handler auth hook (`UserBuilder` on `restHandler`;
a thrown error becomes the HTTP response before any handler runs) and an
owner-scoped Postgres store (`DatabaseTaskStore` over Kysely/pg, load and list
scoped by tenant and owner). The principal reaches the executor through a
typed `ServerCallContext`.

What had to be written (about 130 lines): the trace line (no server-side
interceptor in the Express binding; `CallInterceptor` is client-only), the
cross-replica stop (the SDK writes `canceled` to the store but never notifies
the executing replica; 500 ms store poll aborts the tool's `AbortSignal`),
`messageId` idempotency, and the 404 for a foreign principal on top of the
SDK's owner-scoped list. Rough edges: 401 needs an `A2AError` subclass with
`transport='rest'` and `statusCode=401` (no `UnauthorizedError` export); the
schema migration is CLI-only and runs as a subprocess at boot;
`ServerCallContext.tenant` exists but nothing sets it in the Express binding;
`AgentExecutor.cancelTask` receives no call context; protobuf-generated types
make fixtures verbose.

Footprint: 398 MB image, 78-81 MB RSS per replica, 1.0-1.2 s cold start, 97
packages. Cadence: the 1.x API is three months old with six releases in ten
weeks.

## Scoring

Scores are 0-10 per criterion, set by comparing the three per-candidate
findings above; the weighted column is the owner's weights applied and
divided by 10.

| Criterion | Weight | Python | Go | TypeScript |
|---|---|---|---|---|
| Security / auth hooks | 35 | 7.5 (owner scoping enforced in the SDK store; auth itself is framework middleware; foreign-context refusal is our policy) | 7.5 (SDK interceptor with sentinel errors before the executor; but the per-user store is our code) | 8 (SDK auth hook and SDK owner-scoped store; 404 on top is 7 lines) |
| A2A task + multi-replica correctness | 30 | 7.5 (SDK cluster store with CAS and event log; forced cancel, publish no-op, no cross-replica streaming, per-context turn race) | 5.5 (no SQL store in the SDK; store, ownership guard, idempotency and cross-replica stop all ours; cluster mode experimental without a broker) | 7 (SDK Postgres store; cancel never reaches the executing replica, poll is ours; no idempotency) |
| Tool-loop ergonomics | 15 | 8 (`beta.messages.tool_runner`, plain `while` over a one-method fake model, in-memory SDK fakes for executor tests) | 7.5 (`toolrunner` typed, fakes are interfaces; more boilerplate) | 7 (`toolRunner`, but protobuf-shaped parts make fixtures verbose) |
| SDK maturity / upstream risk | 10 | 6.5 (cluster mode documented only in code; 6 releases in 6 weeks; largest issue count) | 7 (monthly minors, smallest issue count, active; REST surface still moving between minors) | 6 (1.x three months old, 6 releases in 10 weeks) |
| Runtime footprint | 10 | 6 (397 MB, 18 MB RSS, 2.2 s) | 10 (21 MB distroless, 15 MB RSS, 0.4 s, 3 deps) | 5 (398 MB, 80 MB RSS, 1.1 s, 97 packages) |
| **Weighted** | 100 | **73.3** | **71.0** | **70.5** |

The spread is under three points, inside the noise of a 0-10 scale. Two
sensitivities decide it: Python wins because the SDK owns the shared-state
problem (store, versioning, event log) that the other two leave to the
service; Go would win if footprint or supply-chain surface weighed 20 %
instead of 10 %, or if the team is ready to own and prove a Postgres task
store. TypeScript does not lead on any weighted criterion despite the cleanest
auth hook, and Bun adds nothing to the score: it is a confirmed option, not a
reason.

## Real-Anthropic smoke

PASS ×3, run 2026-10-26 on `claude-sonnet-5-5` with a key from the dedicated
spike workspace of the Anthropic Console organisation (key held in the
operator's keychain, injected through the environment, never written to the
tree). Each runtime's official tool runner asked for `get_service_status`,
received the fake tool's answer and ended its turn with "svc-x is degraded,
1 replica":

| Runtime | Runner | Result |
|---|---|---|
| Python | `client.beta.messages.tool_runner` | PASS: one `tool_use` round, then `end_turn` |
| Go | `toolrunner` loop | PASS: 2 iterations, `tool_use` then `end_turn` |
| TypeScript (Node 24) | `client.beta.messages.toolRunner` | PASS: `tool_use` then `end_turn`; not run on Bun |

Spend was a handful of short Sonnet calls. The row is informational and does
not change the recommendation.

### Addendum: the same loop on Hetzner inference (Qwen3.6-35B-A3B-FP8)

Asked by the owner after mctl-agents#791 recorded `ADOPT_EXPERIMENTAL_PROVIDER`
for Hetzner's Anthropic-compatible endpoint. Same three tool loops, same fake
tool, `ANTHROPIC_BASE_URL=https://inference.hetzner.com/api`, Bearer auth via
`ANTHROPIC_AUTH_TOKEN` (the endpoint ignores `x-api-key`), model
`Qwen/Qwen3.6-35B-A3B-FP8`. Informational only; the runtime choice does not
depend on it.

| Runtime | Result | Note |
|---|---|---|
| Go `toolrunner` | PASS unchanged: 2 iterations, "The status of svc-x is currently degraded." | sends the full transcript each turn |
| Python `beta.messages.tool_runner` | PASS with `thread=False`: `tool_use`, then `end_turn` "svc-x is currently in a degraded status and is running with 1 replica" | default fails with Hetzner 400 "No user query found in messages" |
| TypeScript `beta.messages.toolRunner` | PASS with `thread: false`: `tool_use`, then `end_turn` "svc-x is currently in a degraded state with 1 replica" | same 400 by default |

Finding worth carrying into S1b: `anthropic` 2.3.0 and `@anthropic-ai/sdk`
1.3.0 run their tool loops over a **server-side message thread** by default
(`thread: {type: create}` on the first turn, then `continue` with
`previous_message_id` and only the new `tool_result`). Only Anthropic's own
API holds that state; a compatible endpoint sees a second request with no
user message and refuses it. The Go SDK 1.84.0 runner has no thread mode and
resends the history. So a provider switch for the support agent is a
one-line `thread=False` in Python or TypeScript, and the smoke scripts now
accept `ANTHROPIC_AUTH_TOKEN` next to `ANTHROPIC_API_KEY` for that reason.
In S1b that switch belongs to a provider capability (`server_thread:
true|false` next to the base URL and model in the provider config), not to
a condition on the provider's name, so Anthropic, Hetzner or any other
Messages-compatible backend swap cleanly. S1 still ships with Anthropic as
the production provider; whether Hetzner is allowed for a tenant-facing
agent at all is a separate decision gated on mctl-agents#824 (provider in
model policy) and #826 (usage accounting), as #791's verdict states, and
the support MVP does not take on multi-provider logic for it.

## Recommendation

**GO_PYTHON**, by a narrow margin, with Go as the named runner-up.

- Python's `a2a-sdk` is the only one of the three that carries the
  multi-replica task store itself (versioned, with CAS and an event log).
  That store is about the consistency of A2A tasks across replicas, the
  30 % criterion, and it is the part where hand-written code is hardest to
  prove; tenant isolation is not what it buys. Isolation of the support
  agent is enforced independently by mctl-api's `BoundTenant` support
  authority (S0a, mctl-api#811) on every read the agent makes, whatever
  runtime the agent runs on. The hand-written parts that remain in Python
  (bearer backend, trace middleware, idempotency, foreign context rejection,
  cooperative cancel) are about 150 lines of policy, each a few lines and
  testable offline. The prototype's cancel path and turn counting show the
  two SDK behaviours S1a must design around: a cancel from another replica
  is a forced store write, so the loop must watch the event stream; and
  per-context writes are not serialised, so transcript counting needs its
  own lock or a per-context version.
- The footprint gap (397 MB against 21 MB, 2.2 s against 0.4 s) is real
  but not operational for this service: two Python replicas are well under
  a gigabyte and mctl-support is not a service that scales to many replicas.
  It becomes a reason to revisit only if that changes.

Suggested wording for the `decision` block, for the owner to adopt or edit:

> GO_PYTHON. Python wins narrowly because the official A2A SDK provides the
> strongest built-in persistent multi-replica task semantics (versioning,
> CAS, event history), reducing correctness-critical custom code in the
> support service. Go remains the preferred fallback if Python's runtime
> footprint becomes operationally significant. Tenant isolation remains
> enforced independently by mctl-api's BoundTenant support authority.
- Go is the better runtime on every operational axis (21 MB image, 15 MB RSS,
  0.4 s start, three direct dependencies) and has the cleanest auth edge, but
  the SDK ships no SQL store and its cluster mode is experimental without a
  broker. Choosing Go means mctl-support owns a Postgres task store and the
  cross-replica semantics, roughly 400 lines that must be proven both ways
  without the SDK's own conformance suite. That is a reasonable choice if the
  owner weighs footprint higher than the stated 10 %, and the prototype in
  `go/` is a working starting point.
- TypeScript is a credible third, not a recommendation: it leads on no
  weighted criterion, its SDK is the youngest, and its footprint is the
  largest. Bun runs it unchanged, which is recorded as a fact for any future
  TypeScript decision in the org, not as an input to this one.

Pins for S1a if the verdict is GO_PYTHON: `a2a-sdk[http-server,sql,postgresql]==1.2.3`,
`anthropic==2.3.0`, Python 3.12, `python:3.12-slim` non-root image. Re-verify
the cluster package on each SDK minor; it is documented only in code.

## Verdict

Recorded by the owner in the manifest `decision` block and on #249.
