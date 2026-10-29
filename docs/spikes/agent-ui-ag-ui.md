# Agent UI: AG-UI 1.0 as MCTL's northbound agent-to-user protocol

Phase A spike report for mctlhq/.github#258 (carrier DevLoop; refs only, not
closing). Phase B (the DevLoop Run Inspector) is designed here and not started.

## Non-claims and evidence tags

This report changes nothing in production. AG-UI is a presentation projection,
never authority: no event, resume payload or frontend tool grants, edits or
replays an approval. It does not authorize a new runtime, UI, endpoint, tool
registry or auth path, and it does not select CopilotKit or any frontend
framework. Phase B only starts after the owner records a GO-equivalent
`decision` on the manifest.

Every claim carries one tag. `[CODE]` is read at a pinned commit. `[DEPLOYED]`
is observed through mctl tools or gitops. `[DOC]` is issue, plan or tool-contract
text. `unknown (not observed)` means this spike did not see it; it is never
read as "absent". No payload bodies are quoted, only identifiers and paths.

## Evidence pins (read 2026-10-27)

| Repository | Pin (40-hex) | Used for |
| --- | --- | --- |
| mctlhq/.github | `ad7408b2a3512b46d5edd2496e78a0062bf83f3e` | conventions, roadmap schema |
| mctlhq/mctl-api | `b3953e1454de8134363dce37c6592b6465a7d1cb` | routes, approval handlers, SSE search |
| mctlhq/mctl-agents | `f8dc34c52e80fb3bd448ff5902cb92867ffa6d27` | `DevLoopWorkflow` signals and queries |
| mctlhq/mctl-portal | `5f6f48f15f911f361f71e05e33bc78253172b553` | plugin list, transport search |
| mctlhq/mctl-gitops | `d991f7e04a035adf6f24a5c8991b9803e5f188a2` | worker values (`ingress`) |
| ag-ui-protocol/ag-ui | tag `@ag-ui/core@1.0.3` = `a1131c90709d51d562202b7b6cc0a2a1b48b05fc` | event enum, interrupt/resume schema (`sdks/typescript/packages/core/src/generated/schemas.ts`) |

Not observed: `mctl_get_dev_loop` and `mctl_list_agent_executions` were refused
as outside the investigate scope, so live execution records are unknown (not
observed). mctl-api and mctl-portal deployed configuration is unknown (not
observed; neither is a catalogue service in `admins`). How the Claude Code CLI
output is captured inside Argo pods was not traced to a token stream:
unknown (not observed). Event names below were taken from the pinned schema;
where the issue text differs, the schema wins. The 1.0.3 `EventType` enum has:
TEXT_MESSAGE_{START,CONTENT,END,CHUNK}, TOOL_CALL_{START,ARGS,END,CHUNK,RESULT},
STATE_{SNAPSHOT,DELTA}, MESSAGES_SNAPSHOT, ACTIVITY_{SNAPSHOT,DELTA}, RAW, CUSTOM,
RUN_{STARTED,FINISHED,ERROR}, STEP_{STARTED,FINISHED},
REASONING_{START,MESSAGE_START,MESSAGE_CONTENT,MESSAGE_END,MESSAGE_CHUNK,END,ENCRYPTED_VALUE},
SUBAGENT_{STARTED,FINISHED,ERROR}. There is no usage event. Interrupts are a
`RUN_FINISHED` outcome of type `interrupt` (a list of `Interrupt` with `id`,
`reason`, optional `toolCallId`, `responseSchema`), and the answer is
`RunAgentInput.resume[]` entries (`interruptId`, `status` resolved|cancelled,
`payload`). A run has `threadId`, `runId`, `parentRunId`, `protocolVersion`.
All tagged `[CODE]`.

## D1. Fact inventory

