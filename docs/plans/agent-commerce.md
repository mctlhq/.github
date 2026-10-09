# Agent Commerce

Status: proposed for owner review, 2026-10-10. This is the canonical design and
issue-body draft; it does not create issues, change the active roadmap graph,
authorize implementation, or enable payments. After materialization,
`roadmap/epics/agent-commerce.yaml` will own the dependency graph; this document
will retain the architecture and acceptance rationale.

## Goal and first milestone

Enable MCTL to represent, meter, price and budget agent-accessible capabilities
inside its existing governed execution path. First milestone: **Paid Capabilities
with simulated credits**, visibly distinguished from real money. An enterprise
can use the same primitives for cost allocation without an external payment.

Vision: **MCTL is the commerce and governance layer for the agentic web.** This
is a direction, not a claim that commerce is implemented today.

Why now: capability discovery, policy checkpoints, action approvals and model
usage accounting already exist. The missing link is a durable economic
transaction around a governed capability invocation. Marketplace remains a
subsequent layer; the existing marketplace issue is not replaced.

## Live audit and evidence

Audit method: GitHub connector repository/issue discovery across `mctlhq/*`,
followed by authenticated GitHub REST issue and PR reads and fresh default-branch
checkouts. Search excerpts returned null states, so statuses below were read
from individual issue endpoints. Merged claims were checked on PR endpoints.
This is repository/roadmap evidence, not a production execution smoke test.

Pinned source revisions:

| Repository | Audited main revision | Responsibility |
| --- | --- | --- |
| mctlhq/.github | `fe96549ca3baafdbbdbf741810acc25b25fef564` | EpicDefinition and shared roadmap |
| mctlhq/mctl-api | `201cfe3df4b2935fa73cc7dc453d8028f60d5f5c` | identity, tenant auth, Postgres, approvals, usage, audit |
| mctlhq/mctl-agents | `ad99c321f8684b9f80532e71cb02809ffa0313b9` | capability gateway, policy, execution orchestration |
| mctlhq/mctl-gitops | `e5720911ac7f616aa1c3d3960959ecbbbf1b3283` | catalogs, profiles, hosting and edge configuration |
| mctlhq/mctl-portal | `3130ce3f5a34c02c1a3f050515c0b2eb31d66181` | Backstage human surface |

