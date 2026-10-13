# Agent Commerce — payment-rail follow-ups

Status: proposal for review, 2026-10-13. This document does not authorize production real-money behavior, mainnet funds, wallets held by MCTL, merchant onboarding, provider payouts or marketplace rollout.

## Why extend a completed epic

The required Agent Commerce foundation is complete: governed Paid Capabilities work with simulated credits. The remaining question is not whether the foundation is delivered, but how external economic rails attach without creating a second execution or authorization path.

External benchmarks such as AgentRouter and BlockRun/ClawRouter reinforce two useful patterns:

1. support more than one economic rail (account-funded allowance / enterprise chargeback and autonomous wallet payment);
2. keep payment admission/settlement separable from usage and provider execution.

MCTL should adopt those patterns without becoming an inference reseller or treating payment proof as capability authority. Its differentiator is governed economic execution: identity, tenant authorization, policy, budget, exact-action approval where required, payment, execution, durable accounting and reconciliation compose in one path.

## Portfolio decision

Do **not** reopen the completed Agent Commerce foundation and do **not** create a new top-level epic. Add optional interoperability follow-ups to the existing epic. `completion.mode: allRequired` means these items do not invalidate the already-proven simulated-credit milestone.

Proposed optional ladder:

```text
Paid Capabilities proof (#620)
          |
          +--> x402 MCP Check testnet pilot (#616)
          |          |
          +----------+--> payment interoperability decision (#549)
                                   |
                                   v
                    governed external-payment proof
                                   |
                        later production rail decision
                                   |
                      marketplace / payouts much later
```

## #616 — isolated x402 MCP Check pilot

Bind existing `mctl-api#616` into Agent Commerce as optional interoperability evidence.

The pilot intentionally stays isolated and testnet-only. Its value is to prove concrete challenge/payment/receipt, replay protection and settlement-uncertainty recovery on a bounded read-only capability. It must not be described as canonical Agent Commerce integration: the issue explicitly leaves MCTL identity/policy/budget integration for follow-up.

## #549 — refine the interoperability decision

Keep one `PaymentAuthorizer`/commerce core and compare economic rails instead of making x402 the architecture.

The issue should consume #616 evidence and compare at least these shapes:

- **enterprise/internal rail** — existing simulated/chargeback semantics, with no external settlement;
- **account-funded allowance rail** — payment/top-up/invoice funds an allowance consumed by usage; design comparison only, no stored-value implementation authorized here;
- **autonomous wallet rail** — x402/MPP-style per-request or spending-cap authorization.

For each rail record:

- immutable quote/challenge binding to payer, principal, tenant, capability/action and expiry;
- whether it offers exact payment, spending cap, reserve/capture/void/refund, or a different lifecycle;
- signer/key custody and whether MCTL can stay non-custodial;
- retry, replay and lost-response semantics;
- settlement ambiguity and status/reconciliation APIs;
- fee/latency constraints and minimum economically sensible charge size;
- MCP error semantics, streaming and long-running support or explicit rejection;
- how the rail maps to the existing ledger without faking unsupported adapter features.

AgentRouter, BlockRun/ClawRouter and Cloudflare Monetization Gateway are interoperability/UX benchmarks, not dependencies or sources of authorization truth.

Decision output: select a rail for the next governed proof or explicitly defer external payment. A testnet x402 success alone must not imply production adoption.

## New optional work item — governed external-payment proof

Proposed unbound roadmap item:

> **Integrate one external payment rail into governed Agent Commerce**

Purpose: bridge the current isolated payment experiment and the canonical MCTL execution path.

Minimum acceptance for the future issue:

- use one rail selected by the interoperability decision, initially testnet/staging unless separately authorized;
- bind payment evidence to the same immutable quote and canonical invocation as the commerce ledger;
- authenticate canonical actor/subject/tenant before commerce execution;
- keep sealed capability eligibility, authorization and policy authoritative;
- preserve exact-action approval semantics where the capability requires approval;
- enforce MCTL budget independently from payment ability — having funds never grants permission or raises a budget;
- payment proof is never an authorization grant;
- one request cannot execute or charge twice under concurrent retry;
- ambiguous settlement/execution remains queryable and reconciles the original invocation rather than creating a fresh charge or re-running the capability;
- produce one end-to-end receipt/audit chain: actor → policy/approval → quote → payment evidence → execution → usage/charge → reconciliation state;
- prove disable/rollback stops new externally paid requests while preserving status/reconciliation of in-flight records.

Explicit non-goals for this work item:

- mainnet/production money without a separately reviewed rollout;
- MCTL custody of private wallet keys;
- card storage or PCI-sensitive data;
- marketplace/provider onboarding;
- payouts/revenue share;
- tax/VAT/FX;
- general streaming or long-running paid tasks;
- model routing (owned by the Agent Bricks / Unity Gateway evaluation and model-policy work).

## Dual-rail product direction

The future product may expose two different buyer experiences while preserving one governance core:

```text
human / enterprise                  autonomous agent
account / invoice / allowance       wallet / x402 / MPP
             \                       /
              \                     /
               payment-rail adapter
                       |
              governed invocation
 identity -> authz -> policy -> budget -> approval -> payment
                       |
                    execute
                       |
              ledger + reconciliation
```

This is a direction, not authorization to build both rails now. The next implementation decision should be made only after #549 has evidence about lifecycle support, custody, fees and failure semantics.

## What remains deferred

- Marketplace remains `mctl-api#23` and should be revisited only after a trustworthy real external-payment loop exists.
- Provider settlement/payouts and revenue share are later than marketplace discovery.
- Cost/reliability-aware model routing stays outside Agent Commerce; route that finding through the Agent Bricks / Unity Gateway spike rather than creating a duplicate commerce item.
