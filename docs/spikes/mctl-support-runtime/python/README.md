# Python candidate: a2a-sdk 1.2.3 + anthropic 2.3.0

One vertical slice of `mctl-support` (see `../README.md`) on the official
Python A2A SDK, two replicas on one Postgres, scored by `../gate/gate.py`.

## Run

```
# Postgres from ../README.md (container spike-support-pg, port 55432); database spike_python
export DATABASE_URL=postgresql+asyncpg://spike:spike@127.0.0.1:55432/spike_python
PORT=8001 REPLICA=a .venv/bin/python server.py   # replica A
PORT=8002 REPLICA=b .venv/bin/python server.py   # replica B
python3 ../gate/gate.py --a http://127.0.0.1:8001/v1 --b http://127.0.0.1:8002/v1
```

- **REST base: `/v1`** (`create_rest_routes(handler, path_prefix="/v1")`).
  Card at `/.well-known/agent-card.json`.
- Tables (`tasks`, `task_versions`, `task_events`) are created by the SDK on
  first start (`create_table=True`).
- `.venv` is Python 3.10; the Dockerfile uses `python:3.12-slim`.
- Real-Anthropic smoke: `ANTHROPIC_API_KEY=... .venv/bin/python smoke_anthropic.py`
  Run 2026-10-26 on `claude-sonnet-5-5` with a key from the dedicated spike
  workspace: PASS (`beta.messages.tool_runner`, one `tool_use` round, then
  `end_turn` with "svc-x is degraded, 1 replica").

## SDK hooks used

| Concern | Hook (module.Class) | Ours or SDK |
|---|---|---|
| Bearer auth | Starlette `AuthenticationMiddleware` + `SpikeBearerBackend` (server.py). The SDK adapts `request.user` into its `User` via `a2a.server.routes.common.StarletteUser` (`routes/common.py:74-88`) inside `DefaultServerCallContextBuilder.build_user` (`:134`). | Backend hand-written (~12 lines); the SDK has no auth hook of its own, only this adapter |
| Call context | `a2a.server.context.ServerCallContext` built per request by `a2a.server.routes.common.DefaultServerCallContextBuilder` (`:103-120`); the executor reads `context.call_context.user.user_name` (agent.py). | SDK |
| Owner / tenant scoping of the store | `a2a.server.owner_resolver.resolve_user_scope` (default: `context.user.user_name`); every `get`/`list`/`save` in `VersionedDatabaseTaskStore` filters on `owner` (`cluster/database_task_store.py:165, 261, 285-290`). | SDK |
| Request-context builder | `a2a.server.agent_execution.SimpleRequestContextBuilder`, subclassed as `SupportContextBuilder` to derive `taskId = uuid5(owner, messageId)` for idempotency and to remember that the client named a `contextId`. | Subclass, 7 lines |
| Request handler | `a2a.server.request_handlers.DefaultRequestHandlerV2`, subclassed as `SupportRequestHandler` for idempotent replay and cooperative cross-replica cancel. | Subclass, ~45 lines |
| Task store | `a2a.server.cluster.VersionedDatabaseTaskStore` (CAS on a `task_versions` side table, `cluster/database_task_store.py:145-240`). | SDK |
| Cross-replica event stream | `a2a.server.cluster.DatabaseTaskEventStream` (polls `task_events`, `cluster/database_event_stream.py:126-178`, `poll_interval_s=0.25`). | SDK |
| Cross-replica cancel | SDK path: `DefaultRequestHandlerV2.on_cancel_task` -> `_cancel_remote` -> `_write_cancel` (`default_request_handler_v2.py:231-316`) force-writes CANCELED into the store. Ours: write a `cancel_requested` marker event first, wait up to 6 s for the owning replica's loop to write CANCELED + `loop_stopped`, then fall back to the SDK's forced write. The executor's slow tool races its timer against `local_cancel` and the event stream (`agent.py:_sleep_unless_cancelled`). | SDK primitive + ~60 lines ours |
| Executor | `a2a.server.agent_execution.AgentExecutor` (`execute`/`cancel`), `a2a.server.tasks.TaskUpdater`, `a2a.helpers.proto_helpers.new_task/new_text_artifact`. | SDK |
| Tracing | `DefaultRequestHandlerV2` is `@trace_class(kind=SpanKind.SERVER)` (`a2a/utils/telemetry.py:324`), so real OTel spans exist for free when an SDK is installed. The required one-line `trace task=<id> principal=<p>` is a hand-written ASGI middleware (`TraceMiddleware`, server.py), because the SDK has no server-side interceptor; only the client has `a2a.client.interceptors.ClientCallInterceptor`. | Spans SDK; line ours (~30 lines) |
| Routes / card | `a2a.server.routes.create_rest_routes`, `create_agent_card_routes`. | SDK |

Hand-written in total: bearer backend, trace middleware, idempotent
messageId, cooperative cancel, the fake loop. Everything protocol-shaped
(validation, 400 on a terminal task, 401 shape via Starlette, versioned
store, owner scoping, event log, streaming) is the SDK's.