| Fact | Source of truth | API/event surface | Durable | User-authoritative | Correlation id |
| --- | --- | --- | --- | --- | --- |
| Run start/end, agent version | agent execution records `[CODE]` | `GET /agents/executions`, `POST /agents/executions` (router.go:645-646) | yes | no | `we_`, Temporal workflow and run id |
| Loop liveness | Temporal | `GET /agents/dev-loop/{id}` `[DOC]`: status only, no step | Temporal history | no | workflow id `dev-loop-<org>-<repo>-<n>` |
| Workflow phase | `DevLoopWorkflow` (`_canonical_pending_phase`, dev_loop.py:3454-3487) and queries `work_context`, `abandon_state`, `implement_execution`, `lifecycle_claim` `[CODE]` | Temporal queries; not exposed as an API in this read: unknown (not observed) | history | no | workflow id |
| Work item state | WorkItem store `[CODE]` | `GET /work-items`, `/work-items/{id}`, `/executions` (router.go:830-833) | yes | no | `wi_`, `state_version`, `cs_`, `xr_` |
| Streamed model response | not observable `[CODE]`: no token or message stream found | none | no | no | none |
| MCP invocation | policy checkpoint record, if recorded: unknown (not observed) | none seen | unknown (not observed) | no | `trace_id` where present |
| Role/subagent transitions | stage records only; no execution boundary below a stage found | none | partial | no | `we_` |
| Approval request/decision | `aar_` action approval `[CODE]` | `POST /action-approvals`, `/{id}/decision`, `/consume`, `/execute` (router.go:724-727); dev-loop `POST .../approve` | yes | yes (human admin only) | `aar_`, intent hash, artifact digest |
| Human input (clarification) | human-input ledger `[CODE]` | `GET /human-input`, `GET /human-input/{id}`, `POST .../response` (router.go:697, 875-876); signal `human_input_response`, query `human_input_state` | yes | yes | `request_id` |
| Cancel | abandon signal `[CODE]` | `POST .../dev-loop/{id}/abandon` | recorded as workflow result | yes | workflow id |
| Token usage and cost | usage ledger `[CODE]` | `GET /usage/records`, `GET /usage/summary` (router.go:948-950); three distinct cost fields, never summed `[DOC]` | yes | no | `workflow_id`, stage, `execution_id` |
| Proposal/evidence refs | gitops proposals, evidence routes `[CODE]` | `/api/v1/evidence*`, gitops path | yes | no | proposal slug, `wi_` |
| Audit and trace refs | audit log, incident `trace_id` `[DOC]` | `mctl_list_recent_operations`, workflow logs archive | yes | no | `trace_id` |

Streaming surface: a search of mctl-api (non-test Go) for `text/event-stream`
and `http.Flusher` found nothing; matches are tests that send an `Accept`
header. The mctl-portal matches are an OIDC comment and a TechDocs warm-up
client, not agent-run streaming; its work-items and proposals plugins are
backend request/response `[CODE]`. mctl-agents has no server-side event stream
`[CODE]`. So no agent-run streaming surface exists at these pins. Whether a
non-main branch or unmerged work adds one: unknown (not observed).

## D2. Mapping matrix

| MCTL concept | AG-UI event | Class | Where authority stays |
| --- | --- | --- | --- |
| Execution start/end | RUN_STARTED, RUN_FINISHED, RUN_ERROR | exact for `we_` records | execution record |
| Workflow phase | STEP_STARTED/STEP_FINISHED (stage name) | lossy: from records, not live Temporal state | Temporal, execution record |
| Streamed response | TEXT_MESSAGE_* | no mapping until a token stream is sourced; coarse step summary as one message | none |
| MCP invocation | TOOL_CALL_START/END/RESULT | exact only for calls the checkpoint already records; ARGS carry a digest or reference, never bodies | policy checkpoint, mctl-api tenant authz |
| Execution state | STATE_SNAPSHOT, STATE_DELTA | lossy: bounded reference-only projection | WorkItem, `state_version` |
| Role/subagent | SUBAGENT_* | no mapping; step metadata unless a real execution boundary exists | execution record |
| Approval / human input | RUN_FINISHED outcome `interrupt`, resume entry | exact for `aar_` and `request_id` as references | `DecideActionApproval`, human-input API |
| Cancel | existing abandon call, then RUN_FINISHED/RUN_ERROR | lossy: outcome is the recorded one | abandon API |
| Token usage | none in the enum; `CUSTOM` with `mctl.usage` references | no native mapping | usage ledger |
| Evidence/proposal refs | metadata/`CUSTOM` under `mctl.*` | lossy | gitops, evidence store |
| Audit/trace refs | metadata under `mctl.*` | lossy | audit log |

