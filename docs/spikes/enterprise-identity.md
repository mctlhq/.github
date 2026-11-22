# Proposal Revision 2 — .github#374

22 November2026. Proposed only. Primary DEFER enterprise activation, SCIM and custom adapter development. Existing pinned21 November code/config evidence is reused, not live reverified. Supplements earlier research; no old review is claimed to cover this revision. Human ZITADEL, explicit provider/issuer/subject links, canonical principal membership and existing approvals remain authoritative.

## Independent decisions

F: enterprise OIDC/SAML federation. Product must name use case, target relying party, enterprise IdP/protocol, user population, current manual baseline, required trust/email/MFA assertions and existing grants. Private demand evidence is referenced safely, not customer names in public issues. Missing demand -> DEFER. SAML may be compared if required; smallest candidate is OIDC-only, one isolated synthetic org, one IdP, manually linked principals/grants, SCIM off, no automatic admin/JIT/control-plane access, no customer onboarding. Plan review is allowed independently of SCIM; no pilot is authorized here.

L: SCIM provisioning/lifecycle. Require separate concrete Users/Groups/disable requirements, operations and residual-access bounds. Native ZITADEL4.20.5 SCIM2 Preview Users is comparison baseline from pinned upstream, not deployed capability proof. Groups missing is not an automatic adapter verdict. L does not require enterprise federation F to have passed: existing sign-in may suffice; only its actual authentication/lifecycle prerequisites apply. F success does not approve L. Preserve DEFER for SCIM activation/custom adapter until native gap and safety proof exist.

## Revocation contract worksheet

Every enabled path must have accountable owner, initiating operation/scope, maximum accepted residual access, enforcement point, clock/skew and failure policy before any readiness PASS. No universal TTL/SLA is invented. UNSET bound/owner/observations -> UNKNOWN, never unlimited or immediate. Proposed policy floor: zero cross-tenant mutation, zero fallback to email/group/default tenant authority, zero privilege resurrection. Exact time bounds require product/security choice.

| Path | Existing owner role proposed | Required future acceptance evidence |
|---|---|---|
| IdP/ZITADEL browser sessions | IdP + each relying party | Fresh login versus existing session after scoped disable; measured denial within chosen bound |
| Upstream JWT/access/refresh | IdP + API verifier | Effective expiry/introspection/revocation and refresh/rotation race; token expiry alone not instant revoke |
| MCTL local OAuth access/refresh | API OAuth owner | Principal-bound mint/refresh, session revoke trigger, stale/rotating refresh and still-issued access behavior |
| Membership/principal caches | API authz owner | Per-replica invalidation/expiry/outage results, no degraded credential-only authz fallback |
| Enforced agent run token | API run-token + agents owner | Enabled-mode actor/scope checks after disable/expiry, no silent static fallback |
| Delegated900s token | API grant owner + agents activity | Distinct mint via service credentials, exact execution/subject binding, replay and disable policy; nominal TTL not revoke SLA |
| Shared service credential | Service owner + agents consumers | Off/observe use static bearer; observe exchanges/revokes probe token only. User disable is not service revoke; retained bytes do not prove server acceptance; separate rotation scope |
| Outstanding approval/grant | API approval owner | Pending/approved/consumed/executed state policy after decider disable and before/after exercise; no local prefix check substitute for current authority |
| Downstream app sessions/Vault tokens | Each relying-party owner | Independent TTL/revoke/offboarding checks, or proof excluded from bounded pilot |
| Personal/service-user keys/PATs and alternate links | Credential + principal-link owner | Inventory what user can mint, deny/exclude or explicitly revoke; removing one IdP link does not revoke other valid identities |

Record upstream disable commit and downstream receipt timestamps, bounded clock skew, and last accepted/first denied requests. End-to-end bound starts upstream; receipt-only latency must be separately labelled and cannot substitute. Tenant-local grant removal and org-user disable do not imply global principal disable; another tenant/linked identity remains unchanged unless independently authorized.

## Evidence and owner decision gates

