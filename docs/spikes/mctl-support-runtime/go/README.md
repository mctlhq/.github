# Go candidate: a2a-go v2.7.0 + anthropic-sdk-go 1.84.0

The Go prototype of the vertical slice in `../README.md`. Four source files
plus the Anthropic smoke; the gate passes 17/17 on two replicas sharing one
Postgres.

## Run

```
# Postgres from ../README.md must be up (port 55432); the database is created once:
docker exec spike-support-pg psql -U spike -c "CREATE DATABASE spike_go"

cd go
go build -o spike-support-go .
PORT=8011 REPLICA=a ./spike-support-go &      # DATABASE_URL defaults to the spike DSN
PORT=8012 REPLICA=b ./spike-support-go &
python3 ../gate/gate.py --a http://127.0.0.1:8011/v1 --b http://127.0.0.1:8012/v1
```

Env: `PORT` (default 8011), `REPLICA` (log prefix and trace field),
`DATABASE_URL` (default `postgres://spike:spike@127.0.0.1:55432/spike_go?sslmode=disable`).
The schema (`tasks`, `task_messages`) is created on start with
`CREATE TABLE IF NOT EXISTS`.

**REST base is `/v1`.** `a2asrv.NewRESTHandler` mounts its routes at the root
of whatever mux it is given (`a2asrv/rest.go:53-62`: `POST /message:send`,
`GET|POST /tasks/{idAndAction}`), so `main.go` mounts it under
`http.StripPrefix("/v1", ...)` and advertises `http://host:port/v1` in the
agent card's `supportedInterfaces`. The agent card is served by the SDK at
`a2asrv.WellKnownAgentCardPath` (`/.well-known/agent-card.json`).

Real-Anthropic smoke (never part of the gate, needs `ANTHROPIC_API_KEY` in
the environment, prints `skipped: no key` otherwise):

```
go run ./smoke_anthropic
```

Run 2026-10-26 on `claude-sonnet-5-5` with a key from the dedicated spike
workspace: PASS (tool loop, 2 iterations: `tool_use` then `end_turn` with
"svc-x is degraded, 1 replica").

## SDK hooks used

| Concern | Hook | Given by the SDK or hand-written |
|---|---|---|
| Transport | `a2asrv.NewRESTHandler` (HTTP+JSON binding), `a2asrv.NewStaticAgentCardHandler` | SDK |
| Auth | `a2asrv.CallInterceptor` (`bearerAuth` in `interceptors.go`), reads `callCtx.ServiceParams().Get("authorization")`, sets `callCtx.User = a2asrv.NewAuthenticatedUser(principal, nil)` | Hook is SDK; the policy (prefix `spike-`) is hand-written |
| Error to HTTP status | returning `a2a.ErrUnauthenticated` / `a2a.ErrUnauthorized` / `a2a.ErrTaskNotFound` from `Before` maps to 401 / 403 / 404 (`internal/rest/rest.go:217-227`) | SDK |
| Call context in the executor | `a2asrv.ExecutorContext.User` (copied from `CallContext.User` in `a2asrv/agentexec.go:174` and `:333`) | SDK |
| Interceptor / OTel-style trace | `a2asrv.CallInterceptor.After` (`traceInterceptor`), one `trace task=<id> principal=<p> method=<m> replica=<r>` line per request; `callCtx.Method()` gives the A2A method name | Hook is SDK; the line is hand-written. The SDK also ships an `examples/observability` with real OTel, not used here |
| Task store | `a2asrv/taskstore.Store` interface, wired with `a2asrv.WithTaskStore`, per-user masking via `a2asrv.NewTaskStoreAuthenticator()` (`middleware.go:110`) | Interface is SDK; **the Postgres implementation is hand-written** (`pgstore.go`, 267 lines) |
| Cross-replica cancel | `AgentExecutor.Cancel` + the store's OCC (`internal/taskupdate/manager.go:140-150` retries a `canceled` event on `ErrConcurrentModification`) marks the task canceled from any replica; the running loop on the other replica stops itself by polling the store (`executor.go` `watchCancel`) | Marking: SDK. Stopping the in-flight loop on the other replica: hand-written |
| Tenant isolation of contexts | `contextOwnerGuard` interceptor (refuses a foreign `contextId` with the same 404 the store uses) | Hand-written |
| `messageId` idempotency | `contextOwnerGuard` + `task_messages` index; the interceptor's `Before` returns the stored task as an early response (`intercepted_handler.go:218-233` short-circuits the chain) | Hand-written |
| `metadata.loop_stopped` | direct `jsonb_set` in `pgStore.patchMetadata` | Hand-written bypass (see gaps) |
| Model tool loop | `model` / `tool` interfaces in `executor.go`, `fakeModel` | Hand-written (the real one is `toolrunner` in the smoke) |

