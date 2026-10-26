# TypeScript candidate (Node 24, Bun as a compatibility gate)

`@a2a-js/sdk` 1.3.0 (Express REST binding + Postgres task store) and
`@anthropic-ai/sdk` 1.3.0. Contract: `../README.md`; scoring: `../gate/gate.py`.

## Run

Postgres as in `../README.md` (`spike-support-pg` on `127.0.0.1:55432`); this
candidate uses database `spike_ts`. Each replica runs the SDK's `a2a-db upgrade`
migration on start (shared lock, safe from both at once).

```
npm ci
npx tsc -p tsconfig.json --noEmit                       # typecheck (passes)

# Node 24 (tsx loader; the Dockerfile compiles with tsc and runs node dist/server.js)
PORT=8021 REPLICA=a node --import ./node_modules/tsx/dist/loader.mjs src/server.ts
PORT=8022 REPLICA=b node --import ./node_modules/tsx/dist/loader.mjs src/server.ts

# Bun 1.3.14, same source, no changes
PORT=8021 REPLICA=a bun run src/server.ts
PORT=8022 REPLICA=b bun run src/server.ts

python3 ../gate/gate.py --a http://127.0.0.1:8021/v1 --b http://127.0.0.1:8022/v1
```

`DATABASE_URL` overrides the default `postgres://spike:spike@127.0.0.1:55432/spike_ts`.

Note on the measurement run: on the measuring machine port 8021 was held by a
foreign listener (loopback-only, invisible to `lsof`, `EADDRINUSE` for any
new bind, resets every connection). Replica A was therefore run on **8023** and
B on 8022 for both gate runs below; nothing else differs from the commands above.

**REST base**: `/v1` (we mount `restHandler` there; the SDK mounts wherever
`app.use()` puts it). Agent card at `/.well-known/agent-card.json`.

## SDK hooks used (export names, verified against the installed `.d.ts`)

| Concern | SDK export | Where declared (under `node_modules/@a2a-js/sdk/dist/`) | SDK-given or hand-written |
|---|---|---|---|
| Transport, routing, `message:send` / `tasks/{id}` / `tasks/{id}:cancel` | `restHandler` from `@a2a-js/sdk/server/express` | `server/express/index.d.ts:71` | SDK |
| Agent card | `agentCardHandler` from `@a2a-js/sdk/server/express` | `server/express/index.d.ts:2` | SDK (card object hand-written, `src/server.ts:191`) |
| **Auth** | `UserBuilder` option of `restHandler` (`(req) => Promise<User>`); a throw becomes the HTTP error before any handler method runs | `agent_card_handler-DdsMrp1N.d.ts:5`, option at `server/express/index.d.ts:42` | SDK hook; bearer parsing hand-written (`src/server.ts:58-67`). The 401 status comes from subclassing `A2AError` with `transport='rest'`, `statusCode=401` (`src/server.ts:47-55`), which the SDK's `restStatusFor` (`errors/index.d.ts:81`) honours |
| **Call context** | `ServerCallContext` (`.user`, `.tenant`, `.state`), `ServerCallContextBuilder` + `defaultServerCallContextBuilder` from `@a2a-js/sdk/server` | `context-CI8-TqEC.d.ts:45,90`; option at `server/express/index.d.ts:55` | SDK; the executor reads `ctx.context.user.userName` (`src/agent.ts:114`), no global |
| **Middleware / trace** | none server-side (`CallInterceptor` is client-only: `client/index.d.ts:3`) | - | Hand-written Express middleware mounted ahead of the SDK router (`src/server.ts:81-98`); prints `trace task=<id> principal=<p> ...` per request |
| **Task store** | `DatabaseTaskStore` (Kysely over `pg`) from `@a2a-js/sdk/server/database`; schema via the `a2a-db` CLI | `server/database/index.d.ts:79`; `load`/`list` scoped by `(tenant, owner)` where owner defaults to `context.user.userName` (`server/database/index.js:3,691,940`) | SDK; migration invoked as a subprocess (`src/server.ts:104-105`) because the CLI has no function export |
| **Cross-replica cancel** | `DefaultRequestHandler.cancelTask` (`server/index.d.ts:362`): if the task's event bus is local it calls `AgentExecutor.cancelTask` (`server/index.d.ts:144`); otherwise it writes `canceled` straight to the store (`server/index.js:3209-3262`) | | SDK for the state write; **hand-written** store poll in the executor so the in-flight tool loop on the other replica observes it (`src/agent.ts:142-152`) and writes `metadata.loop_stopped` (`src/agent.ts:184-191`) |
| Request handler | `DefaultRequestHandler`, `DefaultExecutionEventBusManager`, `AgentEvent`, `AgentExecutor`, `RequestContext`, `ExecutionEventBus` | `server/index.d.ts:301,233,127` | SDK |
| messageId idempotency | none | - | Hand-written table `spike_message_tasks` + reserve/lookup in a `DefaultRequestHandler` subclass (`src/server.ts:153-186`) |
| Context ownership (other principal cannot continue) | store `list` is owner-scoped, so a foreign and a nonexistent context both yield an empty page | `server/database/index.js:940` | SDK scoping, hand-written refusal as `TaskNotFoundError` -> 404 (`src/server.ts:145-151`) |
| Completed task not resumable | `DefaultRequestHandler.sendMessage` rejects a terminal `taskId` with 400 `FAILED_PRECONDITION` | `server/index.js` | SDK |
| Anthropic tool loop (smoke only) | `client.beta.messages.toolRunner` + `betaTool` | `src/smoke_anthropic.ts` | SDK |

