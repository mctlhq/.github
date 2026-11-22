# Spike: MCP Events (SEP-3415) over the existing MCTL event infrastructure

Issue: mctlhq/.github#377. Revision 2, 2026-11-22. Proposed only. This is an
offline-only slice: report, manifest and a stdlib reference model with tests.
Nothing here changes a service, stream, ACL, secret, host subscription or Claude
Channel, and no live webhook, host session, account link or real content was
used.

Tags: `[OBSERVED]` measured by an agent, `[DOC]` URL or file with retrieval
date, `[CODE]` repo, SHA, path, `[GITOPS]` path and commit, `[INFERRED]`
reasoning without a source. `unknown (not observed)` means nothing was read.

## 1. Status and non-claims

- The proposed verdict is BOUNDED_HOST_PILOT, offline-first (section 12). The owner records the decision after an independent review; the manifest `roadmap/spikes/mcp-events-sep-3415.yaml` has no `decision` block.
- Not claimed: that any host accepts these deliveries, that ChatGPT interoperates with the pinned spec, that any SDK implements the extension, or any runtime property of the deployed event path beyond the cited code and gitops state.
- The offline model proves the delivery contract (tests T1 to T22). Test results are `[OBSERVED]` for the model only.

## 2. Pinned sources and maturity

Normative source: `modelcontextprotocol/ext-events`,
`specification/beta/events/events.mdx` at
`17c0c64a5edb89dc5081d00ed50c4f836bbb67ff` (2026-11-19), introduced by
`24727f38a180b1bde5df1c6eae580ea0a0b703ee`, "Stage: Beta", protocol revision
`2026-07-28` and later `[DOC]`. The whole file was read on 2026-11-22 in two
ranges through a summarizing fetch tool; the per-rule quotes are in
`docs/spikes/mcp-events-sep-3415/model/SPEC_PIN.md`, and a person should re-read
the raw file before any activation gate.

Three separate maturity rows (they are different facts and disagreement is
reported, not resolved):

| Row | State | Tag |
|---|---|---|
| SEP governance, plan.modelcontextprotocol.io | `final`, 2026-11-19, as reported by the review of 2026-11-22. The page rendered no content to the investigator. | reported, `unknown (not observed)` |
| Voting tracker, voting.modelcontextprotocol.io/p/seps/3415 | `Scheduled`; linked PR `modelcontextprotocol/modelcontextprotocol#3415` `Merged`; meeting 2026-11-04 passed | `[DOC]` 2026-11-22 |
| Extension maturity | `Beta` at `ext-events@17c0c64a` | `[DOC]` |

"Not Final" is not a DEFER trigger by itself. SDKs: no package or version
implementing `io.modelcontextprotocol/events` was observed (`unknown (not
observed)`). Ordering, from the spec `[DOC]`: within one subscription events are
delivered in the order the server produces them for poll and push, webhook order
is best-effort because retries reorder, and "No ordering is guaranteed across
subscriptions in any mode." The model guarantees materialization order per
subscription only.

## 3. Finding-to-change matrix

| Finding | Spec quote (`ext-events@17c0c64a`) | Revision 1 claim | Revision 2 change | Model | Tests |
|---|---|---|---|---|---|
| P1-EV-01 | "`delivery.secret` is REQUIRED. The client supplies the HMAC signing secret, and the server never generates one"; "There is no server-initiated rotation." | server generates the secret | client-supplied `whsec_` secret, `InvalidParams` otherwise, client-initiated refresh with dual-sign grace, fingerprint-only records | `webhooks.py`, `subscriptions.py` | T3, T4, T5 |
| P1-EV-02 | `webhook-id` "MUST NOT be derived from `eventId`"; "the same `eventId` delivered on two subscriptions is two deliveries" | `webhook-id = envelope.id` | `eventId = envelope.id`; random `webhook-id` per (subscription, occurrence) and per control envelope, persisted for retries; `X-MCP-Subscription-Id`; dedupe scoped per endpoint and per subscription | `projection.py`, `fanout.py`, `recipient.py` | T6, T7, T8 |
| P1-EV-03 | "`null` means "start from now""; `truncated: true` is "the single signal" with a fresh `cursor` | `cursor_expired` error | `null` is now; `maxAgeMs` later-of; gap is `truncated: true` plus fresh cursor, or a signed `gap` envelope for webhooks; `hasMore`; the internal flag stays in audit | `cursor.py` | T9, T10, T11 |
| P1-EV-04 | retries are independent per event (section 2) | one PEL, XACK after delivery | one transaction materializes a delivery row per matching subscription (unique on subscription and event), XACK after commit; independent retry, quota and gap per subscription | `store.py`, `fanout.py`, `delivery.py` | T12, T13, T14, T15 |
| P2-EV-05 | "A client MUST NOT send them to a server that has not advertised the extension" | polling fallback with the same cursor | no `events/*` without the advertised extension (`-32601`); only existing fallbacks, no shared cursor | `client.py`, `server.py` | T16 |
| P2-EV-06 | unsubscribe ends a webhook subscription immediately | epoch check before POST | revocation linearized at the commit of `state=revoked, epoch+1`; CAS claim; unsubscribe returns after drain; three ACK facts | `delivery.py` | T17, T18 |
| P2-EV-07 | header "Stage: Beta"; tracker `Scheduled` | "not Final" as a DEFER trigger | three maturity rows (section 2); DEFER triggers redefined (section 12) | this report | doc review: sections 2 and 12 |

