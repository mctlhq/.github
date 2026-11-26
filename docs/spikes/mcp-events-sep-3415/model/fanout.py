"""Materialize-then-ACK fanout.

For each stream entry, one store transaction inserts one delivery row per matching
active subscription (unique on (subscription_id, event_id)) with a freshly minted
webhook-id; only after the commit is the entry XACKed. The stream PEL therefore
never waits on a recipient.
"""

from __future__ import annotations

import json

from . import cursor as cursor_mod
from . import webhooks
from .projection import InvalidEnvelope, project
from .subscriptions import matches


class Fanout:
    def __init__(self, stream, store, config, tenant_of, group="mcp-events-relay", consumer="relay-1") -> None:
        self.stream, self.store, self.config = stream, store, config
        self.tenant_of = tenant_of  # subject -> tenant or None (fail closed)
        self.group, self.consumer = group, consumer
        if group not in stream.groups:
            stream.xgroup_create(group, start=0)  # the adapter group exists before producers publish

    def _undelivered(self, sub_id: str) -> int:
        return sum(1 for d in self.store.deliveries_for(sub_id)
                   if d["state"] in ("pending", "retry", "inflight"))

    def _insert(self, sub: dict, event_id: str, kind: str, payload: dict, envelope: dict | None) -> None:
        if self.store.delivery_exists(sub["id"], event_id):
            return  # ON CONFLICT DO NOTHING
        wid = webhooks.mint_webhook_id()
        self.store.deliveries.append({
            "subscription_id": sub["id"], "event_id": event_id, "kind": kind, "payload": payload,
            "webhook_id": wid, "sub_seq": sub["next_seq"], "state": "pending", "attempts": 0,
            "next_attempt_at": 0.0, "first_attempt_at": None, "claimed_at": None, "processed": False,
            "correlation_id": (envelope or {}).get("correlation_id"),
        })
        sub["next_seq"] += 1
        self.store.record_audit("materialized", envelope_id=event_id if kind == "event" else None,
                                correlation_id=(envelope or {}).get("correlation_id"),
                                subscription_id=sub["id"], webhook_id=wid, kind=kind)

    def materialize_entry(self, entry_id: int, env: dict, only_sub: str | None = None) -> int:
        """One transaction per entry. Returns rows inserted for this call."""
        before = len(self.store.deliveries)
        with self.store.transaction():
            try:
                occ = project(env, cursor=cursor_mod.encode(entry_id))
            except InvalidEnvelope:
                self.store.record_audit("rejected_envelope", envelope_id=str(env.get("id"))[:64]
                                        if isinstance(env, dict) else None, entry=entry_id)
                occ = None
            if occ is not None:
                tenant = self.tenant_of(env["subject"])
                for sub in list(self.store.subscriptions.values()):
                    if only_sub and sub["id"] != only_sub:
                        continue
                    if sub["state"] != "active" or entry_id <= sub["start_after"]:
                        continue
                    if tenant is None or tenant != sub["tenant"] or not matches(sub, env):
                        continue
                    if self._undelivered(sub["id"]) >= self.config.per_subscription_quota:
                        if sub["gap_from"] is None:
                            sub["gap_from"] = entry_id
                            self.store.record_audit("quota_gap_started", subscription_id=sub["id"])
                        continue
                    if sub["gap_from"] is not None:
                        self._insert(sub, f"gap:{sub['id']}:{sub['gap_from']}", "gap",
                                     {"type": "gap", "cursor": cursor_mod.encode(entry_id)}, None)
                        sub["gap_from"] = None
                    self._insert(sub, env["id"], "event", occ, env)
            self.store.crash_if("before_commit")
            if not only_sub:
                self.store.positions[self.stream.name] = entry_id
        return len(self.store.deliveries) - before

    def run_once(self, count: int = 100) -> int:
        """Re-materialize pending entries (crash recovery), then read new ones."""
        n = 0
        batch = self.stream.xautoclaim(self.group, self.consumer, count)
        batch += self.stream.xreadgroup(self.group, self.consumer, count)
        seen = set()
        for entry_id, _, env in batch:
            if entry_id in seen:
                continue
            seen.add(entry_id)
            self.materialize_entry(entry_id, env)
            self.store.crash_if("after_commit")  # committed, not yet XACKed
            self.stream.xack(self.group, entry_id)
            n += 1
        return n


def scan_for(text_sources, needles) -> list[str]:
    """Helper for the hygiene test: which needles appear in the JSON of any source."""
    hits = []
    for src in text_sources:
        blob = json.dumps(src, default=str)
        hits += [n for n in needles if n and n in blob]
    return hits


def backfill(fan: Fanout, sub_id: str, start_after: int, truncated: bool) -> None:
    """Webhook subscribe/resume with a cursor: replay retained entries for this one subscription."""
    if truncated:
        with fan.store.transaction():
            sub = fan.store.subscriptions[sub_id]
            fan._insert(sub, f"gap:{sub_id}:{start_after}", "gap",
                        {"type": "gap", "cursor": cursor_mod.encode(start_after)}, None)
    for entry_id, _, env in fan.stream.read_after(start_after):
        fan.materialize_entry(entry_id, env, only_sub=sub_id)