## Measurements

Gate output, verbatim (re-run 2026-10-27 with the fixed step 7c; the first
run on 2026-10-26 passed the same 17 steps with no code changes):

```
PASS  1 agent card served  -- HTTP 200, name='mctl-support (python spike)', interfaces=['HTTP+JSON']
PASS  2a message:send without bearer refused  -- HTTP 401
PASS  2b message:send with foreign bearer refused  -- HTTP 401
PASS  3 send to A returns a task  -- HTTP 200, state=submitted, id='fb15dfeb-4299-58d5-a79b-2caa336ba13e'
PASS  4a task completes, read via B  -- state=completed
PASS  4b answer carries the fake tool's evidence  -- svc-x is degraded (1 replica); principal=namax66; turn 1
PASS  4c executor saw the bearer's principal
PASS  4d contextId stable across replicas  -- ctx=6735385d-d378-4600-8a8c-f6f3a6a0f8b9
PASS  5a context continued on B sees turn 1 (written by A)  -- turn 2; principal=namax66
PASS  5b context continued on A sees turns 1-2 (one written by B)  -- turn 3; principal=namax66
PASS  6 other tenant cannot continue the context  -- HTTP 200, state=rejected, text=unknown context
PASS  7a slow task is working, seen via B  -- state=working
PASS  7b cancel via B, canceled seen via A within 10s  -- HTTP 200, state=canceled, after 0.3s
PASS  7c state still canceled after the slow tool would have finished  -- state=canceled
PASS  7d executor recorded that the loop observed the cancel (metadata.loop_stopped)  -- {"loop_stopped": true, "reason": "remote-cancel-requested", "stopped_on": "a"}
PASS  8 duplicate messageId across replicas returns the same task (idempotency)  -- 913ac437-76ad-5812-9554-c6475c84aeb3 vs 913ac437-76ad-5812-9554-c6475c84aeb3 (HTTP 200/200)
PASS  9 a message addressed to a completed task is refused or opens a new task  -- HTTP 400, body={"error": {"code": 400, "status": "FAILED_PRECONDITION", "message": "Task fb15dfeb-4299-58d5-a79b-2caa336ba13e is in ter

all steps passed
```

Trace hook evidence from replica A's log during the gate (one line per
request; 401s carry `principal=-`):

```
spike.trace trace task=- principal=- method=POST path=/v1/message:send status=401 ms=0
spike.trace trace task=082bcb03-8c3e-50ce-99d5-9619bdec9871 principal=namax66 method=POST path=/v1/message:send status=200 ms=42
spike.trace trace task=9ee99f3f-013a-55e9-8644-2dcf519c323d principal=namax66 method=GET path=/v1/tasks/9ee99f3f-... status=200 ms=5
```

| Metric | Value |
|---|---|
| Gate | 17/17 PASS, exit 0 |
| LOC (`wc -l *.py`) | agent.py 225, server.py 273, smoke_anthropic.py 54, total 552 |
| Dependencies | 54 packages installed (`pip list | wc -l`, includes pip/setuptools); 3 direct: `a2a-sdk[http-server,sql,postgresql]==1.2.3`, `anthropic==2.3.0`, `uvicorn==0.41.0` |
| Notable transitive | starlette 1.7.0, SQLAlchemy 2.0.55, asyncpg 0.32.0, protobuf 7.36.2, pydantic 2.14.1, httpx 0.28.1 |
| RSS after the gate | replica A 18064 kB, replica B 17968 kB (`ps -o rss=`, macOS, Python 3.10) |
| Docker image | `spike-support-python` 397 MB (python:3.12-slim, venv copied from a build stage, non-root uid 10001) |
| Cold start | 2.18 s from `docker run` to the first 200 on `/.well-known/agent-card.json` (includes Postgres connect + table check against host.docker.internal) |
| Cancel latency | 0.6 s from `:cancel` on B to `canceled` read on A (event-stream poll 0.25 s) |
| SDK releases | v1.2.3 2026-10-19, v1.2.2 2026-10-05, v1.2.1 2026-09-30, v1.2.0 2026-09-29, v1.1.5 2026-09-21, v1.1.4 2026-09-08 |
| SDK open issues | 86 (`gh api repos/a2aproject/a2a-python --jq .open_issues_count`, issues + PRs) |

### Test ergonomics