| Existing work | Live state | Reuse / limitation |
| --- | --- | --- |
| [Agent FinOps .github#48](https://github.com/mctlhq/.github/issues/48), [contract #49](https://github.com/mctlhq/.github/issues/49), [instrumentation #50](https://github.com/mctlhq/.github/issues/50) | closed, completed | `agent-finops.yaml` owns these; model costs, not customer charges |
| [mctl-api#266](https://github.com/mctlhq/mctl-api/issues/266) | closed, completed; [PR #344](https://github.com/mctlhq/mctl-api/pull/344) merged | durable usage ledger, replay dedupe and pricing-version pattern |
| [mctl-agents#242](https://github.com/mctlhq/mctl-agents/issues/242) | closed, completed; PRs [#485](https://github.com/mctlhq/mctl-agents/pull/485), [#508](https://github.com/mctlhq/mctl-agents/pull/508), [#509](https://github.com/mctlhq/mctl-agents/pull/509), [#513](https://github.com/mctlhq/mctl-agents/pull/513) merged | ADR-017 CapabilityDescriptor, ProviderRef, sealed set and gateway; owned by `agent-platform.yaml` |
| [mctl-agents#197](https://github.com/mctlhq/mctl-agents/issues/197), [#198](https://github.com/mctlhq/mctl-agents/issues/198) | closed, completed | runtime policy and durable approval orchestration |
| [mctl-api#366](https://github.com/mctlhq/mctl-api/issues/366), [#368](https://github.com/mctlhq/mctl-api/issues/368) | closed, completed; approval [PR #367](https://github.com/mctlhq/mctl-api/pull/367) merged | single-use action approvals; execution requests with leases and fencing |
| [mctl-api#373](https://github.com/mctlhq/mctl-api/issues/373) | closed, completed; [#382](https://github.com/mctlhq/mctl-api/pull/382), [#384](https://github.com/mctlhq/mctl-api/pull/384) merged | canonical principal and dual-written actor IDs |
| [mctl-api#428](https://github.com/mctlhq/mctl-api/issues/428) | closed, completed; [PR #431](https://github.com/mctlhq/mctl-api/pull/431) merged | execution-bound delegation grants |
| [Unified Identity .github#91](https://github.com/mctlhq/.github/issues/91); mctl-api [#374](https://github.com/mctlhq/mctl-api/issues/374), [#375](https://github.com/mctlhq/mctl-api/issues/375), [#376](https://github.com/mctlhq/mctl-api/issues/376), [#377](https://github.com/mctlhq/mctl-api/issues/377) | open | do not equate merged identity slices with complete principal-based tenant authorization; owned by `unified-identity.yaml` |
| [mctl-api#489](https://github.com/mctlhq/mctl-api/issues/489) | open | tenant role decisions for execution/approvals remain unresolved; rollout dependency |
| [Enterprise MCP .github#35](https://github.com/mctlhq/.github/issues/35) | open | `enterprise-mcp.yaml`; Cloudflare is an optional edge, not the canonical capability or policy authority |
| [mctl-agents#195](https://github.com/mctlhq/mctl-agents/issues/195), [discovery follow-ups #517](https://github.com/mctlhq/mctl-agents/issues/517) | open | tracing/discovery work remains; reuse correlation contracts without claiming full coverage |
| [Marketplace mctl-api#23](https://github.com/mctlhq/mctl-api/issues/23) | open | existing later-layer marketplace scope; no duplicate marketplace epic |
| [tenant domain separation mctl-gitops#1504](https://github.com/mctlhq/mctl-gitops/issues/1504) | open | tenant code must not share platform cookie/site trust |
| [site-deployer mctl-api#499](https://github.com/mctlhq/mctl-api/issues/499) | open | target is provider-owned MCP/auth; current tenant-specific proxy is explicitly temporary |

The audited issue records have no individual assignees. Owners below are proposed
repository responsibilities, not invented personal assignments. Existing items
remain in their existing epics and appear only as external dependencies here.

Searches covered identity, Enterprise MCP/Portal, apps.mctl.ai, hosting/destination,
billing, quotas, budgets, metering, usage, observability, policy, approvals, audit,
marketplace, registry, discovery, x402 and payments. No dedicated Agent Commerce
epic, capability billing ledger or apps.mctl.ai routing contract was found in the
searched issues and checked source trees; that is a scoped negative result, not
proof that no unpublished design exists. Newton MCP Gateway was inspected and is
an independent experimental physical-action project, not MCTL's platform gateway.

### Concrete execution and data seams

- [`capability_gateway.py`](https://github.com/mctlhq/mctl-agents/blob/ad99c321f8684b9f80532e71cb02809ffa0313b9/orchestrator/capability_gateway.py)
  implements search/describe/invoke. Invocation checks sealed-set membership,
  then PolicyCheckpoint, then dispatches to a remote or local provider and emits
  a hashed InvocationRecord. It does not persist a commerce transaction.
- [`run_issue_investigator.py`](https://github.com/mctlhq/mctl-agents/blob/ad99c321f8684b9f80532e71cb02809ffa0313b9/orchestrator/run_issue_investigator.py)
  constructs this gateway in the optional discovery mode. This is not a universal
  ingress for every external MCP caller. Commercial providers must enforce the
  transaction at their own protected entry point too; a client-side wrapper alone
  is not a payment boundary.
- [`capability.py`](https://github.com/mctlhq/mctl-agents/blob/ad99c321f8684b9f80532e71cb02809ffa0313b9/orchestrator/capability.py)
  already provides `mctl://<provider_type>/<provider_id>/<tool>` identity,
  ProviderRef, consequence class, schema hash and strict serialization. Extend
  this contract compatibly with a reviewed commerce reference. Do not repurpose
  advisory MCP annotations as authoritative pricing or create a second registry.
- [`usage/types.go`](https://github.com/mctlhq/mctl-api/blob/201cfe3df4b2935fa73cc7dc453d8028f60d5f5c/internal/usage/types.go)
  is model-specific: session/result/model dedupe, token counters, optional
  execution correlation, float64 calculated costs. `usage/pricing.go` prices
  models in USD per million tokens. Reuse durable ingest, validation and
  correlation patterns, not these monetary types or this dedupe grain as a
  customer balance. A sibling commerce ledger in the same service/Postgres is
  justified; a second FinOps service is not.
- `internal/workitems/action_approvals.go` binds one action hash to single-use
  approval. `execution_requests.go` provides claim/fencing patterns. Reuse both
  concepts without pretending a work-item execution ID identifies each tool call.
- `internal/auth/principal.go` still permits absent principal IDs after some
  resolution failures. New commerce endpoints must require resolved identity and
  tenant authority, fail closed, and never accept actor/tenant claims from a body.
- `internal/gitops/reader.go` has Tenant (members, resource quotas) and Service
  (team, name, host, port). Resource quotas are not monetary budgets. Backstage
  and Cloudflare Portal catalogs are views/integration surfaces, not a settlement
  authority.

## Recommended architecture

**Reuse:** capability/provider identity, sealed discovery, policy decisions,
canonical principals/delegation, action approval authority, execution correlation,
Postgres durability, existing audit and OTel pipeline.

**Add:** versioned commerce terms, invocation-level durable state, atomic spend
reservations, idempotent usage/finalization, simulated PaymentAuthorizer, and
permission-filtered commerce discovery/read APIs. Proposed storage belongs to
mctl-api; runtime integration belongs to mctl-agents and the selected provider
adapter. No billing microservice, workflow engine or public registry redesign.

Separate pricing, metering, entitlement, budgets, payment authorization and
settlement. A price is customer spend; model cost is provider expense. Never sum
both as one customer charge. Money uses integer minor units plus currency (or an
explicit fixed decimal asset scale at an adapter boundary), never binary float.
MVP uses one accounting currency per tenant; no FX or cross-currency aggregation.
Simulated balances and receipts carry `simulation` mode and cannot be redeemed.

MVP price modes: `free`, `per_call` (one accepted provider dispatch),
`per_successful_call` (one confirmed successful invocation). Unknown metadata is
not free; only legacy capabilities with commerce disabled keep legacy behavior.
A provider quote/terms snapshot pins capability version/schema hash, provider,
tenant, payer, currency, maximum amount, mode, pricing version and expiry.
Bind the exact action/argument digest when applicable. Discovery is informational;
execution must validate the selected terms. Price drift or expiry requires a new
quote and any affected approval. Do not silently charge the latest price.

```text
existing identity + CapabilityDescriptor + policy + approval + FinOps patterns
                                 |
                commerce terms + invocation ledger
                                 |
                   atomic budgets + simulation
                                 |
                governed provider execution + discovery
                                 |
                 Paid Capabilities evidence / rollout gate
                                 |
            optional external payment adapter (x402 / MPP)
                                 |
             existing marketplace work, payouts much later
```

Execution order: authenticate actor/subject and tenant → resolve capability and
current authorization → policy → immutable quote and preliminary budget check →
existing exact-action approval if required → revalidate identity/policy/quote →
atomically reserve budget → authorize payment → dispatch → record outcome →
capture or release → audit. Approval waits hold no funds. Budget exhaustion is a
hard deny in MVP; approval cannot override an exhausted hard limit. A later
budget-increase workflow is distinct from permission to perform the action.

The API transaction owns the reservation and idempotency uniqueness. For simulated
credits, reservation and authorization can commit together. For external rails,
use durable intents/outbox and reconciliation; a database transaction cannot
atomically commit a remote payment. Do not expose real rails before that is proven.
Approval consumption and dispatch also need a durable resume state: a consumed
approval linked to the same invocation can resume reconciliation, never authorize
a different invocation. Do not invent a second approval service.

Idempotency scope: tenant + authenticated actor + client key, with immutable
request/quote digest. Same key/same digest returns the original handle; changed
content conflicts. Server generates invocation ID. Provider operation and payment
adapter receive stable derived keys. Persist intent before dispatch and prevent
concurrent dispatch with fencing. A crashed dispatcher is not automatically safe
to replace: provider-side dedupe or outcome lookup is required after uncertainty.

### Failure and charging contract

| Case | Required result |
| --- | --- |
| auth/policy/budget/approval rejection | no provider dispatch, no charge; audit refusal |
| failure before provider accepts dispatch | release reservation, no charge |
| confirmed provider failure | per_call captures once if dispatch accepted; per_successful_call releases |
| confirmed success | capture exactly once using pinned price, including when client disconnected |
| timeout/crash after dispatch, ambiguous response | outcome_unknown; retain conservative pending liability, no blind re-execution; reconcile by provider key |
| retry or lost capture response | return original transaction; query adapter with same key, never issue a fresh charge |
| cancellation before dispatch | release; after dispatch, await confirmed cancellation/outcome |
| payment authorization denied | release budget reservation; no dispatch |
| settlement fails after successful execution | settlement_pending; retry/reconcile settlement only, never execute again |
| partial stream or long-running task | reject as unsupported before spending in MVP; later task adapters own chunk/final outcome semantics |

Bound reservation deadlines and operator-visible reconciliation deadlines are
required. Expiry of a lease does not prove that a provider did nothing; unresolved
liability must not become freely spendable again. Resolution records are auditable
and late receipts cannot cause a second charge. For per_call, an accepted dispatch
is billable even if its response fails, as disclosed in the quote.

Exactly-once accounting is required. Exactly-once effects are only claimed for a
provider with durable idempotency/outcome lookup and demonstrated failure tests.
For an arbitrary MCP server, MCTL must report uncertainty rather than promise
exactly-once execution. The pilot must use a controlled provider meeting that
contract; MCP request IDs alone are not business idempotency keys.

## Protocol research as of 2026-10-10

| Source | Verified finding | MCTL decision |
| --- | --- | --- |
| [Cloudflare Monetization Gateway](https://developers.cloudflare.com/monetization-gateway/) | Closed beta for APIs, MCP tools, sites and datasets; US buyer/seller eligibility | Potential edge adapter; not an MVP prerequisite. The slogan “Cloudflare monetizes knowledge, MCTL capabilities” is no longer a factual competitive distinction. |
| [Cloudflare x402](https://developers.cloudflare.com/monetization-gateway/x402/) | v2, exact/upto; PAYMENT-CONTEXT and PAYMENT-SETTLEMENT are vendor-specific origin headers | Keep origin verification separate from MCTL authorization. Payment does not grant capability access. |
| [Cloudflare validation](https://developers.cloudflare.com/monetization-gateway/configuration/payment-validation/) | Signed context, pinned JWKS; variable actual amount is sent before response headers; no settlement header on HTTP errors | Streaming final amounts and MCP errors carried in HTTP 200 need explicit adapter tests; do not infer billing success from HTTP status. |
| [Pay Per Crawl](https://developers.cloudflare.com/ai-crawl-control/features/pay-per-crawl/what-is-pay-per-crawl/) | Content/crawler payment product | Adjacent model, not a capability runtime or replacement for MCTL budget enforcement. |
| [x402 v2](https://github.com/x402-foundation/x402/blob/main/specs/x402-specification-v2.md), [auth-capture](https://github.com/x402-foundation/x402/blob/main/specs/schemes/auth-capture/scheme_auth_capture.md) | Schemes include exact, upto, batch-settlement and auth-capture; the latter separates authorization/capture/void/refund | x402 does not universally require immediate settlement. Select and pin a scheme/network/SDK/facilitator tuple, rather than assume all implement the same lifecycle. |
| [Coinbase SDK example](https://github.com/coinbase/cdp-sdk/blob/main/examples/typescript/x402/README.md) | exact/upto examples cover Base and Solana; auth-capture example is explicitly client-only/mock, not end-to-end settlement | Specification availability is not deployed facilitator support. A real adapter must prove authorize/capture/void/refund and ambiguous retries on its exact versions. |
| [Stripe machine payments](https://docs.stripe.com/payments/machine) | MPP card/SPT and stablecoin support; x402 Base/USDC; MPP Tempo/USDC.e and Solana/USDC; refunds and sessions documented | Evaluate MPP alongside x402. Card minimums differ from micropayment units; no raw card data in MCTL. Business eligibility remains a real-integration gate. |
| [AP2 authorization](https://ap2-protocol.org/ap2/agent_authorization/) | Signed delegated mandates and action authorization | Potential proof adapter for delegated spend. Do not replace MCTL principal/tenant/policy decisions or import checkout entities into the core. |

Wallet identity is not agent identity. Bind a validated payment proof to the
server-authenticated payer, tenant, invocation and quoted terms. Future invoice
and enterprise chargeback authorizers can implement the same domain operations
without blockchain identifiers. Processor settlement is not provider payout or
revenue sharing. API marketplace catalogs can publish offers later; they do not
supply authoritative execution evidence for this pilot.

Unproven here: actual Cloudflare account eligibility, supported network set in the
user's facilitator account, end-to-end MCP client retry behavior, streaming billing,
refund behavior after an ambiguous execution, and processor settlement under
outage. These are adapter acceptance tests, not claims of current compatibility.

## apps.mctl.ai recommendation

Choose **Option A: a future platform-owned discovery UI/manifest surface**, with
explicit runtime endpoints elsewhere. Preserve the desired human address
`apps.mctl.ai/foo` without making it the canonical execution or identity key.
No existing contract for that host was found. Do not proxy arbitrary tenant HTML
or code under a shared platform origin. #1504 makes the isolation concern concrete;
#499 favors provider-owned execution. A future reverse proxy needs a separate
origin/auth/cookie/routing ADR. No new domain or route is provisioned by this epic.

## Proposed epic issue body

Title: **Agent Commerce**. Home: **mctlhq/.github**. Technical owner: mctl-api,
with mctl-agents runtime ownership. Priority recommendation: P0 for the five
foundation items below; P1 for protocol evaluation. Project fields remain
Project-owned and are not changed merely by this recommendation.

Goal: a governed capability can declare a price, and an authenticated agent can
consume it under policy, exact-action approval and an atomic budget, with durable
usage and deterministic charging. First milestone uses simulated credits.

Non-goals: marketplace UI, new registry, wallet product, real prepaid stored value,
provider onboarding, payouts/revenue share, tax/VAT engine, subscriptions engine,
FX, auctions, outcome pricing, arbitrary smart contracts, long-running or streamed
paid execution, production enablement by merely merging this roadmap.

Done when the five required children below are completed with evidence. Discovery,
spend visibility and failure/recovery tests are required, not optional finishing
work. The protocol spike may close with a reasoned defer decision. Existing
marketplace #23 follows successful foundations; it does not block this epic.

## Child issue drafts and acceptance

### C1 — Define capability commerce terms and execution contract

Repository: mctl-api. ID: `commerce-contract`. Required. Dependencies: existing
mctl-agents#242, mctl-api#266, #373 and #428 (all completed at audit).

Problem: capability descriptors and model usage records cannot currently express
an immutable customer charge or payer-bound invocation. Author a reviewed
cross-repository contract extending ADR-017 and reusing ADR-012's attribution rules.

Acceptance:
- Specify canonical capability/provider references, principal/tenant/payer binding,
  currency and integer amounts, versioned prices, immutable quotes and expiry.
- Define free/per_call/per_successful_call and every failure case in this plan;
  execution success, metered usage, payment status and settlement remain distinct.
- Define idempotency, provider capability requirements, durable state transitions,
  migration/backward compatibility and recovery ownership.
- Define PaymentAuthorizer authorize/capture/void/status contract, adapter feature
  negotiation and future refund records without a blockchain dependency.
- Define required discovery/read fields and authorization; signed payment proof
  is never an authorization grant. No production schema/API contract is implied
  by this planning document before C1 review.

### C2 — Persist invocation usage and economic state durably

Repository: mctl-api. ID: `invocation-ledger`. Required. Depends on C1.

Problem: InvocationRecord traces cannot serve as a durable billing source of truth.
Add sibling commerce persistence in the existing API/Postgres boundary.

Acceptance:
- Required actor, subject, tenant, capability/version, provider, invocation and
  execution IDs; pinned price, meter/quantity, timestamps and separate outcomes.
- Server-authenticated writers, tenant-scoped reads, no payload/payment secrets;
  no spoofed caller identity or provider-supplied unauthenticated finalization.
- Unique operation keys, conflicting replay rejection, transactional transition
  history and finalization; append-only monetary adjustments.
- Crashes, duplicated/late events and out-of-order delivery preserve one usage
  result/charge; unresolved outcomes remain queryable and reconcilable.
- Regression tests retain the existing model ledger contract; estimates are not
  mislabeled as charged money. Audit/outbox failure cannot silently lose a charge.

### C3 — Enforce atomic tenant budgets with simulated authorization

Repository: mctl-api. ID: `budget-authorizer`. Required. Depends on C2.

Problem: concurrent requests can overspend a budget if checks and deductions are
separate. Start with tenant monthly budgets in UTC, plus a per-request maximum;
agent daily budgets are deferred until tenant foundations and identity rollout.

Acceptance:
- Atomic committed-plus-reserved comparison and reservation under concurrency;
  durable capture/release/status using the same invocation key.
- Simulation mode is explicit and non-redeemable; no real-money top-up endpoint.
- No funds held during human approval; after approval recheck available budget.
- Month rollover attributes captures to their reserved period; unresolved holds
  cannot reset into spendable balances. Currency mixing is refused.
- Denial/timeout/replay/authorization-failure tests show no double spend or release;
  unknown remote outcomes require reconciliation, not blind lease expiry.
- Budget change authorization and audit are explicit. No self-granted limits;
  missing canonical identity fails closed at commerce boundaries.

### C4 — Integrate commerce into governed capability invocation

Repository: mctl-agents. ID: `governed-invocation`. Required. Depends on C3 and
existing mctl-agents#197/#198, mctl-api#366.

Problem: adding a payment path beside the gateway could bypass governance. Extend
the existing invocation seam and use a controlled read-only MCP pilot provider
with durable request dedupe/outcome lookup. mctl-api remains commerce authority.

Acceptance:
- Sealed-set eligibility, actor/tenant authorization and policy precede execution;
  changed/revoked permission is rechecked after approval and before dispatch.
- Quote/args binding, one-use action approval, budget reservation and payment
  authorization form one resumable invocation; a consumed approval cannot be
  replayed for another action or lose the original invocation on restart.
- Provider enforces the authorized transaction at its ingress; direct endpoint,
  eager-mode and alternate-adapter attempts cannot bypass the paid capability gate.
- Existing free capabilities preserve behavior. Paid metadata with unknown terms
  fails closed; unsupported stream/task execution is rejected before reservation.
- Deterministic fault tests cover concurrent retry, crash before/after dispatch,
  disconnect, provider error, late result, authorization/capture uncertainty and
  approval expiry/denial. No new workflow engine or production payment rail.

### C5 — Expose commerce discovery and prove Paid Capabilities

Repository: mctl-agents (cross-repo API/GitOps changes referenced from this issue).
ID: `paid-capabilities-proof`. Required. Depends on C4 plus mctl-api#377/#489 for
shared-tenant rollout. Local simulation development does not wait for the entire
Unified Identity epic; shared-tenant enablement does wait for these concrete gates.

Acceptance:
- Existing authorized search/describe exposes versioned price, currency/unit,
  payment mode, quote requirements and approval/risk information. Invisible
  capabilities and other tenants' balances remain invisible.
- Tenant-scoped API/MCP reads expose available/reserved/committed amounts and a
  receipt joined to the invocation, execution, policy and approval evidence;
  existing surfaces can consume these without a marketplace UI.
- Demo: simulated EUR 0.12 per_successful_call; EUR 1.00 budget admits at most
  eight successes under concurrency. Ninth is denied; failed call releases,
  repeated key does not re-execute or re-charge, changed payload conflicts.
- Demonstrate direct-ingress bypass denial, cross-tenant denial, permission
  revocation, pending/denied approval, restart recovery and settlement-only retry.
- Bounded-cardinality metrics reuse OTel conventions; IDs stay in audit/traces,
  not metric labels. Ledger survives telemetry loss/retention expiry.
- Pin tested repo versions and provider contract, attach reproducible evidence,
  document rollout/disable procedure; disablement stops new paid requests while
  allowing reconciliation. No outstanding unexplained liability in the demo.

### C6 — Evaluate x402/MPP adapter compatibility

Repository: mctl-api. ID: `payment-interop`. Optional. Depends on C5.

Acceptance: pin SDK/protocol/scheme/network/facilitator versions; prove test-mode
challenge/retry, identity binding, replay rejection, authorize/capture/void/refund,
settlement ambiguity and MCP error handling. Explicitly test or reject streaming
and long-running calls. Compare x402 and MPP against the same PaymentAuthorizer
contract and enterprise chargeback. Record eligibility, secrets custody and fee
constraints. Conclude adopt/defer; any production integration requires separately
reviewed scope. No stored card data, live funds, payouts or merchant onboarding
are authorized by this issue.

## Proposed EpicDefinition

The existing schema requires a real root issue number. `EPIC_ISSUE_NUMBER` below
is an explicit template marker, **not a schema-valid number and not a live issue**.
Do not place this draft in `roadmap/epics/` until root creation is approved and its
number is bound. Child items can legally be unbound with title/owner. No schema
change or fake GitHub binding is needed.

```yaml
apiVersion: roadmap.mctl.ai/v1alpha1
kind: EpicDefinition
metadata:
  name: agent-commerce
  owner: mctl-api
  labels: [roadmap]
spec:
  title: Agent Commerce
  goal: >-
    Enable governed capabilities to declare a price and be consumed under policy,
    approval and atomic budgets, with durable usage and simulated payment first.
  lifecycle: proposed
  github:
    issue:
      repository: mctlhq/.github
      number: EPIC_ISSUE_NUMBER
    issueType: Epic
  phases:
    - id: foundation
      title: Commerce contract and durable accounting
    - id: execution
      title: Governed Paid Capabilities with simulated credits
    - id: interoperability
      title: External payment compatibility
  workItems:
    - id: commerce-contract
      title: Define capability commerce terms and execution contract
      owner: mctl-api
      phase: foundation
      required: true
      externalDependsOn:
        - {repository: mctlhq/mctl-agents, number: 242}
        - {repository: mctlhq/mctl-api, number: 266}
        - {repository: mctlhq/mctl-api, number: 373}
        - {repository: mctlhq/mctl-api, number: 428}
    - id: invocation-ledger
      title: Persist invocation usage and economic state durably
      owner: mctl-api
      phase: foundation
      required: true
      dependsOn: [commerce-contract]
    - id: budget-authorizer
      title: Enforce atomic tenant budgets with simulated authorization
      owner: mctl-api
      phase: foundation
      required: true
      dependsOn: [invocation-ledger]
    - id: governed-invocation
      title: Integrate commerce into governed capability invocation
      owner: mctl-agents
      phase: execution
      required: true
      dependsOn: [budget-authorizer]
      externalDependsOn:
        - {repository: mctlhq/mctl-agents, number: 197}
        - {repository: mctlhq/mctl-agents, number: 198}
        - {repository: mctlhq/mctl-api, number: 366}
    - id: paid-capabilities-proof
      title: Expose commerce discovery and prove Paid Capabilities
      owner: mctl-agents
      phase: execution
      required: true
      dependsOn: [governed-invocation]
      externalDependsOn:
        - {repository: mctlhq/mctl-api, number: 377}
        - {repository: mctlhq/mctl-api, number: 489}
    - id: payment-interop
      title: Evaluate x402 and MPP adapter compatibility
      owner: mctl-api
      phase: interoperability
      required: false
      dependsOn: [paid-capabilities-proof]
  completion:
    mode: allRequired
  successCriteria:
    - A capability exposes immutable versioned commerce terms through authorized discovery.
    - Canonical identity, tenant authorization, policy and exact-action approvals remain authoritative.
    - Concurrent invocations cannot overspend an atomic tenant budget.
    - Simulated authorization, capture and release are durable and idempotent.
    - Usage, outcome, payment and audit evidence correlate to one invocation and execution.
    - Retries never double-charge; uncertain provider effects are reconciled without blind re-execution.
    - One controlled provider proves the full paid-capability lifecycle and bypass denial.
```

## Materialization and review gate

The current [roadmap workflow](../../roadmap/README.md#roadmapproposal-integration)
says: “A model can influence the GitHub graph only by proposing manifest text that
a human merges”. This proposal therefore stops before creating an issue graph.
The existing schema requires the epic root to pre-exist, and the current apply
engine explicitly does not create issues. That bootstrap limitation must be
handled openly, not hidden with an invented issue ID or a schema relaxation.

After owner review of this concrete proposal:
1. Re-read relevant issues/PRs and repeat duplicate searches; incorporate changes.
2. With explicit approval for issue creation/bootstrap, create the root and six
   child issues from the bodies above without implementation-triggering labels.
   Record returned IDs; partial failures are resumed by existing URLs, not by
   re-creating issues.
3. Move the YAML to `roadmap/epics/agent-commerce.yaml`, bind actual root/children,
   validate the full corpus, and remove this embedded draft to leave one graph.
4. Human-review/merge the exact manifest through the existing PR gate. Do not
   merge it as a side effect of accepting the conceptual architecture.
5. Use governed plan/apply for native parent and blocked-by edges, with the
   reviewed manifest hash and audit evidence. External dependencies are not owned
   or edited. Keep optional interop separate from required completion.
6. Re-read all issue states, parent/child and blocked-by edges; run live reconcile,
   health and ready checks. Report links, actual counts, required/optional flags,
   duplicate results and remaining blockers. Do not claim completion from YAML
   validation alone, and do not launch implementation or enable real payments.

## Proposal validation

2026-10-10: parsed the embedded YAML and confirmed that its only schema error is
the intentional `EPIC_ISSUE_NUMBER` marker. A temporary, explicitly synthetic
copy with a numeric root placeholder passed the repository's schema, semantic
and full-corpus validator alongside all 17 existing epics. That synthetic binding
was never sent to GitHub or committed. This validates graph structure, not live
relations or a materialized EpicDefinition. No runtime implementation was changed.