## Measurements

### Gate, Node 24.22.1 (verbatim, re-run 2026-10-27 with the fixed step 7c; A on 8023, B on 8022, see note above)

```
PASS  1 agent card served  -- HTTP 200, name='mctl-support (spike, typescript)', interfaces=['HTTP+JSON']
PASS  2a message:send without bearer refused  -- HTTP 401
PASS  2b message:send with foreign bearer refused  -- HTTP 401
PASS  3 send to A returns a task  -- HTTP 200, state=submitted, id='476958ef-b7dd-40c4-9487-5d3c7e13426e'
PASS  4a task completes, read via B  -- state=completed
PASS  4b answer carries the fake tool's evidence  -- svc-x is degraded (1 replica); principal=namax66; turn 1
PASS  4c executor saw the bearer's principal
PASS  4d contextId stable across replicas  -- ctx=83eaa2d8-a98b-4dba-9c0a-1a4da0d2caaf
PASS  5a context continued on B sees turn 1 (written by A)  -- turn 2; principal=namax66
PASS  5b context continued on A sees turns 1-2 (one written by B)  -- turn 3; principal=namax66
PASS  6 other tenant cannot continue the context  -- HTTP 404, state=-, text=
PASS  7a slow task is working, seen via B  -- state=working
PASS  7b cancel via B, canceled seen via A within 10s  -- HTTP 200, state=canceled, after 0.0s
PASS  7c state still canceled after the slow tool would have finished  -- state=canceled
PASS  7d executor recorded that the loop observed the cancel (metadata.loop_stopped)  -- {"replica": "a", "loop_stopped": true, "stopped_on": "a"}
PASS  8 duplicate messageId across replicas returns the same task (idempotency)  -- db21bb65-9ebd-4364-acb2-d2c5e1b342e3 vs db21bb65-9ebd-4364-acb2-d2c5e1b342e3 (HTTP 200/200)
PASS  9 a message addressed to a completed task is refused or opens a new task  -- HTTP 400, body={"error": {"code": 400, "status": "FAILED_PRECONDITION", "message": "Task 476958ef-b7dd-40c4-9487-5d3c7e13426e is in a t

all steps passed
```

### Gate, Bun 1.3.14 (verbatim, re-run 2026-10-27; identical source, `bun run src/server.ts`)