The loop is a plain `while` over `FakeModel.step(user_text, tool_results)`,
which returns either a `ToolCall` or a `FinalText`, so any scripted model is
a small class with one method. Unit-testing `SupportExecutor.execute` needs
three fakes that the SDK makes cheap: `RequestContext` is a dataclass-like
object the SDK builds from a `SendMessageRequest` (`SimpleRequestContextBuilder`),
`EventQueue` is an in-memory asyncio queue whose events can be drained and
asserted on (`Task`, `TaskStatusUpdateEvent`, `TaskArtifactUpdateEvent`),
and the store can be `InMemoryTaskStore` wrapped in `LegacyTaskStoreAdapter`.
What is not ideal today: `FakeModel` is constructed inside `execute` rather
than injected, so a test swaps it by subclassing or monkeypatching; and the
slow tool subscribes to a `TaskEventStream`, so a test for the cancel path
needs a stub stream (an async generator that yields one marker event). Both
are 10-line changes. No network, no Postgres and no event loop beyond
`asyncio.run` are needed for the happy paths.

### SDK gaps (honest)

1. **No server-side auth or interceptor hook.** Auth is "whatever Starlette
   puts in `request.user`"; the SDK only adapts it (`StarletteUser`). The
   per-request trace line had to be an ASGI middleware. Spans are there via
   `@trace_class`, but they do not know the principal.
2. **Cross-replica cancel is forced, not cooperative.** The SDK's
   `_cancel_remote` writes CANCELED into the store and relies on the owner
   "seeing the version bump on its next save" (`default_request_handler_v2.py:252-254`).
   A loop sitting in a 20 s tool call has no next save, so with the SDK path
   alone step 7d (`loop_stopped`) cannot be met and the owner's eventual
   `complete()` would die with `ConcurrentTaskModificationError`. The
   marker-event + wait + fallback in `SupportRequestHandler.on_cancel_task`
   is ours, and the executor must poll the event stream itself.
3. **`DatabaseTaskEventStream.publish` is a no-op** (`database_event_stream.py:102-104`):
   the event is only persisted when passed to `store.save(event=...)` in the
   same transaction. Calling `publish` separately silently does nothing.
   Not documented at the call site in the handler (`_write_cancel` calls it anyway).
4. **No idempotency on `messageId`.** Each send creates a new task unless the
   client sets `taskId`. The `uuid5(owner, messageId)` derivation in
   `SupportContextBuilder` plus the replay in `on_message_send` is ours.
5. **No "unknown context" refusal for a foreign principal at the protocol
   layer.** The store is owner-scoped (good: a foreign bearer sees no tasks),
   but a `message:send` naming someone else's `contextId` silently starts a
   new task in that contextId under the new owner. The executor rejects it
   (step 6 passes with `rejected`), which is a policy we had to write.
6. **Streaming across replicas is explicitly unsupported in V2**: the handler
   docstring says to "route subscription requests to the replica holding the
   task" (`default_request_handler_v2.py:126-129`). Polling `GET /tasks/{id}`
   works from any replica; `message:stream` / `tasks/{id}:subscribe` do not.
7. Minor: Starlette's `AuthenticationMiddleware` returns a generic 401 body
   unless `on_error` is overridden; `TaskNotCancelableError` on an already
   terminal task is a 400 (`FAILED_PRECONDITION`), not 409.

### Multi-replica story

Documented in code, not in the package README (`a2a_sdk-1.2.3.dist-info/METADATA`
does not mention replicas or clustering). The `a2a.server.cluster` package
(831 lines) is the whole story:

- `cluster/database_task_store.py:145-240`: `save()` does compare-and-swap on
  `task_versions.version` (owner-scoped). A CANCELED write locks the version
  row and overwrites any non-terminal task; any other write with a stale
  `prev_version` raises `ConcurrentTaskModificationError` (never retried).
  Lock order is always versions row then tasks row (`:192-194`), so two
  writers cannot deadlock.
- `cluster/database_event_stream.py:126-178`: `subscribe()` is a polling
  loop over `task_events.seq`. It starts from the last seq at or before the
  requested version (`:106-124`), so a subscriber that comes late replays.
- Races visible from reading it:
  - **Forced remote cancel vs. the owner's in-flight write.** The owner's
    next `save(prev_version=stale)` fails with `ConcurrentTaskModificationError`
    (`:234`). The SDK surfaces that to the executor as an exception from
    `TaskUpdater`; our loop avoids it only because it observes the marker
    first. A loop that ignores the stream would end with an unhandled
    exception on the owner and CANCELED in the store, which is the right
    final state but a noisy one.
  - **Two sends on the same `contextId` on different replicas** both read
    `list(context_id)` to count prior turns before either saves, so both can
    compute the same turn number. The store does not serialize per context,
    only per task. Harmless for this slice, real for a transcript-counting
    agent.
  - **Poll latency**: cancel visibility is bounded by `poll_interval_s`
    (default 0.5 s, `database_event_stream.py:35`; set to 0.25 s here);
    nothing pushes.
  - `VersionedDatabaseTaskStore.save` ignores `prev` (`:162 del prev`), so
    the CAS is purely on the version counter, not on content.

## Files

- `server.py`: wiring, auth backend, trace middleware, request handler subclass.
- `agent.py`: executor, fake model, fake tool, cancellation race.
- `smoke_anthropic.py`: real-Anthropic tool runner smoke, key from env only.
- `Dockerfile`, `requirements.txt`.
