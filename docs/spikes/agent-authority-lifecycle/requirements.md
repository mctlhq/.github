# Agent Authority Lifecycle — requirements (PROPOSED)

Issue: https://github.com/mctlhq/.github/issues/375
Status: research only; no owner GO, implementation, agents:intake, merge, or production changes.

## Source pins (retrieved 2026-11-22)
- .github#375 https://github.com/mctlhq/.github/issues/375 (open, spike:scout, no comments at retrieval)
- .github#181 https://github.com/mctlhq/.github/issues/181 (active decision-assurance owner)
- .github#151 https://github.com/mctlhq/.github/issues/151 (adversarial regression owner)
- mctl-api#700 https://github.com/mctlhq/mctl-api/issues/700 (closed, authority/freshness contract)
- mctl-api#701 https://github.com/mctlhq/mctl-api/issues/701 (closed, durable decision/attempt contract)
- mctl-api#901 https://github.com/mctlhq/mctl-api/issues/901 (open, planning-only delegation enrollment)
- mctl-agents#703 https://github.com/mctlhq/mctl-agents/issues/703 (closed, execution authorization semantics)
- mctl-agents#752 https://github.com/mctlhq/mctl-agents/issues/752 (open, observe-only run-token migration)
- .github#356 https://github.com/mctlhq/.github/issues/356 (deferred gateway cache; proposed 30s soft / 300s hard / 5s refresh)
- AGENTS.md blob 7771009f5b84cfe2b3baf23ac0fa6e736f6eb6cc; roadmap/epics/agent-governance-decision-assurance.yaml blob 2b701cea195f5eb3f5989bf2816160eed213a63b; docs/plans/mcp-gateway.md blob 453b3af6d6a170ffb4fe87b0dd5a8a5ccf2e7061.

These are issue/document snapshots, NOT pinned code or deployment SHAs. Runtime coverage, active policies, and production evidence are UNKNOWN pending code/manifests and read-only deployment verification.

## Required invariants
R1. Existing mctl-api identity/tenant/policy/approval authority remains canonical; no second authorization plane. Actor != subject; policy actor != human approver.
R2. For each path, identify authoritative revocation source, event ID, tenant/subject/actor/grant/scope, effective_at, observed_at, version/epoch, propagation path and max stale-authority window. Numbers require owner sign-off.
R3. Revalidate at queue admission, dispatch, retry/replay, and immediately before irreversible effect. Previously approved exact content does not bypass changed authority. An approval is not consumed when authority is unavailable.
R4. Revoked/expired/tenant-removed/cross-tenant/mismatched grants fail closed with typed outcomes. Unknown source availability is not equivalent to allowed. No fallback from policy to human authorization or vice versa.
R5. Revalidation must be atomic with an authoritative version/fencing decision or explicitly document a remaining TOCTOU race; do not promise impossible cancellation of committed effects.
R6. Distinguish NOT_STARTED_DENIED, CANCEL_REQUESTED, CANCEL_CONFIRMED, EFFECT_COMMITTED, EFFECT_UNKNOWN_RECONCILE. Do not assert rollback or kill running processes by default.
R7. Each attempt retains durable references to original decision, exact action digest, authorization version, effect idempotency key, outcome and audit event. Sampled traces alone are insufficient.
R8. Offline synthetic tests must cover non-revoked control, expiry, membership removal, revoked grant, stale cache, unavailable authority, queued work, retry/replay, cross-tenant, unaffected tenant, and pre/post-effect races.
R9. No network access, real credentials, customer data, production writes, incident remediation, automatic revocation, or activation in this research.

## Surface inventory to verify (not claims of enforcement)
Browser sessions; upstream JWT; local OAuth access/refresh; tenant membership/cache; run tokens; policy enrollment/delegation; provider connections; shared service credentials; outstanding approvals; queued/running/retried Temporal/Argo work; automations; direct MCP paths; PA/Telegram/Glovo credential custody and fences.

## Proposed measurement targets (NOT accepted SLOs)
- Local synchronous authorization: revalidate at each irreversible effect; refuse stale/unknown authority rather than rely on time-only TTL.
- Queue/retry: revalidate on every dispatch/attempt; no stale approval reuse.
- Cache: measure actual revocation delay and maximum stale authorization per surface. Deferred gateway's 305s design bound is NOT an acceptable platform-wide revocation SLO and is NOT deployed proof.
- Offline fixture acceptance: 0 unauthorized post-revocation new effects; 0 cross-tenant effects; 0 duplicate effects under replay; all unknown/partial effects require reconciliation.