```
PASS  1 agent card served  -- HTTP 200, name='mctl-support (spike, typescript)', interfaces=['HTTP+JSON']
PASS  2a message:send without bearer refused  -- HTTP 401
PASS  2b message:send with foreign bearer refused  -- HTTP 401
PASS  3 send to A returns a task  -- HTTP 200, state=submitted, id='1b554f47-e99b-4a00-abdd-96064a22408a'
PASS  4a task completes, read via B  -- state=completed
PASS  4b answer carries the fake tool's evidence  -- svc-x is degraded (1 replica); principal=namax66; turn 1
PASS  4c executor saw the bearer's principal
PASS  4d contextId stable across replicas  -- ctx=979c96a4-ba8b-44ea-af5a-0cee17fc4c01
PASS  5a context continued on B sees turn 1 (written by A)  -- turn 2; principal=namax66
PASS  5b context continued on A sees turns 1-2 (one written by B)  -- turn 3; principal=namax66
PASS  6 other tenant cannot continue the context  -- HTTP 404, state=-, text=
PASS  7a slow task is working, seen via B  -- state=working
PASS  7b cancel via B, canceled seen via A within 10s  -- HTTP 200, state=canceled, after 0.0s
PASS  7c state still canceled after the slow tool would have finished  -- state=canceled
PASS  7d executor recorded that the loop observed the cancel (metadata.loop_stopped)  -- {"replica": "a", "loop_stopped": true, "stopped_on": "a"}
PASS  8 duplicate messageId across replicas returns the same task (idempotency)  -- 94529512-5ea5-4c92-9fa7-f6c90de94687 vs 94529512-5ea5-4c92-9fa7-f6c90de94687 (HTTP 200/200)
PASS  9 a message addressed to a completed task is refused or opens a new task  -- HTTP 400, body={"error": {"code": 400, "status": "FAILED_PRECONDITION", "message": "Task 1b554f47-e99b-4a00-abdd-96064a22408a is in a t

all steps passed
```

### Numbers

| Metric | Value |
|---|---|
| LOC (`wc -l src/*.ts`) | agent.ts 225, server.ts 246, smoke_anthropic.ts 54, **525 total** |
| Direct deps | 5 runtime (`@a2a-js/sdk`, `@anthropic-ai/sdk`, `express`, `kysely`, `pg`) + 5 dev (`typescript`, `tsx`, `@types/*`) |
| `ls node_modules \| wc -l` | 97 top-level entries (17 of them scoped packages under `@*/`) |
| RSS replica A after the gate, Node 24 (tsx loader) | 80,736 KB (B: 78,016 KB) |
| RSS replica A after the gate, Bun 1.3.14 | 77,232 KB (B: 71,360 KB) |
| Docker image (`node:24-slim`, multi-stage, non-root, prod deps only) | **398 MB** |
| Cold start `docker run` -> first 200 on agent card (incl. `a2a-db upgrade` against host Postgres), 3 runs | 1.18 s, 1.00 s, 1.15 s |
| Typecheck | `tsc --noEmit` clean |

### SDK maturity (`a2aproject/a2a-js`, read 2026-10-26)

```
$ gh api repos/a2aproject/a2a-js/releases --jq '.[0:6][]|.tag_name+" "+.published_at'
v1.3.0 2026-09-29T10:40:16Z
v1.2.1 2026-09-24T07:31:59Z
v1.2.0 2026-09-18T08:28:17Z
v1.1.0 2026-08-26T08:31:03Z
v1.0.1 2026-07-28T10:40:21Z
v1.0.0 2026-07-22T15:03:42Z
$ gh api repos/a2aproject/a2a-js --jq '.open_issues_count'
55        (issues + PRs; open issues alone: 21)
```

Six releases in ten weeks since 1.0.0; the REST binding, `DatabaseTaskStore`
owner scoping and `ServerCallContext.tenant` are all 1.x additions.

## Multi-replica story

- **Shared state**: every task read/write goes through `DatabaseTaskStore`
  scoped by `(tenant, owner)`; the turn counter is rebuilt from the store on
  each request (`src/agent.ts:92-110`), which is why turn 2 on B after turn 1
  on A passes (5a/5b).
