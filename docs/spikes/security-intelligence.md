# Proposal Revision 2 — .github#373

22 November2026. Proposed only. Primary DEFER for live ingestion and SIEM/sensor adoption remains. This is a reviewed experiment specification candidate, not a runnable harness or authority to build/run it. Supersedes the K8s minimal-slice candidate in earlier exit-criteria addenda; the broader SEC373 cases remain future integration coverage. Old snapshots/reviews are preserved and do not cover this revision.

## One minimal vertical slice

Use existing Squid agent-egress source ownership from mctlhq/mctl-gitops#2232. Current issue read22 November: counters/silence alerts remain open, source arrival and bypass coverage unproved. Inspected proxy configuration uses native Squid stdout and stage-A allow-all (subject to port/local-address denies); no deployed-state claim. Therefore the rule is **observed CONNECT destination outside a frozen evaluation allowset**, not proof of blocked egress, compromised agent, enforced policy violation or complete egress coverage. A non-2xx response proves neither established connection nor successful data transfer. No new collector/Promtail stage is proposed.

One rule EG373-R1 revision1: on a valid fixture CONNECT observation whose status is200, compare normalized DNS destination:port against exact versioned synthetic allowset. Outside -> MATCH_REFERENCE; inside -> NO_MATCH. Non200 -> NO_MATCH for this rule; input case retains response context; absent/malformed/untrusted/unsupported inputs -> declared UNKNOWN/REJECT. No suffix matching, DNS resolution, IP attribution, tenant guess or action. HTTP absolute-URL requests, IPv6/IP literals and unknown format variants are unsupported -> UNKNOWN, not clean negative. Same host with an unlisted port is outside the allowset. All destination values below are invented .invalid domains.

## Exact normalized fixtures and answer key

Allowset revision eval1: `["allowed.invalid:443"]`. Envelope fields are exactly id, source, method, destination, status; source=`fixture-squid` is a harness trust assumption. Ordered stream:

| ID | Input (all source fixture-squid unless stated) | Expected |
|---|---|---|
| E1 | id=e1,CONNECT,allowed.invalid:443,200 | NO_MATCH |
| E2 | id=e2,CONNECT,outside.invalid:443,200 | MATCH_REFERENCE |
| E3 | identical E2 repeated | REPLAY; no extra detection |
| E4 | id=e2,CONNECT,changed.invalid:443,200 | CONFLICT; retain first, no extra detection |
| E5 | id=e5,CONNECT,outside.invalid:443,403 | NO_MATCH; denied response, no successful-tunnel assertion |
| E6 | id=e6,CONNECT,ALLOWED.INVALID.:443,200 | NO_MATCH after ASCII lowercase and one terminal-dot normalization |
| E7 | id=e7,CONNECT,allowed.invalid:8443,200 | MATCH_REFERENCE |
| E8 | id=e8,CONNECT,outside.invalid:443,200,source=untrusted-fixture | REJECT; no detection |
| E9 | id=e9,CONNECT,allowed.invalid.evil.invalid:443,200 | MATCH_REFERENCE; exact equality only |
| E10 | id=e10,GET,http://outside.invalid/path,200 | UNKNOWN unsupported method/format |
| E11 | id=e11,CONNECT,outside.invalid:443,missing status | UNKNOWN malformed |
| E12 | separate coverage input=missing source | COVERAGE_UNKNOWN; not a threat negative |

Twelve input cases: three matches E2/E7/E9; three NO_MATCH cases E1/E5/E6, E3 REPLAY, E4 CONFLICT, E8 REJECT, E10/E11 UNKNOWN, E12 COVERAGE_UNKNOWN. Count outputs by this explicit per-case vector, not an ambiguous detection denominator. Replay key (source,id) and immutable canonical source-content digest; per-run in-memory only, discarded at exit. Generated receive time is outside source digest. Existing source lacks an asserted stable event ID: fixture IDs do not solve production dedup; that remains UNKNOWN with the existing source/API owner to confirm.

Output for each case: case_id, disposition, rule_revision, allowset_revision and opaque evidence_ref; no destination/client/user/tenant text, URLs, source-line copies or arbitrary labels. No separate detection-ID output field is defined. No incident/finding/proposal/approval fields or clients. Raw Squid lines are a separate parser fixture layer: exact safe byte examples, field indexes and expected normalized rows must be frozen against the pinned native-format source contract before a harness run can claim parser readiness. Normalized-case PASS proves rule semantics only; it does not prove raw-log parsing, source authenticity or live coverage.

## Runnable harness specification — NOT IMPLEMENTED

