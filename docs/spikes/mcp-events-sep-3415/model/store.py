"""Transactional in-memory stand-in for mctl-api's existing database.

Provides atomic transactions (rollback on exception), unique-key checks done by
callers inside a transaction, and named crash-injection points.
"""

from __future__ import annotations

import copy
import threading
from contextlib import contextmanager

from .errors import Crash


class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.subscriptions: dict[str, dict] = {}
        self.deliveries: list[dict] = []
        self.positions: dict[str, int] = {}
        self.audit: list[dict] = []
        self.crash_points: set[str] = set()
        self._seq = 0

    def next_id(self, prefix: str) -> str:
        with self.lock:
            self._seq += 1
            return f"{prefix}_{self._seq}"

    @contextmanager
    def transaction(self):
        with self.lock:
            snap = (
                copy.deepcopy(self.subscriptions),
                copy.deepcopy(self.deliveries),
                copy.deepcopy(self.positions),
                copy.deepcopy(self.audit),
                self._seq,
            )
            try:
                yield self
            except BaseException:
                self.subscriptions, self.deliveries, self.positions, self.audit, self._seq = snap
                raise

    def crash_if(self, point: str) -> None:
        """Raise Crash once when `point` was armed (one-shot)."""
        if point in self.crash_points:
            self.crash_points.discard(point)
            raise Crash(point)

    # delivery helpers (call inside a transaction when mutating)
    def delivery_exists(self, sub_id: str, event_id: str) -> bool:
        return any(d["subscription_id"] == sub_id and d["event_id"] == event_id for d in self.deliveries)

    def deliveries_for(self, sub_id: str) -> list[dict]:
        return sorted((d for d in self.deliveries if d["subscription_id"] == sub_id), key=lambda d: d["sub_seq"])

    def record_audit(self, stage: str, *, envelope_id=None, correlation_id=None,
                     subscription_id=None, webhook_id=None, **extra) -> None:
        """Audit record: identifiers only, never content or secrets."""
        self.audit.append({
            "stage": stage, "envelope_id": envelope_id, "correlation_id": correlation_id,
            "subscription_id": subscription_id, "webhook_id": webhook_id, **extra,
        })