Does not fit: live Temporal phase (no API), per-token output, MCP invocation
coverage, subagent boundaries, usage. No event is invented for any of them.

## D3. Interrupt/resume and governance boundary

Path: a canonical `aar_` (or human-input `request_id`) already exists, created
by the existing path. The adapter reads it and emits `RUN_FINISHED` with
outcome `interrupt` whose `Interrupt.id` is the `aar_`/`request_id` and whose
`responseSchema` is closed (approve|reject plus the digest the user saw). The
client sends a new run with `resume[{interruptId, status, payload}]`. The adapter
does not decide: it forwards as the authenticated caller to
`DecideActionApproval` (or `RespondHumanInput`); `ConsumeActionApproval` stays
the single spend. A resume payload alone carries no authority.

| Case | Refused by | Status |
| --- | --- | --- |
| Stale digest or edited action | `prepareApprovalGrant` refuses a body-supplied digest; consume matches `intent_hash` and `expires_at`; an edit supersedes the pending grant `[CODE][DOC]` | existing |
| Duplicate / reconnect replay | consume is single-use (`WHERE state='approved'`) `[CODE]` per governance spike | existing; adapter resume must be idempotent on `aar_` (new check) |
| Wrong actor | `isHumanAdmin` (handlers_action_approvals.go:87) and self-decision refusal `[CODE]`; a service or relayed credential is refused | existing |
| Wrong tenant | principal tenant authorization (`PrincipalAuthz`) `[DOC]`; `canSeeActionApproval` | existing for approvals; adapter subscribe/run authorization is a required new check |
| Injected frontend event | adapter accepts only the closed resume schema; everything else is refused | required new check |
| Unknown `interruptId` | adapter lookup of canonical record | required new check |

Consequential actions stay in the governed operation and exact-action gate,
never in a frontend tool. Frontend tool classes: backend MCP (not a frontend
tool), safe UI-only (render, focus), consequential (re-routed to an `aar_`
interrupt, never executed client-side). Reasoning policy: REASONING_START,
REASONING_MESSAGE_*, REASONING_END: not emitted. REASONING_ENCRYPTED_VALUE: not
emitted. Only step-status summaries are shown; raw private chain-of-thought is
not a requirement. Tool output and model text are data: rendered escaped, never
parsed into control.

## D4. Ownership

| Shape | Assessment |
| --- | --- |
| Adapter in mctl-agents behind mctl-api | Workers deploy with `ingress.enabled: false` (`platform-gitops/services/admins/mctl-agents-worker/values.yaml`, also `-exec`, `-implement`) `[DEPLOYED]`. Public ingress on a worker is rejected. A proxy through mctl-api still duplicates identity. Rejected. |
| Adapter in mctl-api | mctl-api owns identity, tenant authorization, approvals and the execution read model `[CODE]`. Chosen. |
| Library only | Types without a server give no governed subscribe/resume path. Fallback if the adapter is rejected. |

Chosen shape, adapter in mctl-api, read-only SSE plus a resume that only
forwards to the existing handlers:

