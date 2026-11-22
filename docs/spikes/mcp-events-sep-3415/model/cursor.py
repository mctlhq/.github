"""Cursor rules (events.mdx at ext-events@17c0c64a).

- null / absent cursor: start from now, no backfill, maxAgeMs ignored.
- cursor + maxAgeMs: start from the later of the cursor and now - maxAgeMs.
- start earlier than the oldest retained entry, or earlier than now - maxAgeMs:
  deliver from the earliest available position, truncated=true, fresh cursor.
  Never a JSON-RPC error. The internal below_retention flag is audit-only.
"""

from __future__ import annotations

import base64

from .projection import InvalidEnvelope, project
from .errors import INVALID_PARAMS, RpcError


def encode(entry_id: int) -> str:
    return base64.urlsafe_b64encode(f"v1:{entry_id}".encode()).decode()


def decode(cursor: str) -> int:
    try:
        tag, _, num = base64.urlsafe_b64decode(cursor.encode()).decode().partition(":")
        if tag != "v1":
            raise ValueError
        return int(num)
    except Exception:
        raise RpcError(INVALID_PARAMS, "malformed cursor") from None


def resolve_start(stream, cursor, max_age_ms, now_ms: int) -> dict:
    """Return {after, truncated, below_retention}. `after` is an entry id: deliver ids > after."""
    if cursor is None:
        return {"after": stream.tail(), "truncated": False, "below_retention": False}
    after = decode(cursor)
    below_retention = after < stream.last_trimmed_id
    truncated = below_retention
    if max_age_ms is not None:
        cutoff = now_ms - max_age_ms
        for eid, ts, _ in stream.read_after(after):
            if ts < cutoff:
                after = max(after, eid)  # skip entries older than the window
                truncated = True
    if below_retention:
        after = max(after, stream.last_trimmed_id)
    return {"after": after, "truncated": truncated, "below_retention": below_retention}


def poll(stream, cursor, max_age_ms, max_events: int, now_ms: int, audit=None, accept=None) -> dict:
    start = resolve_start(stream, cursor, max_age_ms, now_ms)
    if start["below_retention"] and audit is not None:
        audit("below_retention")
    avail = stream.read_after(start["after"])
    events, last, has_more = [], start["after"], False
    for idx, (eid, _, env) in enumerate(avail):
        if len(events) >= max_events:
            has_more = any(accept is None or accept(e) for _, _, e in avail[idx:])
            break
        last = eid
        if accept is not None and not accept(env):
            continue
        try:
            events.append(project(env, cursor=encode(eid)))
        except InvalidEnvelope:
            if audit is not None:
                audit("invalid_envelope_skipped")
    return {"events": events, "cursor": encode(last), "hasMore": has_more, "truncated": start["truncated"]}
