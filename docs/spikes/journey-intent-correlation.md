# Journey-level correlation across governed execution

Spike report for mctlhq/.github#306 (carrier DevLoop; refs only, not closing).
Proposed verdict: `REUSE_WORK_ITEM_ID`, which is a NO-GO on a new
`mctl.intent.id`. The owner has not recorded a decision, so the manifest
`roadmap/spikes/journey-intent-correlation.yaml` stays `lifecycle: proposed`
with no `decision` block.

## Non-claims and evidence tags

This report changes nothing in production. It adds no identifier, attribute,
propagation or sampler change and opens no implementation issue. Evidence tags: `[CODE]` = source read at a pinned revision; `[CONFIG]` = GitOps configuration, not the effective runtime environment; `[LIVE]` = authenticated MCTL read; `[GIT]` = proposal Git history; `[DOC]` = prior report, contract or tool description. `unknown (not observed)` must never be read as "absent" or "off". Only opaque ids and digests appear here: no
prompts, completions, message bodies, tool payloads or tokens.

## Evidence pins

| Repository | Pin (40-hex) | Used for |
| --- | --- | --- |
| mctlhq/.github | `b0a7e86b0179dba23d87e96161975137c53040db` | roadmap schema, prior spike reports, `observability` and governance epics |
| mctlhq/mctl-gitops | `bdaa0b73b1c3c363fe0f3111505e5f55e69de81e` | worker values (`MCTL_TRACE_*`) |
| mctlhq/mctl-api | `f72a470d1a6ac12eee8ad6223f633adf666dce49` | `internal/operations/execution_required.go`, `internal/api/handlers_dev_loop.go`, `docs/work-context-contract.md` `[CODE]` |
| mctlhq/mctl-agents | `a99c8881403f2bbe64fea9864d252bed4fe58a4d` | `orchestrator/temporal/activities/canonical_execution.py`, `orchestrator/temporal/workflows/dev_loop.py`, `orchestrator/tracing.py` `[CODE]` |
| mctlhq/mctl-docs | `fc218134c275896432ed0b326bca781d8dfd0768` | `docs/reference/telemetry-attributes.md`: same attribute key reserved with different *roadmap work-item* semantics `[CODE]` |
| mctlhq/mctl-telegram | `3cd42d1e320b6499d6a71adada9372fed408e416` | `internal/mcp/audit_span.go` emits edge request correlation, not proven WorkItem join `[CODE]` |

While the original investigator lacked access to live execution records, an operator-level follow-up on 2026-11-04 retrieved the WorkItem, execution, Temporal status and agent execution ledger; see **Live verification** below. The permission refusal is not an absence of runtime records.

## Deployed tracing baseline

- `services/admins/mctl-agents-worker/values.yaml`: `otel.enabled: true`,
  `MCTL_TRACE_WORKFLOW_TYPES: DevLoopWorkflow`, `MCTL_TRACE_ARGO_PARAM: "true"`;
  other workflow types stay unsampled; export to Tempo via the Collector.
  `[DEPLOYED]`
- `mctl-agents-worker-exec` and `mctl-agents-worker-implement` set the same
  variables; the traceparent crosses Argo as a declared workflow parameter. `[DEPLOYED]`
- `MCTL_SUBMIT_CREATES_EXECUTION`: effective production value **unknown (not observed)**; there was no `mctl-api` entry in the inspected GitOps services and a live service-config lookup for `admins/mctl-api` returned service-not-found. Source explicitly defaults to `off`, but this flag gates **submitter-created `we_` for direct Argo investigator submits**, not the canonical Temporal DevLoop's own WorkItem creation (see `ensure_issue_execution`). Never infer the deployed value from its default. `[CODE]`

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

Earlier `.github#182` documents P2 direct approve and P3 incident responder without a WorkItem `[DOC]`; current coverage remains **unverified**. Separately, `MCTL_SUBMIT_CREATES_EXECUTION=off` disables *submitter-created* executions on direct Argo investigate submits; it does **not** imply a modern Temporal DevLoop lacks `wi_`. The actual #306 Temporal run has one `[CODE] [LIVE]`.

## D2 lifecycle