- Actor and tenant: resolved from the existing session or bearer; no new cookie scope, no new token. Authorization runs before subscribe, run, resume and cancel.
- CORS/CSRF: bearer-only for programmatic clients; the portal session keeps its existing protections; no wildcard origins.
- Replay: event ids are `<execution id>:<sequence>` derived from canonical records; `Last-Event-ID` rebuilds from records, not an adapter buffer. Following the `events/README.md` rule, the event is disposable and the source is canonical.
- Retention: bounded count and bytes per run, then a fresh STATE_SNAPSHOT.
- Backpressure: a slow consumer is dropped and reconnects to a snapshot.
- Correlation: `we_`, `wi_`, `aar_`, `trace_id` as references.
- Scaling: stateless, state in Postgres or Temporal.
- Owners: adapter and ingress mctl-api; client mctl-portal; schema pin `mctlhq/.github`.

## Q7. Frontend

Plain `@ag-ui/client` in mctl-portal for the experiment. CopilotKit is not
selected; revisit only if measured effort justifies it. mctl-web and a bare test
client are out of the first slice.

## Q8. Non-overlap

MCP is agent-to-tools; MCP Apps is tool-supplied UI inside an MCP host;
AG-UI is the run/event projection to the user; Temporal/Argo is durability.
Scenario: an `mctl_get_service_logs` result is recorded by the policy
checkpoint (single source). AG-UI shows a TOOL_CALL_RESULT reference to that
record. Any MCP App UI for it stays in the MCP host.

## Q9. Versioning

Pin `1.0.x` (`@ag-ui/core@1.0.3`). Advertise the version in `protocolVersion`
and an HTTP header. Clients ignore unknown event types and fields; the server
never emits them. Types are generated from the upstream schema at the pinned
commit. Custom data only under a closed `mctl.*` namespace (`mctl.usage`,
`mctl.refs`, `mctl.trace`), field count capped, every field a reference, never
a control, each justified by a D1 row.

## D5. Experiment design (Phase B, not authorized)

Slices: mctl-api read-only SSE plus resume forwarder; mctl-portal minimal
operator view with `@ag-ui/client`; mctl-agents none unless a needed fact is not
recorded. Fixture: owner-controlled issue on a throwaway repository.

| Proof | Named test | Pass condition |
| --- | --- | --- |
| P1 run lifecycle maps | `TestRunLifecycleProjection` | RUN_STARTED/FINISHED match the `we_` record |
| P2 stage steps | `TestStageSteps` | STEP pairs match stage records |
| P3 usage reference | `TestUsageRefOnly` | three cost fields stay separate, none summed |
| P4 interrupt from `aar_` | `TestInterruptFromApproval` | interrupt id equals `aar_` |
| P5 resume forwards | `TestResumeForwards` | decision happens only via `DecideActionApproval` |
| P6 stale resume | `TestResumeStaleDigest` | refused |
| P7 duplicate/replay | `TestResumeReplay` | second spend refused |
| P8 wrong actor/tenant | `TestResumeWrongActorTenant` | refused |
| P9 injected event | `TestInjectedEventRefused` | refused, no state change |
| P10 reconnect | `TestLastEventIDReplay` | rebuilt from records |

Measurements: events per run, bytes per run, reconnect latency, portal effort.

## D6. Verdict

`ADOPT_LIMITED_EVENT_SUBSET`

Evidence: run, step, approval, human-input and usage facts exist as durable
records `[CODE]`; no message or token stream and no streaming surface were
found at the pins, and live phase is not exposed through an API `[CODE]`.
Interrupt/resume can fail closed on existing checks (D3), with three small new
adapter checks. Adopted subset: RUN_*, STEP_*, TOOL_CALL_* for recorded calls,
STATE_SNAPSHOT/DELTA, interrupt/resume, and `CUSTOM` under `mctl.*`. Not
adopted: TEXT_MESSAGE_* streaming, SUBAGENT_*, REASONING_*.

Flips to `ADOPT_AG_UI_ADAPTER` if a token stream and event-granular tool
records are sourced. Flips to `REJECT_KEEP_CUSTOM` if interrupt/resume cannot
fail closed without a second authority, or if the unobserved items above show
the subset has no consumer.

## D8. Roadmap disposition

`KEEP_AS_AGENT_PLATFORM_FOLLOWUP`. No new epic; the owner decides.