Future separately authorized command interface: `python3 -I offline_eval.py --fixtures fixtures/normalized.jsonl --allowset fixtures/allowset.json --expected fixtures/expected.json --out results.json`. Stdlib-only local files, no network/env credentials/API clients/subprocess tools, fixed seed and deterministic canonical JSON. Validate allowed keys/types/bounds first, parse supported raw fixtures separately, run rule/replay projection, compare every exact output, emit per-case observed/expected and summary. Exit0 only for all mandatory expectations and isolation assertions satisfied; exit1 for observed mismatch/boundary violation; exit2 for missing fixture/hash/precondition (UNKNOWN). Exact normalized fixture/allowset/expected-output files are supplied under fixtures/; no harness implementation is supplied or executed. Raw parser fixtures remain NOT CREATED and parser readiness UNKNOWN. Freeze bytes, hashes, parser contract, forbidden-field list, stdout/output schema and exact counts before implementation evaluation; publish no actual customer lines.

Baselines on identical frozen cases: null rule must fail E2/E7/E9; substring allow-check must fail E9; ignore-port baseline must fail E7. A key admitting these baselines is non-discriminating and FAIL. Manual review precedes comparison and records ambiguities. No performance/accuracy superiority is inferred from12 synthetic cases. Measure wall time/peak memory as descriptive host-bound observations only; owner must set budgets before any performance readiness claim.

## Exit from DEFER and downstream isolation

Owner roles proposed, not appointed. Accountable person and future experiment GO are separate required records; unset means UNKNOWN. Evidence stays local until publication authorized, then references existing #373/#2232 only, no duplicate issue or changed closing criteria.

| Unknown | Proposed existing owner | Required evidence / acceptance threshold | Unlocked decision |
|---|---|---|---|
| Source format/coverage | GitOps #2232 source owner | Pinned native format + raw parser fixtures; parser cases exact; live arrival/bypass explicitly UNKNOWN | Review offline harness plan only |
| Rule semantics | Existing detection/API owner TBD | All12 frozen outputs exact; three deliberately weak baselines fail designated cases; zero forbidden-field/authority violations | Owner may consider bounded offline implementation/evaluation scope |
| Replay identity | Source owner + existing API/incident owner | Fixture run reset/replay/conflict proof; production key/retention owner separately named | No live dedup claim until production ownership proof |
| Current consumer isolation | Agents/API incident owners | Enumerate Temporal, cron, responder tools, incident watch and other readers; deterministic server-side/pre-model exclusion or keep no writer; injected security references yield zero hydration, text-copy, proposal, resolve/ack/finding/remediation/verification writes or dispatch; cross-tenant negative tests | Review integration plan only, not live routing |
| Live usefulness and cost | Security/product + source operator | Separate source freshness/coverage/access/retention, representative sanitized evaluation and owner-frozen performance/resource bounds | Reconsider DEFER for a separately scoped native pilot; never automatic GO |

Local harness isolation is weaker than downstream isolation: no-client sandbox plus forbidden-import/call spies can establish the former; the latter requires instrumented tests of existing consumers in isolated fakes and complete consumer inventory. No model prompt/status label is enforcement. All mandatory cases must PASS; any boundary FAIL blocks; missing proof UNKNOWN blocks readiness. Evidence-ready is only permission to submit a decision request, never execution authority. Keep incident writes, remediation, live ingestion and external SIEM deployment out of scope.

## Normative review clarifications

Only the substring baseline is required to fail E9; a pure suffix implementation need not fail E9 and is not a baseline failure criterion. Fixture E7 is deliberately counterfactual normalized rule conformance: the inspected stage-A configuration denies CONNECT outside443, so no real8443/200 observation or parser provenance is claimed. It does not test deployed policy enforcement. E12's exact union input is `{ "coverage": "missing-source" }`, as supplied; other event records require the five typed fields listed above. Check harness case envelope/coverage union, then event field types/bounds, source trust, supported method/host syntax, replay key/digest, then rule. Unsupported syntax -> UNKNOWN, untrusted source -> REJECT, never a trusted negative. Additional malformed/IP/double-dot cases must be added before claiming general input robustness; this12-case key alone does not do so.

The exact output schema is the five fields in expected.json. NO_MATCH intentionally does not distinguish E5 denial from E1 allowed destination; the per-case input/answer record preserves that context. No response-outcome or detection-ID output field is required; the earlier detection-ID sentence is non-normative and superseded by this schema. fixture:e2 intentionally identifies the same source observation for E2/E3/E4; case_id distinguishes repeat/conflict results. PASS is conformance to this public key, not blind holdout/general accuracy. An expected case-level UNKNOWN can PASS conformance; missing evaluator preconditions cause exit2/overall readiness UNKNOWN, never a successful run.

Parser readiness is blocked: no raw fixture is supplied, and built-in access_log squid configuration does not define its field layout in this checkout. Inspected file's last-touch commit0840233f71f4dc864b48658a4d884259be6f0f2a and image digest8a3baed477e2c282ab8aa5edad442f69873246964f225c5c2ae8364b6610963c pin configuration/image only. Before a future parser run, the existing source owner must provide exact versioned upstream format/code proof plus synthetic raw byte fixtures, field layout and normalization outputs, review/hash them independently. Missing proof -> parser readiness UNKNOWN and blocks parser evaluation, but does not prohibit reviewing this normalized-only plan. Stage-B denial detection is a separate rule, excluded from EG373-R1; no coverage claim for it or direct/bypass egress.
