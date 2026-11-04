# Journey-level correlation across governed execution

Spike report for mctlhq/.github#306 (carrier DevLoop; refs only, not closing).
Proposed verdict: `REUSE_WORK_ITEM_ID`, which is a NO-GO on a new
`mctl.intent.id`. The owner has not recorded a decision, so the manifest
`roadmap/spikes/journey-intent-correlation.yaml` stays `lifecycle: proposed`
with no `decision` block.

## Non-claims and evidence tags

This report changes nothing in production. It adds no identifier, attribute,
propagation or sampler change and opens no implementation issue. Every claim
carries one tag. `[CODE]` is read at a pinned commit. `[DEPLOYED]` is observed
through gitops or mctl tools. `[DOC]` is issue, proposal or tool-description
text. `unknown (not observed)` means this spike did not see it; it is never
read as "absent" or "off". Only opaque ids and digests appear here: no
prompts, completions, message bodies, tool payloads or tokens.

## Evidence pins

| Repository | Pin (40-hex) | Used for |
| --- | --- | --- |
| mctlhq/.github | `b0a7e86b0179dba23d87e96161975137c53040db` | roadmap schema, prior spike reports, `observability` and governance epics |
| mctlhq/mctl-gitops | `bdaa0b73b1c3c363fe0f3111505e5f55e69de81e` | worker values (`MCTL_TRACE_*`) |
| mctlhq/mctl-api | unknown (not observed) | WorkItem code was not in this clone; cited via `.github#182` report and MCP tool descriptions `[DOC]` |
| mctlhq/mctl-agents | unknown (not observed) | DevLoop tracing code not read; deployed env read from gitops |
| mctlhq/mctl-docs | unknown (not observed) | attribute catalog (#116) not read; whether `mctl.work_item.id` is catalogued is unknown (not observed) |
| mctlhq/mctl-telegram | unknown (not observed) | request correlation (#617) not read |

`mctl_list_agent_executions` and `mctl_get_dev_loop` for
`dev-loop-mctlhq-.github-306` were refused for the investigating agent, so no
live execution or DevLoop record for this journey was observed.

## Deployed tracing baseline

- `services/admins/mctl-agents-worker/values.yaml`: `otel.enabled: true`,
  `MCTL_TRACE_WORKFLOW_TYPES: DevLoopWorkflow`, `MCTL_TRACE_ARGO_PARAM: "true"`;
  other workflow types stay unsampled; export to Tempo via the Collector.
  `[DEPLOYED]`
- `mctl-agents-worker-exec` and `mctl-agents-worker-implement` set the same
  variables; the traceparent crosses Argo as a declared workflow parameter. `[DEPLOYED]`
- `MCTL_SUBMIT_CREATES_EXECUTION`: no match in the gitops checkout and no
  mctl-api values available, so the value is unknown (not observed). The tool
  contract says it defaults to off. `[DOC]`

## D1 identity map

| Candidate | Minter | Trust | Lifetime / cardinality | Spans multiple traces | Retry / resume | Reading |
| --- | --- | --- | --- | --- | --- | --- |
| `trace_id` | first instrumented root | propagated (W3C) | one trace; the approve CWFT was observed under its own id `[DOC]` (.github#182 1.4) | no, by definition | new trace per root | cannot be the journey key |
| ingress request correlation id | ingress (mctl-telegram#617, audit request id) | asserted | one request | no | new per request | child reference; not recorded on WorkItem intents `[DOC]` |
| `execution_id` (`we_`) | mctl-api | asserted | one execution | no | resume creates another `we_` `[DOC]` | child |
| Temporal workflow id / run id | DevLoop start | asserted | workflow id `dev-loop-mctlhq-{repo}-{issue}` is stable, including after abandon restart; run id changes `[DOC]` | yes | run id changes | secondary join key; DevLoop-only |
| `aar_` approval ref | mctl-api | asserted | one approval request | no | superseded on amendment | authorization record, never a correlation key |
| `intent_hash` / `artifact_hash` / proposal digest | derived | computed | exact content binding; covers `work_item_id`, `policy_version`, `artifact_hash` `[DOC]` | no | changes on amendment | not stable by design |
| WorkItem intent entry (integer id) | mctl-api, appended by a caller | asserted | one "what was asked" entry; "provenance, not approval" `[DOC]` | n/a | appended on amendment | child of the WorkItem |
| mctl-api#701 decision record | mctl-api | asserted | one decision, many per journey | n/a | n/a | child; not yet a store |
| `work_item_id` (`wi_`) | mctl-api | asserted, tenant scoped | holds many intents, executions (`we_`), requests (`xr_`), snapshots (`cs_`) across resumes `[DOC]` | yes | survives; each resume adds `we_` and `cs_` | candidate journey key |

Paths where `wi_` is absent today: P2 direct approve and P3 incident responder
have no WorkItem (.github#182 report, 9.1) `[DOC]`; a DevLoop started while
`MCTL_SUBMIT_CREATES_EXECUTION` is off is not recorded as an execution of a
WorkItem `[DOC]`, and the deployed value is unknown (not observed).

## D2 lifecycle

- Mint: WorkItem creation by the surface that accepts the delegation; for an
  issue, the create-or-open done by `mctl_trigger_issue` when the flag is on.
  Authoritative minter: mctl-api.
- Boundary: one delegated objective. A material amendment appends an intent
  entry and makes the existing grant stale (mctl-agents#701 supersede); a
  different objective is a new WorkItem.
- Retry, resume and `continue_as_new` keep the `wi_`; each adds a `we_` and a
  `cs_`. Content hashes remain the exact-binding layer.
- Parent/child: the WorkItem is the parent; executions, requests, snapshots,
  intents and approvals are existing children. No new id is needed.

Counterexample tests:

- Epic wave: already grouped by epic and plan hash; covered by an existing key.
- One Telegram request spawning issues in two repos: would be two WorkItems.
  No evidence was observed that a single query key across them is required;
  unknown (not observed). This is the only case that would justify
  `ADOPT_INTENT_ID`.
- P2 / P3 without a WorkItem: a gap in WorkItem creation (mctl-api), not
  evidence that a second id is needed.

## D3 propagation matrix

| Boundary | `wi_` state |
| --- | --- |
| ingress / MCP to mctl-api | asserted by mctl-api when an item is created or opened; request id not stored on intents `[DOC]` |
| decision / approval | bound into `intent_hash` `[DOC]`; absent on P2, P3 |
| Temporal | unknown (not observed) |
| Argo CWFT | traceparent propagated as a parameter `[DEPLOYED]`; `wi_` unknown (not observed) |
| runtime, MCP/tool, GitHub mutation | unknown (not observed) |
| durable evidence | audit rows carry `execution_id` and `grant_ref` `[DOC]`; join to `wi_` via execution |

Trust rule: only mctl-api asserts `wi_`. Downstream it is an opaque attribute
and never an authorization input; the authorizing binding is `intent_hash`.
Gaps and owners: flag value and ingress-id / trace-id recording (mctl-api);
attribute emission on DevLoop and CWFT roots (mctl-agents); catalog (mctl-docs).

## D4 OTel contract

- Attribute `mctl.work_item.id` on root and boundary spans only: ingress
  request, DevLoop root, each CWFT root, approval decision event, mutation
  span. Not on every span.
- Not a resource attribute (one process serves many WorkItems). Not W3C baggage
  on external calls (leak risk); internal baggage only if a boundary needs it.
- Cardinality: one value per WorkItem; fine as a span attribute and in TraceQL;
  never a metric label.
- Catalog owner: mctl-docs#116. Retention follows Tempo retention.
- Source of truth: durable records (audit events, `action_approval_requests`,
  WorkItem executions, `.status.yaml`), not sampled telemetry (mctl-api#701,
  .github#182).

## D5 sampling decision: DEFER

Baseline is workflow-type head sampling: `DevLoopWorkflow` always sampled,
other types unsampled. For the governed path this already keeps every DevLoop
trace, so journey-level coordination adds nothing now. Hash-of-`wi_`
deterministic sampling couples identity to the sampler and needs a custom
sampler in each producer. A per-WorkItem force-sample flag read at the next
root span is a candidate for debug and incident overrides. Privacy: ids only.
Cardinality and cost: spans added 0; one attribute on a bounded set of spans.

## D6 operator queries and worked journey

1. Everything from one delegation: WorkItem with intents, executions,
   snapshots, requests (durable); approvals with that `work_item_id` (durable);
   audit by `we_` (durable); Tempo search on `mctl.work_item.id` (sampled,
   optional).
2. Which decision authorized a side effect: audit row, then `we_` / `aar_`,
   then `intent_hash`, then policy rule and version. Durable only.
3. Traces, executions and retries: WorkItem executions, trace ids from submit
   annotations and audit, then Tempo (sampled, optional).
4. Did it finish: WorkItem state and latest execution phase. Caveat: the #702
   misprojection.

Worked journey for `.github#306`, one `wi_` throughout. All facts are
illustrative except where tagged; ids are placeholders.

| Step | Trace | Fact | Status |
| --- | --- | --- | --- |
| `mctl_trigger_issue`, DevLoop investigate | trace A (`aaaa...`) | proposal digest D1 | illustrative |
| `aar_` created, human decision | boundary | grant bound to D1 via `intent_hash` | illustrative |
| approve CWFT | trace B (`bbbb...`), separate from A | separate trace id observed in .github#182 1.4 | observed for the separate-trace behaviour; ids illustrative |
| implement | trace C (`cccc...`) | `we_` for the run | illustrative |
| optional reinvestigate | new root | digest D2 supersedes the grant | illustrative |

Traces A, B and C are distinct and sit on opposite sides of the approval
boundary; they share one `wi_`.

## Verdict and follow-ups

Proposed: `REUSE_WORK_ITEM_ID` (NO-GO on `mctl.intent.id`). Reasons: `wi_` has
the lifetime, minter, tenant scope, child model and hash binding needed (D1,
D2); a new id would be a second correlation scheme against .github#55 and would
collide with WorkItem intents and `intent_hash`. `ADOPT_INTENT_ID` would apply
only if a journey needing one key across several WorkItems, or starting where
no WorkItem can be created, is proven. `NO_CHANGE` does not apply: emission of
`mctl.work_item.id` and WorkItem coverage of P2 / P3 are unknown (not observed).

Candidate follow-ups, one owner each, none filed. Each must be duplicate
checked against mctl-api#285, mctl-agent#97, mctl-agents#195/#196,
mctl-telegram#617, mctl-docs#116 and mctl-api#701 before filing, after the
owner decides:

- mctl-docs: catalogue `mctl.work_item.id`.
- mctl-api: verify `MCTL_SUBMIT_CREATES_EXECUTION`; record ingress request id
  and trace id on the intent or execution request; WorkItem for P2 / P3.
- mctl-agents: emit the attribute on DevLoop and CWFT roots.
- mctl-telegram: attach `wi_` to the #617 request correlation.

## Limitations

mctl-api, mctl-agents, mctl-docs and mctl-telegram source and live DevLoop
records were not observed; claims about them are `[DOC]` or unknown. The
verdict should be re-checked once those are read.