| Unknown | Proposed existing owner | Proof / acceptance threshold | Unlocked decision |
|---|---|---|---|
| F demand/trust | Product + IdP/API federation | Plan-time requirement, pinned CODE/CONFIG trace and exact reviewed expected wrong issuer/aud, email/MFA, explicit-link and unmapped-grant contracts; no executed IdP test required to review plan. Execute negatives only in separately authorized isolated pilot before adoption; no default grant | Review one OIDC-only pilot plan |
| F plan readiness | IdP/GitOps + API/relying parties | Accountable people, exact isolated scope, per-enabled-path chosen bounds, offline/plan evidence complete, rollback removes only pilot grants/revokes pilot sessions while preserving audit/last-admin/other tenants | Owner may separately authorize pilot; not authorized here |
| F adoption | Same owners + independent reviewer | Separately authorized pilot results satisfy all mandatory frozen criteria and residual bounds; no unobserved enabled path | Consider REUSE_EXISTING_FEDERATION, no SCIM consequence |
| L demand/native fit | Product + IdP owner | Required operations/Groups and provisioning channel; native behavior tested against manual baseline first | Decide native evaluation scope; no duplicate Users server |
| L protocol isolation | IdP/API hook owner | Users and Bulk least-privilege/org tests; zero unauthorized sub-operations, cross-tenant disclosure/mutation; exact expected partial success/failOnErrors/retry; no duplicate/divergent state contrary to frozen contract; stale update never resurrects disabled access; ETag unsupported so no assumed If-Match | Review native-fit verdict; missing evidence remains UNKNOWN, not adapter approval |
| L revocation/operations | API authz/OAuth/grants + IdP/app operators | Every enabled worksheet path measured within its owner-selected bound, outages/replays/reactivation covered, credential rotation/audit/retention/rollback/cost owned | Review a separate lifecycle pilot/activation plan only |

Per decision, mandatory gates/path results aggregate independently: all required expectations PASS -> evidence-ready for its stated decision request; any required FAIL blocks; UNKNOWN blocks readiness without being relabelled FAIL. Pilot-plan prerequisites must precede pilot GO; pilot observations occur after separate pilot GO and gate later adoption, avoiding circular requirements. Evidence-ready never grants GO; GO never substitutes for required proof. No public customer data, no new identity registry/epic/issue, no approved decision/activation.

## Normative review clarifications

Use explicit path states ENABLED, EXCLUDED_BY_SCOPE or UNKNOWN. Exclusion requires dated config/route/client/capability evidence showing the bounded pilot cannot reach/mint/use that path, an accountable component owner and independent review; it is not an unmeasured PASS. Plan readiness requires an enabled-path inventory reconciled against all configured relying parties, accepted credential/provider types, token issuance/refresh routes, linked identities, agent/service integrations and approval consumers. Each existing owner attests completeness in their domain; an unmatched surface or missing owner leaves readiness UNKNOWN. Execution later verifies each enabled path; excluded-path assumptions must be checked for drift.

The initiating org-user/tenant-grant disable is its own authoritative operation: IdP org-owner plus API membership-owner must define scope and map to exact owned user/grants, preserving other tenant memberships and alternate valid links. No global principal action follows automatically. Bulk/ETag facts are earlier ORCH/pinned evidence, not fresh runtime proof. Passing native protocol tests is not proof of deprovisioning. Default mctlhq/mctl attribution labels must never supply a canonical tenant/grant; mismatch-derived authority is FAIL.

Pilot rollback must stop new login/provisioning first, revoke all pilot-owned sessions/credentials and grants within chosen bounds, inventory persistent identity links/JIT users and handle them explicitly without global deletion, preserve audit/last-admin/other tenants, and prevent reactivation from restoring stale grants. Removing IdP configuration alone is insufficient. These are reviewed plan requirements before pilot GO and executed checks only after separate authorized pilot starts; no account actions occur now. Any unresolved path/rollback effect blocks later adoption. Mapping to outcomes: demand/evidence gaps retain DEFER; owner may separately consider BOUNDED_ENTERPRISE_IDP_POC for the scoped federation plan; validated adoption can propose REUSE_EXISTING_FEDERATION; native gap alone cannot approve ADD_SCIM_ADAPTER or justify an epic.
