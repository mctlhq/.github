# MCTL events

Contract for event-driven inbound delivery to running Claude Code sessions
(epic mctlhq/.github#87).

```
producer (mctl-telegram, mctl-api) → Valkey Streams (platform-events)
  → mctl-events Channel adapter (mctl-claude-remote) → running Claude session
  → hydration through the source's MCP / GitHub tools → ack_event → XACK
```

## The event is disposable; the source is canonical

An envelope says *what happened and where to look* — never the content. A
Telegram message body, a pull request title or diff stays in its system of record
and is read by Claude through that system's tools, under that system's
authorization. A stale, duplicated or lost event therefore costs nothing: the
consumer can always re-read the current state from the `subject` references.

## Envelope v1

[`schemas/event-envelope.v1.schema.json`](schemas/event-envelope.v1.schema.json);
examples in [`examples/`](examples/) (CI validates both directions).

| Field | Meaning |
|---|---|
| `specversion` | `mctl.events/v1` |
| `id` | stable dedup key derived from the source's own identity (Telegram `event_id`, `X-GitHub-Delivery`) |
| `type` | `<domain>.<entity>.<action>` |
| `source` | producing service |
| `occurred_at` | RFC 3339 with offset, when the source observed the fact |
| `correlation_id` | carried unchanged through every stage and the audit stream |
| `subject` | `kind` plus up to 11 scalar references (no nested values) |

The envelope is closed and capped at 4096 bytes. Consumers enforce the same rules
(`mctl-claude-remote/events/mctl_events/envelope.py`) and reject, audit and
acknowledge anything else so it never loops.

## Streams

| Stream | Producer (ACL user) | Types |
|---|---|---|
| `mctl:events:telegram` | mctl-telegram (`telegram-producer`) | `telegram.message.created`, `telegram.message.edited` |
| `mctl:events:github` | mctl-api (`github-producer`) | `github.pull_request.<action>`, `github.pull_request_review.submitted` |
| `mctl:events:synthetic` | operator probe (`synthetic-producer`) | `synthetic.*` |
| `mctl:events:audit` | every stage | — |

Each stream entry has one field, `envelope`, holding the JSON document. Producers
trim with `XADD ... MAXLEN ~ 10000`. Consumers use one consumer group per session.

## Hydration

| `subject.kind` | Canonical read |
|---|---|
| `telegram.message` | mctl-telegram MCP `get_messages(peer, before_id = message_id + 1, limit = 1)` |
| `github.pull_request` | `gh pr view <number> --repo <repository>` (or GitHub MCP) |

## Delivery guarantees

At-least-once. A producer publishes from a durable outbox after its own commit;
the consumer acknowledges a stream entry only after Claude acknowledges the event,
deduplicates by `id`, and redelivers what was never acknowledged with
`attempt > 1`, so Claude checks current state before repeating a side effect.
