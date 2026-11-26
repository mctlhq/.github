# Spec pin

Every protocol rule the model implements is tied to this pin. A rule that cannot
be quoted at the pin is tagged `[INFERRED]` and is not tested as a spec
requirement.

- Source: `modelcontextprotocol/ext-events`, `specification/beta/events/events.mdx`
- Commit: `17c0c64a5edb89dc5081d00ed50c4f836bbb67ff` (2026-11-19T15:41:56Z)
- Introduced by: `24727f38a180b1bde5df1c6eae580ea0a0b703ee` ("Add Events extension specification at Beta")
- Header: "Stage: Beta", extension identifier `io.modelcontextprotocol/events`, protocol revision `2026-07-28` and later
- Retrieved: 2026-11-22, raw file at the commit above, read in two ranges (characters 0 to 100000 and 100000 to the end, 100380 characters in total) through a summarizing fetch tool. Quotes below are the short phrases that tool returned verbatim. A re-read of the raw file by a person is still advisable before any activation gate. `[DOC]`

## Rules, quotes and where the model enforces them

| Rule | Quote or paraphrase `[DOC]` | Model | Test |
|---|---|---|---|
| Capability | advertised in `capabilities.extensions`; "A client MUST NOT send them to a server that has not advertised the extension"; `-32601` otherwise | `server.py`, `client.py` | T16 |
| Subscription key | "The subscription key is `(principal, delivery.url, name, arguments)`"; "`principal` comes from the authorization layer" | `subscriptions.py` | T2, T6 |
| Secret | "`delivery.secret` is REQUIRED. The client supplies the HMAC signing secret, and the server never generates one"; `whsec_` plus base64 of 24 to 64 bytes; `InvalidParams` otherwise | `webhooks.py` | T3 |
| Rotation | client refresh with a new secret; server "SHOULD dual-sign ... for a short grace window"; "There is no server-initiated rotation." | `subscriptions.py`, `webhooks.py` | T4 |
| webhook-id | "MUST NOT be derived from `eventId`", "reused across retries of that message", receivers "SHOULD deduplicate on `webhook-id`"; "the same `eventId` delivered on two subscriptions is two deliveries" | `webhooks.py`, `fanout.py` | T6, T7 |
| Cursors | "`null` means "start from now""; `truncated: true` is "the single signal" of a possible gap, with a fresh `cursor`; with `maxAgeMs` start from the later of cursor and `now - maxAgeMs`; `maxAgeMs` ignored for `null` | `cursor.py` | T9, T10, T11 |
| Retry | independent per event, exponential backoff, capped attempts and window; `410` and `413` non-retryable | `delivery.py` | T15 |
| Verification | delivery starts after a constant-time echo match; failure is `CallbackEndpointError` `-32027` | `delivery.py` | T19 |
| Webhook safety | receivers SHOULD reject a `webhook-timestamp` older than 5 minutes; non-globally-routable IPs rejected; "MUST NOT follow HTTP redirects" | `recipient.py`, `callback_guard.py` | T8, T20 |
| Ordering | "For poll and push, events within a single subscription are delivered in the order the server produces them"; webhook order is best-effort because retries reorder; "No ordering is guaranteed across subscriptions in any mode." | `fanout.py` (materialization order per subscription only) | T11, T15 |
| Ending | webhook clients may call `events/unsubscribe` or stop renewing; a subscription without expiry must be unsubscribed explicitly | `server.py`, `delivery.py` | T17, T18 |
| Rate limiting | servers may set polling frequency through `nextPollMs` | not modelled | none |

## Not defined by the spec (MCTL choices, tagged `[INFERRED]`)

- The shape of the `verification` echo body (`{"challenge": ...}`).
- A recipient answering `200` with `duplicate: true` on a replayed `webhook-id`.
- `mctl/ack_event` (the processing ACK) is an MCTL tool, not an `events/*` method.
- The undelivered-row quota and the `gap` row inserted on quota resume.
- `events/stream` is modelled as activation (cursor and `truncated`) only; the push notifications are not simulated.

## Maturity, three separate rows

| Row | State | Tag |
|---|---|---|
| SEP governance (plan.modelcontextprotocol.io) | reported `final` 2026-11-19 by the review of 2026-11-22; the page rendered no content to the investigator | reported, `unknown (not observed)` |
| Voting tracker (voting.modelcontextprotocol.io/p/seps/3415) | `Scheduled`; linked PR `modelcontextprotocol/modelcontextprotocol#3415` `Merged`; meeting 2026-11-04 passed | `[DOC]` 2026-11-22 |
| Extension maturity | `Beta` at `ext-events@17c0c64a` | `[DOC]` |

SDK packages implementing `io.modelcontextprotocol/events`: none observed (`unknown (not observed)`; "none published" is not asserted).