### Finding: no SQL task store in a2a-go v2.7.0

Confirmed against the module source. `a2asrv/taskstore/` holds `api.go`,
`inmemory.go`, `update_materializer.go`, `validator.go` and tests. No file in
the whole module imports `database/sql`, `pgx` or any driver
(`grep -rln 'database/sql' .` is empty; `go.mod` has no SQL dependency).
`taskstore.InMemory` is the only implementation; `pgstore.go` is the port of
its contract (OCC via `PrevVersion`, foreign task masked as
`a2a.ErrTaskNotFound`, `ErrTaskAlreadyExists` on duplicate id) to pgx. The
conformance tests in `taskstore/store_test.go` are package-internal and cannot
be run against an external implementation.

## Measurements

### Gate output (verbatim, 2026-10-26)

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

Wall time of the gate: 14.3 s (12 s of it is the gate's own sleep in step 7c).

### Numbers

| Metric | Value | How |
|---|---|---|
| LOC | 768 total: `executor.go` 234, `interceptors.go` 119, `main.go` 82, `pgstore.go` 267, `smoke_anthropic/main.go` 66 | `wc -l *.go smoke_anthropic/*.go` |
| Modules in the graph | 84 (incl. the main module) | `go list -m all \| wc -l` |
| Direct requires | 3: `a2a-go/v2 v2.7.0`, `anthropic-sdk-go v1.84.0`, `pgx/v5 v5.11.0` | `go.mod` |
| Static binary | 16.8 MB (`-s -w -trimpath`) | `ls -l` |
| RSS of one replica after the gate | 15.2 MB (A), 13.7 MB (B) | `ps -o rss` |
| Container memory at idle | 2.2 MiB | `docker stats` |
| Docker image | 21.3 MB (`gcr.io/distroless/static-debian12:nonroot`) | `docker build -t spike-support-go . && docker images spike-support-go --format '{{.Size}}'` |
| Cold start | 0.35 s / 0.41 s / 0.40 s (three runs) from `docker run` to the first 200 on `/.well-known/agent-card.json`, including schema `CREATE IF NOT EXISTS` against `host.docker.internal:55432` | timed loop with `curl` every 50 ms |

### Test ergonomics

The SDK's executor contract is `iter.Seq2[a2a.Event, error]`, so a unit test
drives `Execute` directly with a hand-built `a2asrv.ExecutorContext` and
collects events without any HTTP. Interceptors are plain structs with
`Before`/`After` and take an `*a2asrv.CallContext` built by
`a2asrv.NewCallContext(ctx, a2asrv.NewServiceParams(map))`, so auth and the
trace line test in isolation. End-to-end, `a2aclient` has a REST transport
that points at an `httptest.Server`; the two-replica case needs a real
Postgres (the gate uses the docker one; `testcontainers-go` would do the same
in CI). What is missing: the SDK's `internal/testutil` (fake store, queues,
test executor) is not importable, and the task-store conformance tests are
package-internal, so a custom store has no reusable contract suite. Standard
`go test`, race detector and `go vet` apply; no code generation is involved.

### SDK maturity

```
$ gh api repos/a2aproject/a2a-go/releases --jq '.[0:6][]|.tag_name+" "+.published_at'
v2.7.0 2026-10-21T09:27:19Z
v2.6.0 2026-09-25T06:22:49Z
v2.5.0 2026-08-18T12:40:33Z
v2.4.0 2026-07-28T14:26:22Z
v2.3.1 2026-05-13T12:20:05Z
nightly-metrics 2026-05-13T15:03:32Z
$ gh api repos/a2aproject/a2a-go --jq '.open_issues_count'
36
```

Of the 36, 23 are open pull requests (GitHub counts them in
`open_issues_count`), so 13 open issues. Last push 2026-10-26, 495 stars.
Monthly minor releases since July; v2.7.0 (five days before this spike)
changed the REST content type to `application/a2a+json` with negotiation and
started rejecting unknown `A2A-Version`. The CHANGELOG shows the REST/JSON
surface is still moving between minors.

## Multi-replica story

What the SDK gives and where it stops, with the lines in
`$(go env GOMODCACHE)/github.com/a2aproject/a2a-go/v2@v2.7.0/`:

- **Shared state** is the task store. Every request handler loads the task
  from `taskstore.Store` (`a2asrv/agentexec.go:234-270` builds the execution
  context from `StoredTask`), so a task started on A is readable and
  continuable on B as soon as the store is shared. Steps 4-5 pass on that
  alone.
- **Cancel across replicas** is designed through OCC, not through a signal.
  `AgentExecutor.Cancel` (`a2asrv/agentexec.go:112-127`, doc comment) says the
  canceled status is applied to the store and "the next execution update will
  fail on OCC and execution will be canceled". `internal/taskupdate/manager.go:140-150`
  retries the `canceled` event after a concurrent modification. That marks the
  task canceled from B while A is still running (step 7b, 0.0 s).
