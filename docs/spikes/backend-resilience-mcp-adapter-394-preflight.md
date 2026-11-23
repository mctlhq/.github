# Backend Resilience & MCP Adapter — source-pinned audit preflight

**Date:** 2026-11-23  
**Parent:** [mctlhq/.github#394](https://github.com/mctlhq/.github/issues/394)  
**Classification:** Research-only evidence inventory and **review candidate**, not the completed cross-repo gap analysis.  
**Decision state:** `REVIEW_PENDING`. There is **no** proposal approval, implementation, merge, deployment, provider linking or feature activation authority in this document.

## Purpose and independent review request

Before considering a DevLoop issue-investigator (which writes an OpenSpec triplet immediately), ask independent Claude and Agy PR reviewers to evaluate whether this bounded research intake is well formed, source-pinned, truthful about evidence and safe for a *subsequent separately authorized* investigator. This is a **preflight review**, not the final independent review of the investigator's full gap report or an ADR-025 autonomous reviewer receipt.

Review for: falsely promoted planned behavior, erroneous references, missing high-risk negative cases, duplicate owners, an implementation-shaped task disguised as research, inappropriate retry/compensation of writes, OAuth/security authority shifts, and unjustified shared-adapter extraction. Record findings by severity P1/P2/P3. Unknown/unrun evidence must never count as PASS. This PR is deliberately **not a proposal** and must **not be merged automatically**.

## Exact source revision set

| Repository | Main commit (full SHA) | Scope |
| --- | --- | --- |
| `mctlhq/mctl-api` | `6d01897380d1654113602e46f1d40ff137de3b18` | MCP WorkItems, Glovo error registry, Postgres outbox tests |
| `mctlhq/mctl-agents` | `eec4fdd865491d89d221a2bd2b8ed40fab65ef23` | Temporal worker queues and admitted implementation capacity |
| `mctlhq/mctl-telegram` | `ccaa39212261b58e1358f977d1bed7361f9f5b9d` | PA connector and proposed governance ADR |
| `mctlhq/.github` | `60e9db522f18299e73dac23fffedfe5b4ce81930` | Intake/reviewer workflow baseline |

Source status must be assessed at these revisions. Repository code is not evidence of a deployed configuration. Search indexing on `mctl-api` was observed referencing a different earlier commit; claims below were manually checked with `fetch_file` at the pinned revision, not inferred from search results. No CI commands or production failure tests have been run as part of #394.

## Source-pinned *presence* observations, not production verdicts

| Track | Evidence at exact SHA | Evidence class | Still unknown / falsification needed |
| --- | --- | --- | --- |
| A. Provider resilience | [PA dispatcher](https://github.com/mctlhq/mctl-telegram/blob/ccaa39212261b58e1358f977d1bed7361f9f5b9d/internal/pa/connector/dispatch.go#L16-L52) documents server-side tool/scope/flag/connection checks; [DefaultCallTimeout](https://github.com/mctlhq/mctl-telegram/blob/ccaa39212261b58e1358f977d1bed7361f9f5b9d/internal/pa/connector/dispatch.go#L145-L150) is 60s. [Glovo error registry](https://github.com/mctlhq/mctl-api/blob/6d01897380d1654113602e46f1d40ff137de3b18/internal/glovo/errors.go#L29-L86) includes `rate_limited` and `unavailable` classifications. | Code present | Effective LLM/MCP provider timeouts, 429 retry behavior, retry budgets, provider isolation, circuit breaker, quota semantics and operational measurement; do not interpret lack of search hits as lack of control. |
| B. Workflow reliability | [mctl-api outbox Postgres fixture](https://github.com/mctlhq/mctl-api/blob/6d01897380d1654113602e46f1d40ff137de3b18/internal/bridge/outbox_pg_test.go#L26-L83) sets up isolated schema and explicitly skips when `TEST_DATABASE_URL` is absent. [Temporal constants](https://github.com/mctlhq/mctl-agents/blob/eec4fdd865491d89d221a2bd2b8ed40fab65ef23/orchestrator/temporal/constants.py#L10-L95) define control, execution and implementation task queues, with documented admission width/mutex considerations; [ADR-008](https://github.com/mctlhq/mctl-agents/blob/eec4fdd865491d89d221a2bd2b8ed40fab65ef23/docs/adr/008-worker-queue-split-and-capacity.md) says accepted. | Code + tests + ADR present | Whether real-Postgres tests run as mandatory CI, duplicate callbacks and crash recovery are tested, current deployed worker roles match code, and per-provider backpressure prevents starvation under load. |
| C. MCP portability | [PA `MCPEndpoint` interface](https://github.com/mctlhq/mctl-telegram/blob/ccaa39212261b58e1358f977d1bed7361f9f5b9d/internal/pa/connector/dispatch.go#L123-L135) offers Server/ListTools/CallTool; [Glovo error registry](https://github.com/mctlhq/mctl-api/blob/6d01897380d1654113602e46f1d40ff137de3b18/internal/glovo/errors.go#L29-L117) maps stable domain codes to HTTP status; [MCP WorkItem tools](https://github.com/mctlhq/mctl-api/blob/6d01897380d1654113602e46f1d40ff137de3b18/internal/mcp/workitems.go#L30-L82) delegate identity/tenant/policy to canonical REST. | Distinct existing adapters and domain contracts | Whether semantic duplication is measurable, whether provider-neutral error mapping is necessary, actual OAuth/transport/capability compatibility with Grok Bot, and whether shared library would cross privilege boundaries. |
| D. Governance enforcement | [PA connector executor](https://github.com/mctlhq/mctl-telegram/blob/ccaa39212261b58e1358f977d1bed7361f9f5b9d/internal/pa/connector/execute.go#L15-L110) has pre-dispatch audit/refusal and `tool_error`/`outcome_unknown` classes. [PA dispatcher](https://github.com/mctlhq/mctl-telegram/blob/ccaa39212261b58e1358f977d1bed7361f9f5b9d/internal/pa/connector/dispatch.go#L16-L48) explicitly documents read-tool checks and [write flag inert for Phase P2-S1](https://github.com/mctlhq/mctl-telegram/blob/ccaa39212261b58e1358f977d1bed7361f9f5b9d/internal/pa/connector/dispatch.go#L107-L118). [PA ADR 0011](https://github.com/mctlhq/mctl-telegram/blob/ccaa39212261b58e1358f977d1bed7361f9f5b9d/docs/adr/0011-personal-assistant.md) is **Proposed**, expressly not phase authorization. | Mixed implemented read-path code and proposed write-path design | Effective production gates, MFA receipt binding, revocation races, all negative authorization cases, audit integrity during outage and exact payload hash handling across actual writes. |

**Important:** A missing executed test, deployment proof or source path is an `UNKNOWN`, not an identified P1/P2 production defect. Conversely a complete-looking design or test file is not `PRODUCTION_PROVEN`.

## Audit contracts for a bounded Investigator

### A — Provider Resilience
Inventory each actual outbound provider and capture policy, runtime and tests for `timeout`, `retry-after`/429, 5xx, jitter, budget, 503 fail-closed, limited-per-provider concurrency, circuit open/half-open, and cancellation. Design offline synthetic tests for slow-provider starvation, retry storms and quota exhaustion. A failed write never becomes an automatic retry unless an explicit idempotency/known-effect contract permits it; distinguish call never sent from outcome unknown.

### B — Workflow Reliability
Map operation key and payload digest scope, duplicate call/refused mismatch, durable outbox transaction, lease fencing, Temporal retries, Argo submission admission and unknown results. Synthetic/fault-injection matrix: crash after commit before publish; crash after remote effect before receipt; lease expiry and concurrent leasers; repeated callback; revocation after approval before dispatch; double tap; restart; queue burst. A Saga/Compensation is a possible pattern, not permission to reverse externally irreversible operations.

### C — MCP Portability / Adapter
Compare **three options** against existing code: (1) keep per-service adapters, (2) introduce a thin shared *semantic type/contract*, (3) introduce a shared runtime gateway. Options 2/3 require demonstrated duplicated logic, measured compatibility benefit, explicit lifecycle ownership and a negative security case that is not made worse. The adapter cannot hold a second trust/policy/credential authority; normalize presentation, not authorization.

Separate native MCP transport/OAuth (audience/resource/PKCE, discovery, registration, revocation, scope ceiling) from **MCP Apps host UI** capability, A2UI and AG-UI projections. For external Grok Bot, classify `documented`, `fixture-tested`, `real-host-tested`; no compatibility claim from documentation alone. Read-only synthetic accounts only; no account linking or private messages in this audit.

### D — Governance Enforcement
Audit read/query vs write/command contracts (semantic CQRS; no forced two-database implementation), server-derived principal/team, exact argument digest, approval receipt/MFA, consent, deny/revoke/fence, audit append and result classification. Negative tests: cross-tenant, missing scope, expired grant, unauthorized tool, stale connection revision, replayed approval, changed schema/description hash, double-dispatch and failure of audit sink. Deployment/phase GO is independent from code merge.

## Existing issue owners — avoid duplicating

- Bridge/outbox/fencing: [mctl-api#962](https://github.com/mctlhq/mctl-api/issues/962), [#970](https://github.com/mctlhq/mctl-api/issues/970), [#985](https://github.com/mctlhq/mctl-api/issues/985).
- Tenant lifecycle idempotency spike: [mctl-api#981](https://github.com/mctlhq/mctl-api/issues/981).
- Execution admission/backpressure: [mctl-agents#395](https://github.com/mctlhq/mctl-agents/issues/395), [#396](https://github.com/mctlhq/mctl-agents/issues/396), [#418](https://github.com/mctlhq/mctl-agents/issues/418).
- PA approval/read/write ownership: [mctl-telegram#1320](https://github.com/mctlhq/mctl-telegram/issues/1320), [#1379](https://github.com/mctlhq/mctl-telegram/issues/1379), [#1398](https://github.com/mctlhq/mctl-telegram/issues/1398).
- Agent-native/host portability: [.github#393](https://github.com/mctlhq/.github/issues/393), [.github#376](https://github.com/mctlhq/.github/issues/376); optional Dapr adapter [.github#314](https://github.com/mctlhq/.github/issues/314), MCP Events [.github#377](https://github.com/mctlhq/.github/issues/377).

## Governance and stage distinction

`mctl-agents/config/settings.py` [at pinned SHA](https://github.com/mctlhq/mctl-agents/blob/eec4fdd865491d89d221a2bd2b8ed40fab65ef23/config/settings.py#L94-L112) includes `.github` as a non-rotating supported investigator target, so the target is valid **in source**; this does not establish deployed service registry version. Existing [mctl-agents#1114](https://github.com/mctlhq/mctl-agents/issues/1114) and [#1115](https://github.com/mctlhq/mctl-agents/issues/1115) track deterministic unknown-service and investigator output failure handling: inspect actual release/workflow before starting.

The canonical independent `review-worker` is **not assumed live**: [mctl-agents#986](https://github.com/mctlhq/mctl-agents/issues/986) still has [draft PR #990](https://github.com/mctlhq/mctl-agents/pull/990) at preflight; [ADR-025](https://github.com/mctlhq/mctl-agents/blob/eec4fdd865491d89d221a2bd2b8ed40fab65ef23/docs/adr/025-independent-review-receipts.md) says no consumer uses its review gate yet. Conventional independent Claude/Agy PR reviews on this report are **advisory input**, not an ADR-025 receipt or `GO_IMPLEMENT`.

**Gate R0 — this PR:** independent reviewer must explicitly validate the **research intake/preflight only** on this exact HEAD and classify open findings. If P1/P2 remain, revise this document and repeat review on new HEAD. A clean R0 can justify a separate `GO_RESEARCH_INVESTIGATOR` authorization on issue #394, provided its payload is revised to clarify that the default issue investigator immediately emits an OpenSpec triplet and that the triplet is **research/report-only**, never implementation permission.

**Gate R1 — after investigator:** a full evidence matrix with reproducible test references, provenance, status and severity is reviewed independently at its fixed SHA/digest. This is the actual audit acceptance, not R0. A proposal candidate can be considered only for proven gaps after R1 and a fresh owner decision.

**Hard HOLD:** no `agents:intake`, manual DevLoop start, issue investigator, acceptance, implementation, merge or release until their specific gate passes. This PR must not be auto-merged. If reviewer failed to run, timed out, or returned no verdict, record `REVIEW_UNOBSERVED` — not `PASS`.

## Independent reviewer checklist

- [ ] Source URLs resolve to the stated SHA and actually support every presence claim.
- [ ] No proposed behavior is presented as deployed.
- [ ] Failure matrix covers unknown remote side effect, replay, cross-tenant/approval, and provider saturation.
- [ ] Adapter is optional; no policy, credential or workflow authority duplication.
- [ ] R0 intake review is not misrepresented as final R1 evidence review or autonomous ADR-025 review.
- [ ] Existing owners retained; no irreversible actions or implicit implementation authorization.
- [ ] Review has concrete actor, timestamp, head SHA, findings/severity, and non-silent failure handling.
