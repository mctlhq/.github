# Agent Authority Lifecycle — proposed design / research model

Status: PROPOSED / NOT IMPLEMENTED. Source and uncertainty ledger: [requirements.md](requirements.md).

## Minimal decision model
Input: canonical actor, subject, tenant, target, exact_action_hash, decision_id, approval_context_digest, authority_source, grant_id, authority_version/epoch, policy_version, effect_key, attempt_id, clock.

Authority snapshot is evidence, not permission. At each execution boundary query the authoritative source (or validated freshness/version proof), compare effective revocation epoch, membership, scope, expiry, exact action and policy checkpoint. If unreachable, return AUTHORITY_UNAVAILABLE and do not execute or spend approval. A changed relevant authority/policy/context requires refusal or reapproval; preserve existing mctl-api#700 error semantics.

Proposed typed outcomes (mapping to existing codes required before implementation): ALLOW, DELEGATION_REVOKED, DELEGATION_EXPIRED, TENANT_MEMBERSHIP_REMOVED, SCOPE_MISMATCH, CROSS_TENANT_DENIED, AUTHORITY_UNAVAILABLE, APPROVAL_STALE_REAPPROVAL, EFFECT_COMMITTED, EFFECT_UNKNOWN_RECONCILE. Never label a model inference as authoritative.

## Temporal race model
1. Approval granted for exact action hash at t0.
2. Revocation effective at t1 before queued dispatch t2 -> dispatch must deny, including retry/replay.
3. Revocation between dispatch and effect t3 -> effect boundary must revalidate; if cannot atomically fence with effect, classify residual TOCTOU as UNKNOWN and block GO.
4. Revocation after effect committed -> preserve committed effect, no retroactive rollback; report and reconcile only when necessary.
5. Cancellation is cooperative, not proof of external effect prevention. An effect service with no fencing/idempotency may not meet bounded-revocation proof.

## Authority matrix / existing owners
| Surface | Authority | Existing owner | Status / gap |
|---|---|---|---|
| Approval context / decision | mctl-api canonical policy + principal | api#700/#701, .github#181 | issue closed; live effect revalidation UNKNOWN |
| Delegated policy enrollment | mctl-api policy enrollment | api#901 | planning only; not active |
| Manual/Temporal/incident responder | canonical execution authorization | agents#703 | issue closed; coverage under live paths UNKNOWN |
| Agent run tokens / static fallback | token exchange + runtime rollout | agents#752 | investigate observe-only; static token still tracked |
| Gateway policy cache | mctl-api policy | .github#356 | deferred; proposed R_max=305s only |
| Adversarial regression | deterministic bounded harness | .github#151 | reuse for proven cases |
| PA/Telegram/Glovo | product-specific consent/credential custody | respective product owners | inspect only if affecting tested vertical slice |
| SIEM | passive audit event consumer | .github#373 | never a revocation decision maker |

## Offline vertical slice
Use a deterministic in-memory fake authority clock and synthetic IDs: tenant T1, tenant T2, subject S1, delegation D1, approval A1, action digest H1, effect key K1. Revoke D1 after approval and before dispatch; simulate stale cache, authority outage, replay, retry, and two phase effect boundary. Assert deny + no effect for every unauthorized path. Positive control executes once; T2 unaffected. All events include source and observed timestamps, attempt id, outcome, evidence digest. Fixture oracle is independent of implementation logic.

## Open architectural blockers
- P1: No source-pinned code-path and deployed enforcement inventory yet; cannot prove all direct MCP/worker/provider paths hit a common checkpoint.
- P1: Atomic effect-boundary fencing with external providers not established; possible post-check race.
- P1: No owner-approved per-path revocation bounds; gateway draft 305s must not silently propagate.
- P2: Credential custody, refresh-token and long-running job cancellation behavior unknown.
- P2: No frozen executable fixture bytes or measured results yet.
- P3: Typed outcome names require alignment with existing API codes and schema.