- **Stopping the in-flight loop** on A is not done by the SDK when the cancel
  landed on B. The executor's `ctx` is cancelled only by
  `localManager.handleCancelWithConcurrentRun`
  (`internal/taskexec/local_manager.go:247`, `:363`), which runs only when the
  execution is in this process's map. On A the loop would keep running the
  slow tool until its next event hits OCC. For a read-only agent that is
  wasted model tokens, not a correctness bug, but the gate requires
  `loop_stopped`, so `executor.go` `watchCancel` polls the store every 250 ms
  and cancels its own `ctx` when the task turns terminal.
- **Recording that the loop stopped** has no sanctioned path: once the task
  is terminal, `internal/taskupdate/manager.go:93-99` rejects every further
  event with `ErrInvalidAgentResponse`, so the executor's own
  `canceled` event with `metadata.loop_stopped` is dropped. `pgStore.patchMetadata`
  writes the key with `jsonb_set` and bumps the version; this is the one place
  the prototype writes to the store outside the SDK's contract.
- **Cluster mode exists but is not usable off the shelf.**
  `a2asrv.WithClusterMode(ClusterConfig)` (`a2asrv/handler.go:197-212`,
  marked experimental) distributes executions over a `workqueue.Queue` and an
  `eventqueue.Manager`. v2.7.0 ships only `workqueue.NewInMemory`,
  `eventqueue.NewInMemoryManager` and the `NewPullQueue`/`NewPushQueue`
  adapters, whose `ReadWriter`/`Writer` the operator must implement on a real
  broker. Nothing is provided for Postgres, Redis or NATS. With such a queue
  the running replica would receive the cancel as a work item and the
  `watchCancel` poll would be unnecessary.

## SDK gaps (honest list)

1. No SQL/Postgres task store; 267 lines of hand-written store with OCC and
   ownership masking, and no importable conformance suite to prove it.
2. No cross-replica cancellation signal without a user-supplied broker
   (`WithClusterMode` is experimental and ships in-memory adapters only).
3. A terminal task accepts no further event, so an executor that learns of a
   cancel late cannot record anything through the SDK.
4. No `messageId` idempotency: a repeated `messageId` opens a second task
   (`a2asrv/agentexec.go:299-308` creates a new task per `SendMessage`
   without a message id lookup). Hand-written index and interceptor.
5. A client-supplied `contextId` is accepted as-is
   (`a2asrv/agentexec.go:301-303`); per-task ownership masking is in the
   store, per-context ownership is not. Hand-written guard.
6. Auth is "a `CallInterceptor` that sets `callCtx.User`"; there is no
   bearer/JWT helper on the server side (`a2aclient/auth.go` holds client-side
   credentials only), and `CallContext.Tenant()` (`a2asrv/middleware.go:70`)
   is the agent owner's id from the transport, not the caller's tenant.
7. The REST surface moved in v2.7.0 (content type, `A2A-Version` check), so
   client compatibility must be re-verified at each minor.

What the SDK did well: 401/403/404 mapping from sentinel errors, the
interceptor early-response short-circuit (used for idempotency), the OCC
contract on the store, UUIDv7 task ids, the terminal-task refusal in step 9
(`FAILED_PRECONDITION`, from `a2a.ErrUnsupportedOperation` at
`a2asrv/agentexec.go:239`), and the iterator-based executor that
needs no channels or goroutine plumbing in user code.

## Gaps in the hand-written layer (found in review, PR .github#252)

These are defects of this prototype's own code, not of the SDK. The gate runs
its requests one after another and cannot see them. They are recorded rather
than fixed so the measured LOC and gate output stay those of the code that
produced them.

1. **`messageId` idempotency is check-then-act.** `contextOwnerGuard` looks the
   `messageId` up, the task is created, and only then does `indexMessages`
   write the index row with `ON CONFLICT DO NOTHING`. Two concurrent sends with
   one `messageId` on two replicas both miss the lookup and create two tasks.
   A correct version reserves the `messageId` first (insert, then read back the
   winner), as the TypeScript prototype does, or derives the task id from it,
   as the Python one does.
2. **Context existence oracle.** A named `contextId` owned by another principal
   gets 404; a `contextId` that does not exist starts a new task (200). A
   caller can therefore probe which context ids exist. Both cases should get
   the same answer.
3. **`pgStore.List` filters status after `LIMIT`.** The SQL query pages, the
   status filter runs in Go, so a page can come back short or empty while
   matching tasks exist further on.
4. **Bearer scheme is case-sensitive.** Only `Bearer ` is accepted; RFC 6750
   treats the scheme name case-insensitively.
5. **No tests for `pgStore`.** Only the gate exercises it.