## 4. Method map

Exactly the pinned surface:

| Pinned name | MCTL equivalent |
|---|---|
| `events/list` | catalog of envelope `type` names with a references-only `payloadSchema` (`projection.py`) |
| `events/poll` | read of `mctl:events:*` through a cursor that encodes a stream entry id, tenant-filtered (`cursor.py`) |
| `events/stream` | same cursor, push activation; notifications `notifications/events/{event,active,error,heartbeat,terminated}` are not simulated in the model |
| `events/subscribe` | subscription row keyed `(principal, url, name, arguments)`, verification, delivery rows |
| `events/unsubscribe` | revocation commit plus drain |
| `notifications/events/list_changed` | only message outside an `events/*` request; not modelled |
| webhook control envelopes `gap`, `terminated`, `verification` | delivery rows of kind `gap`, `terminated`, `verification`, each with its own `webhook-id` |
| (not in spec) processing ACK | MCTL tool `mctl/ack_event` mirroring the Channel `ack_event` `[INFERRED]` |

Kept distinct:

| Mechanism | What it is | Relation |
|---|---|---|
| Events (SEP-3415) | server-defined event types, poll, push or webhook, cursor, per-subscription delivery | this spike |
| `resources/subscribe` + `notifications/resources/updated` | core MCP: change notice for one resource URI, no cursor, no replay | different contract; not reused |
| Tasks status notifications (#41) | status of one long-running execution | reference by id only; no second status path |
| `notifications/claude/channel` | Claude Code Channel push into one active session (mctl-claude-remote) | unchanged; comparison baseline |

## 5. WP1 inventory

| Item | State | Tag |
|---|---|---|
| Envelope v1 (`events/schemas/event-envelope.v1.schema.json`), no tenant or principal field, 4096-byte cap enforced by consumers | merged, `mctlhq/.github@60e9db5` | `[CODE]` |
| Delivery chain and rules in `events/README.md` (at-least-once, dedupe by `id`, one group per session, `MAXLEN ~ 10000`) | merged | `[CODE]` |
| mctl-api `4.173.0` GitHub webhook outbox plus relay to `mctl:events:github` (`bootstrap/templates/mctl-platform/mctl-api.yaml` lines 430-431) | deployed | `[GITOPS]` mctl-gitops `e1d0b67` |
| mctl-telegram producer to `mctl:events:telegram` | merged, runtime not observed | `[CODE]` per events README |
| Valkey: `appendfsync everysec`, `maxmemory 200mb`, `noeviction` | deployed | `[GITOPS]` `infra-components/data/valkey/configmap.yaml` |
| ACL users: `default` off; three producers `+xadd` own stream and audit; `claude-remote` group commands on three streams; `events-observer` read-only audit; none for any other consumer | deployed | `[GITOPS]` `externalsecret.yaml` lines 48-54 |
| `labs/claude-remote` `0.31.0`, `replicaCount: 0` (in-cluster consumer paused); comments record #66 (fake ack, inflight failure) and #82 (token renewal metrics) | deployed | `[GITOPS]` `services/labs/claude-remote/values.yaml` |
| `mctl_get_service_status(labs, claude-remote)`: Argo Healthy and Synced, image `0.31.0` | observed 2026-11-22 | `[OBSERVED]` |
| #300 (events push into an existing ChatGPT chat): `status: accepted`, `blocked.code: execution-unauthorized` | gitops state | `[GITOPS]` `agents-state/.github/proposals/issue-300-...` |
| DevLoop human-facing events (mctl-agents#712): no Valkey producer or ACL user today | unknown beyond gitops | `[GITOPS]` |
| Live stream lengths, consumer lag, Mac mini tunnel consumer liveness, audit stream contents, `mctl-events-policy` | `unknown (not observed)` | none |

## 6. WP2 host matrix and the ChatGPT divergence

| Host | Surface and transport | Capability key | Evidence | Interop with the pinned spec |
|---|---|---|---|---|
| Claude Code | Channels, `notifications/claude/channel`, active session | not the Events extension | used by mctl-claude-remote (epic #87) | not applicable; Events support `unknown (not observed)` |
| ChatGPT | Work chats on web, desktop Cloud and dots; webhook only | `events` in `capabilities` per the doc | `[DOC]` developers.openai.com/plugins/build/mcp-events, retrieved 2026-11-22 | `unknown (not observed)` |
| Codex, Antigravity, Grok, Claude (other surfaces) | not read | n/a | n/a | `unknown (not observed)` |

No cell infers support from another surface of the same vendor or from spec
conformance. Plan and build for ChatGPT are not stated by the doc beyond the
surfaces above. #300 (existing ordinary ChatGPT chat) is a distinct question.

ChatGPT documentation versus the pinned spec, all `[DOC]` 2026-11-22:

| Point | Pinned spec | OpenAI doc |
|---|---|---|
| Capability key | `capabilities.extensions["io.modelcontextprotocol/events"]` | `events` in `capabilities` |
| `CallbackEndpointError` code | `-32027` | `-32015` |
| `webhook-id` | high entropy, MUST NOT derive from `eventId` | "The same value as the body's `eventId`" |
| Poll, stream, `gap`, `terminated` | defined | not supported |

The model is spec-conformant and does not implement a ChatGPT compatibility
mode: `webhook-id = eventId` breaks multi-subscription dedupe. Whether ChatGPT
accepts a spec-conformant server, or the doc predates the Beta text, is
`unknown (not observed)` and is resolved only by gate G-L3.

## 7. Adapter reference design

```
producers --XADD--> mctl:events:{github,telegram,synthetic}   (unchanged)
   | XREADGROUP (future ACL user mcp-events-relay, one adapter group)
   v
fanout: ONE transaction per entry in mctl-api's existing database
   match active subscriptions (tenant-resolved, authorized filters)
   INSERT delivery(subscription_id, event_id, webhook_id=random, sub_seq) ON CONFLICT DO NOTHING
 COMMIT -> XACK   (the PEL never waits on a recipient)
   v
per-subscription worker: CAS claim (subscription, epoch, state=active), re-resolve and pin IP,
   sign (dual-sign in grace), POST, no redirects; 2xx delivered, 410 deactivate, 413 final,
   else capped backoff then dead-letter with audit
```

- Projection: `eventId = envelope.id`, `name = type`, `timestamp = occurred_at`, `data = {source, correlation_id, subject}` references only. No Telegram body, PR title, body or diff.
- Ownership: tenant, principal and client come from authentication. Envelope v1 has no tenant, so tenancy is resolved through subject-to-tenant bindings (as `mctl_get_repo_binding` does) and fails closed; an unresolvable subject reaches no tenant subscription. Telegram events are admin-only in any first slice.
- Crash rules (T12 to T14): before commit the entry stays pending and `XAUTOCLAIM` re-materializes it, with the unique key making that idempotent; after commit and before XACK replay inserts nothing; after POST and before marking `delivered` the same `webhook-id` is resent and the recipient dedupes.
- Quota: a subscription over its undelivered quota stops materializing, records a gap position and later receives a `gap` envelope. Other subscriptions are never evicted.
- Authority: a delivery carries no grant. Side effects go through existing authorized tools and approval gates (DevLoop decide page; #375 boundaries, where "#375" may mean mctlhq/.github#375 or mctl-api#375 and both are preserved).
- Future slice (documented, not done): ACL user `mcp-events-relay`, mctl-api tables `mcp_event_subscription`, `mcp_event_delivery`, `mcp_event_adapter_position`, Vault references for secrets, an egress policy for webhooks.

## 8. ADR options

| Option | Summary | Verdict input |
|---|---|---|
| REUSE_NATIVE_EVENTS | Keep Channels and host-scheduled reads. Cheap and deployed, not portable; the in-cluster consumer is paused. Remains the fallback. | baseline |
| ADOPT_THIN_MCP_EVENTS_ADAPTER now | Rejected as the immediate step: Beta extension, no SDK observed, the one documented host diverges on wire identity, envelope v1 has no tenant, and the gateway (#236) that may own the surface is moving. | later |
| BOUNDED_HOST_PILOT | Offline-first; live gates separate (section 10). | proposed |
| DEFER | Only on the triggers in section 12. | fallback |

Dropped alternatives: per-subscription Valkey consumer groups (multiplies PELs on a `200mb noeviction` instance, needs dynamic ACLs, harder fencing); an adapter inside mctl-claude-remote (single-session, paused) or a new webhook fan-out service (a second bus and authorization plane). Open ADR choices: which endpoint advertises the extension (`api.mctl.ai/mcp` or the gateway, #236), and a tenant field or envelope v2 (out of scope; option only).

## 9. Pilot and hypotheses

Pilot (offline): a synthetic `synthetic.devloop.proposal_ready` or `github.pull_request` fixture, the in-process mock recipient and the reference poll client. Claude Channels and existing watchers (`codex-watch`, pr-steward ticks) are left unchanged, compared only as a baseline; no push into an ordinary ChatGPT chat is assumed (#300). Events reference existing canonical ids for Tasks (#41) and Apps (#376); no second execution or status infrastructure.

Hypotheses to measure in a later gate (none measured here): time-to-review, duplicate and noise rate, delivery lag, retry, expiry and revocation counts, token cost per successful reaction versus polling, repeat usage and qualitative value.

Acceptance tests (all offline, in `docs/spikes/mcp-events-sep-3415/tests/`): stale subscriber (T18), cross-tenant (T2), spoofed event (T8, T19), replay (T8), duplicate (T6, T14), two-subscriber fanout (T6, T15), cursor gap (T10), crash recovery (T12 to T14), revocation race (T17), host fallback (T16).

Future slices, named only: Telegram PA, Alice alerts, SeerrSense availability, Glovo status (only with an authorized source), SecOps, release changes.

## 10. Fixture-versus-live runbook

Fixture mode (the only mode this spike runs):

    python3 -I -m unittest discover docs/spikes/mcp-events-sep-3415/tests    # OK, 37 tests
    python3 roadmap/scripts/validate.py roadmap/spikes/mcp-events-sep-3415.yaml    # PASS
    python3 -m unittest events.tests.test_envelope_schema                    # OK

Live gates, each needing separate owner approval. This spike executes none.

| Gate | Preconditions | Kill switch | Evidence to capture |
|---|---|---|---|
| G-L1 Valkey ACL user plus a flagged mctl-api slice on `mctl:events:synthetic` only | owner approval; ACL limited to that stream and audit; Vault reference for secrets | feature flag off; drop the ACL user | stream length, lag, PEL size, delivery rows by state |
| G-L2 one synthetic delivery to an operator-owned mock HTTPS recipient | G-L1 done; egress allowlist for that host | revoke the subscription; remove the egress rule | request headers without secrets, verification result, latency |
| G-L3 one ChatGPT Work or dots subscription with a synthetic event | G-L2 done; owner-held account and plan; coordinated with #300 | unsubscribe; delete the host connector | which capability key, error code and `webhook-id` the host actually uses; resolves the section 6 table |
| G-L4 any real producer or content | G-L1 to G-L3 reviewed; tenant resolution and consent decided | flag off, ACL revoked | audit sample with identifiers only |

Secrets never appear in captures; only the fingerprint is recorded.

## 11. Risks

| Sev | Risk | Mitigation |
|---|---|---|
| P1 | Claiming host interop from spec conformance, or the reverse | separate divergence table; ChatGPT `unknown (not observed)` until G-L3 |
| P1 | Lost or duplicated per-subscriber delivery | materialize-then-ACK; T12 to T15 |
| P1 | Cross-tenant leak through platform-wide streams | fail-closed tenant resolution and re-authorization at materialization (T2) |
| P1 | Secret leakage | client-supplied secrets, fingerprint-only records, T5 |
| P2 | Revocation race | linearization at commit, CAS claim, T17 |
| P2 | SSRF and DNS rebinding | guard at registration and every delivery, T20 |
| P2 | Channels regression | no change to that path |
| P2 | The model's in-memory store hides real database behaviour (isolation level, lock contention) | mctl-api slice would need real transaction tests; not proven here |
| P3 | Spec churn while Beta | pinned commit; isolated `projection.py` and `cursor.py` |
| P3 | Cost | no live runs |

## 12. GO/NO-GO

Proposed verdict: BOUNDED_HOST_PILOT, offline-first. The first approvable slice
is this report, model and tests. A flagged mctl-api slice on the synthetic
stream and any host delivery are separate later gates (section 10).

DEFER triggers (none is "not Final" alone):

- the full spec text cannot be pinned at a commit;
- the model cannot prove T12 to T15 (the fanout contract);
- no target host is observed accepting spec-conformant deliveries, leaving no interoperable consumer;
- the owner rejects adding per-subscription rows to mctl-api's database.

Smallest reversible pilot: the offline slice here; rollback is reverting this PR.
If the owner rejects the verdict, a follow-up sets the manifest to `parked` with
an `activation.trigger` (for example "a target host is observed accepting
spec-conformant deliveries") or to `archived`.

Open questions carried forward: whether ChatGPT accepts a spec-conformant server
(G-L3); the SEP governance state (reported, not observed); SDK availability;
which endpoint would advertise the extension; stream lag and the tunnel
consumer's liveness (not observed).