- **Cancel across replicas**: `DefaultRequestHandler.cancelTask`
  (`node_modules/@a2a-js/sdk/dist/server/index.js:3209-3262`) only reaches the
  executor when the task's event bus lives in the same process
  (`eventBusManager.getByTaskId`, line 3223). On the other replica it writes
  `canceled` to the store directly (lines 3232-3262) and returns, which is the
  "after 0.0s" in 7b. Nothing in the SDK tells the replica actually running
  the tool. The executor therefore polls the store every 500 ms while a tool
  runs (`src/agent.ts:145-152`), aborts the tool's `AbortSignal`, and records
  `metadata.loop_stopped` (`src/agent.ts:184-191`). That poll is the
  hand-written part; a real deployment would replace it with a Postgres
  `LISTEN/NOTIFY` or a shared pub/sub, which the SDK does not provide.
- **Idempotency**: the SDK has no `messageId` index; `spike_message_tasks`
  with an `INSERT ... ON CONFLICT DO NOTHING` reservation makes a concurrent
  duplicate on the other replica wait for the winner (`src/server.ts:153-175`).
- **Migrations**: `a2a-db upgrade` takes a shared migration lock
  (`a2a_migrations_lock` table), so both replicas can start simultaneously; the
  hand-written table creation uses `pg_advisory_xact_lock` for the same reason.

## Bun verdict: **passes unchanged**

`bun run src/server.ts` with zero source edits, flags, polyfills or
`bunfig.toml`: all 17 gate steps pass (output above). `process.execPath` under
Bun points at `bun`, so the `a2a-db` migration subprocess also ran under Bun
("tasks: up to date" in the replica log). `express` 5, `pg` 8 and Kysely all
worked on Bun's Node compatibility layer. RSS is marginally lower than Node
with the tsx loader. Not tested: the Docker image is Node-only (no Bun image
was built), and the Anthropic smoke was run on Node only (see below).

## Test ergonomics

The gate drives the server over HTTP, so the natural unit seam is the
`AgentExecutor`: `SupportExecutor.execute(ctx, bus)` takes a `RequestContext`
and an `ExecutionEventBus`, both plain interfaces, and the fake tool is an
exported async function with an `AbortSignal`. A test can construct a
`DefaultRequestHandler` with the SDK's `InMemoryTaskStore` (exported from
`@a2a-js/sdk/server`) and call `sendMessage`/`cancelTask` directly without
Express, or mount `restHandler` on an Express app and use `supertest`. Node 24
ships `node:test`, Bun ships `bun test`; neither needs extra deps. The
`UserBuilder` is a one-line function, trivially unit-testable. What is awkward:
the SDK's protocol types are generated from protobuf (`Part.content.$case`,
`Role.ROLE_USER` numeric enums, `TaskState` numbers), so hand-written
fixtures are verbose and must fill every field (`metadata: undefined`,
`extensions: []`) to satisfy `strict`.

## SDK gaps found

1. No server-side interceptor/middleware hook in the Express binding
   (`CallInterceptor` is client-only). Tracing is plain Express middleware
   outside the SDK, so it sees the HTTP layer but not the parsed A2A call; the
   task id has to be fished out of the URL or the response body
   (`src/server.ts:84-92`).
2. Cross-replica cancel stops at the store; the executing replica is never
   notified. Hand-written poll (see above).
3. No `messageId` idempotency. Hand-written table.
4. Schema migration is a CLI (`bin a2a-db`), not a function; it is invoked as
   a child process at boot (`src/server.ts:104-105`).
5. Returning a 401 (rather than the SDK's default error mapping) from
   `UserBuilder` requires subclassing `A2AError` with REST transport fields;
   there is no `UnauthorizedError` export.
6. `ServerCallContext.tenant` exists but nothing in the Express binding sets
   it; the spike leaves it `''` and uses the principal as the store owner.
7. `AgentExecutor.cancelTask` receives the task id and bus only, not the
   `ServerCallContext`; the executor keeps its own in-flight map keyed by task
   id (`src/agent.ts:80`).

## Real-Anthropic smoke

`src/smoke_anthropic.ts` uses `client.beta.messages.toolRunner` with one
`betaTool` wrapping `getServiceStatus`. Runs only with `ANTHROPIC_API_KEY`.
Run 2026-10-26 on Node 24 on `claude-sonnet-5-5` with a key from the
dedicated spike workspace: PASS (`tool_use` then `end_turn` with "svc-x is
degraded, 1 replica"). Not run on Bun.