- Mint: mctl-api is authoritative. A modern Temporal DevLoop invokes `ensure_issue_execution` to create/join the issue's tenant-scoped WorkItem and attach `(temporal, workflow_id#run_id)` as its canonical execution **independently of the direct-Argo submission flag**; a dispatched execution request instead brings its existing `wi_` and `we_`. An unavailable or refused WorkItem store stops the new Temporal loop. `[CODE]`
- Boundary: one delegated objective. A material amendment appends an intent
  entry and makes the existing grant stale (mctl-agents#701 supersede); a
  different objective is a new WorkItem.
- The identity activity retries idempotently for a given engine run. A later Temporal run gets a new `we_` under the same **open** WorkItem; if that WorkItem is already terminal, a new WorkItem may be minted. Do not claim unconditional `wi_` continuity across terminal items or automatic new `we_`/`cs_` for every `continue_as_new` without path-specific evidence. Content hashes remain the exact-binding layer. `[CODE]`
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
| Temporal | canonical new DevLoop runs `ensure_issue_execution` to resolve `wi_`, attach `we_`, and fails closed without identity; legacy paths unknown `[CODE]` |
| Argo CWFT | traceparent propagated as a parameter `[CONFIG]`; canonical Temporal path passes `work_item_id`/`execution_id` to investigate CWFT `[CODE]`; actual Tempo export not observed |
| runtime, MCP/tool, GitHub mutation | unknown (not observed) |
| durable evidence | audit rows carry `execution_id` and `grant_ref` `[DOC]`; join to `wi_` via execution |

Trust rule: only mctl-api asserts `wi_`. Downstream it is an opaque attribute
and never an authorization input; the authorizing binding is `intent_hash`.
Gaps and owners: flag value and ingress-id / trace-id recording (mctl-api);
attribute emission on DevLoop and CWFT roots (mctl-agents); catalog (mctl-docs).

## D4 OTel contract

- `mctl.work_item.id` already exists in `mctl-agents/orchestrator/tracing.py` and is documented on Argo/pod roots in `docs/observability/execution-traces.md`. But **the existing mctl-docs catalog reserves this same key for *roadmap work item* id**, example `attribute-catalog`, whereas the canonical runtime ID is `wi_<uuid>`. The observability owner must resolve this semantic collision before any broad rollout. Proposed placement after disambiguation: selected root/boundary spans (not every span); actual Tempo emission remains unverified. `[CODE]`
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
4. Did it finish: compare WorkItem state, canonical execution phase, **live Temporal status** and **GitHub PR state**. For #306, `we_` was `Succeeded` while WorkItem stayed `active`, DevLoop `Running` (shepherd active), and PR #307 `open`. In `dev_loop.py`, `_end_canonical_execution` **intentionally closes the canonical `we_` after the approval flip** before implementer and PR watch finish; `we_.phase` is not whole-journey completion. `[CODE] [LIVE]`

### Live verification: governed journey `.github#306` (2026-11-04)

Read-only, operator-level MCTL queries gave a concrete multi-stage example. These observations supersede the earlier illustrative-only sketch:

| Stage | Verified durable data | Evidence |
| --- | --- | --- |
| WorkItem | `wi_703fa13b-b693-4b4a-b7b9-59920ae68d14`; tenant `mctl`; external issue key `https://github.com/mctlhq/.github/issues/306`; created 2026-11-03 15:32:11Z; state `active` | `mctl_get_work_item` `[LIVE]` |
| Temporal canonical execution | `we_c2b5b1d4-024e-4b6f-a536-91d1aa7a32ed`; engine ref `dev-loop-mctlhq-.github-306#run_01a1a20a-111e-7c44-8572-2e83b2505063`; ended `Succeeded` 2026-11-04 00:56:10Z | `mctl_list_work_item_executions` `[LIVE]` |
| Investigate trace | `eaa404495b8377355b48bdf1d604c6ff` in the **historical** proposed `.status.yaml` blob `75a3557eda4ced5a67527c62265f4770c21650f1` | GitOps `[GIT]` |
| Approval boundary | `aar_ceca5b519bc444e494e8d481b6c64385`; content digest `sha256:177b2cd2246da531b51e21b1cd3092ce7f7a14343ebd46518ba075a89a4c8ce7`; approved 2026-11-04 00:54:59Z | GitOps `.status.yaml` `[GIT]` |
| Implement trace | **different** `ba4faa4a4a41d294655f13d6bb8eee55` in the implemented `.status.yaml`; implementation PR #307 opened | GitOps `[GIT]` |
| Cross-stage join | MCTL usage ledger grouped by `work_item_id` has investigator **and** implementer buckets under the same `wi_`; separate agent executions both `Succeeded` | API `[LIVE]` |
| Current task status | DevLoop `Running` (own shepherd active), PR #307 `open`, WorkItem `active`, although canonical `we_` already `Succeeded` | API + GitHub `[LIVE]` |

The investigation and implementation trace IDs are **observed run metadata on opposite sides of an observed human approval**, not fabricated placeholders. The approval CWFT's own trace ID and the presence of these traces in Tempo were **not** observed. The WorkItem currently lists **one canonical Temporal execution**, not one `we_` per agent step; the two stage records and usage rows are a separate ledger. `wi_` is correlation metadata, not proof of authorization.

## Verdict and follow-ups

Proposed **conditional** verdict: `REUSE_WORK_ITEM_ID` (NO-GO on `mctl.intent.id`), not yet an owner decision. The real #306 DevLoop demonstrates an actual `wi_` join; legacy and direct-submit coverage remain unverified. Reasons: `wi_` has
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

- mctl-docs: **resolve existing attribute-key collision** (`mctl.work_item.id` means roadmap work item in catalog, but canonical `wi_<uuid>` in runtime); align semantics before rollout.
- mctl-api: verify **effective production** `MCTL_SUBMIT_CREATES_EXECUTION` for direct Argo submits (do not conflate with Temporal's own canonical execution); record ingress request id
  and trace id on the intent or execution request; WorkItem for P2 / P3.
- mctl-agents: emit the attribute on DevLoop and CWFT roots.
- mctl-telegram: attach `wi_` to the #617 request correlation.

## Limitations

The code review and API read establish one modern Temporal DevLoop journey, **not a fleet-wide coverage percentage**. Effective production `MCTL_SUBMIT_CREATES_EXECUTION` is unknown; the direct-Argo path, legacy histories, P2/P3 paths and exported Tempo spans remain unverified. The existing mctl-docs `mctl.work_item.id` collision is a rollout blocker, not grounds to invent a second journey identity. No new runtime controls, labels, flags, collector settings or implementation issues are changed by this PR.

Owner decision and follow-up issue creation remain separate governance steps.
