# Agent Authority Lifecycle — research tasks (no implementer)

Status: PROPOSED. No agents:intake, approvals, merge, deploy or live probes.

- [x] T0: Confirm #375 research-only authority and non-duplication constraints.
- [x] T1: Read canonical owner issue contracts #181/#151, api#700/#701/#901, agents#703/#752 and gateway#356; capture source links and available blob hashes.
- [ ] T2: Pin HEAD commit SHAs for .github, mctl-api, mctl-agents, mctl-gitops, mctl-telegram, PA/Glovo as applicable; inspect actual auth middleware, dispatch, effect handlers, caches, worker templates and deployment manifests. Record path:line and SHA; do not infer deployment from merge.
- [ ] T3: Read-only deployed inventory where permitted: checkpoint activation, run-token observe/enforce, provider custody, revocation propagation, tenant cache and direct MCP bypass. Record timestamp or UNKNOWN.
- [ ] T4: Freeze synthetic fixture bytes, expected typed outcomes and deterministic clock; include all R8 cases, race ordering and independent oracle. No real network.
- [ ] T5: Execute isolated offline harness only after separate owner authorization; record command, SHA, TP/FP/denials, replay behavior, mutation tests and negative controls. No production writes.
- [ ] T6: Build path-by-path revocation-window matrix with measured or UNKNOWN values and explicit owner-proposed thresholds.
- [ ] T7: Map confirmed gaps to existing api/agents/gitops/Telegram/PA/Glovo owners; no new epic or duplicate authorization plane.
- [ ] T8: Independent reviewer (separate from author) checks source pins, fail-closed semantics, pre/post effect races, tenant isolation, actual fixture results and governance. Record P1/P2/P3 and require revision before review-ready.
- [ ] T9: Owner chooses exactly one: REUSE_EXISTING_CONTRACTS / EXTEND_EXISTING_OWNERS / BOUNDED_REVOCATION_PROOF / DEFER. Separate GO required for any implementer or pilot.

## Exit criteria for review-ready
All P1 source/atomicity/bounds blockers resolved or explicitly scoped to DEFER; no claims of live proof without runtime evidence; independent reviewer signs off exact HEAD; proposal status stays proposed until owner decision.
