"""In-memory fake of the Valkey Streams subset the adapter uses.

XADD with approximate MAXLEN, XREADGROUP, XACK, XAUTOCLAIM, plus the read-only
helpers poll clients need. Entry ids are integers (the real id is `ms-seq`).
"""

from __future__ import annotations

import copy


class Stream:
    def __init__(self, name: str, clock, maxlen: int = 10000) -> None:
        self.name = name
        self.clock = clock
        self.maxlen = maxlen
        self.entries: list[tuple[int, int, dict]] = []  # (id, ts_ms, envelope)
        self.last_id = 0
        self.last_trimmed_id = 0  # highest id removed by MAXLEN
        self.groups: dict[str, dict] = {}

    def xadd(self, envelope: dict) -> int:
        self.last_id += 1
        self.entries.append((self.last_id, self.clock.now_ms(), copy.deepcopy(envelope)))
        while len(self.entries) > self.maxlen:
            self.last_trimmed_id = self.entries.pop(0)[0]
        return self.last_id

    def _group(self, group: str) -> dict:
        # A group created on first use starts at the current tail ("$").
        return self.groups.setdefault(group, {"last_delivered": self.last_id, "pel": {}})

    def xgroup_create(self, group: str, start: int | None = None) -> None:
        self.groups[group] = {"last_delivered": self.last_id if start is None else start, "pel": {}}

    def xreadgroup(self, group: str, consumer: str, count: int = 100) -> list[tuple[int, int, dict]]:
        g = self._group(group)
        out = [e for e in self.entries if e[0] > g["last_delivered"]][:count]
        for eid, _, _ in out:
            g["pel"][eid] = consumer
            g["last_delivered"] = eid
        return copy.deepcopy(out)

    def xautoclaim(self, group: str, consumer: str, count: int = 100) -> list[tuple[int, int, dict]]:
        """Claim every pending entry (min-idle is not modelled)."""
        g = self._group(group)
        out = [e for e in self.entries if e[0] in g["pel"]][:count]
        for eid, _, _ in out:
            g["pel"][eid] = consumer
        return copy.deepcopy(out)

    def xack(self, group: str, entry_id: int) -> None:
        self._group(group)["pel"].pop(entry_id, None)

    def pending_count(self, group: str) -> int:
        return len(self._group(group)["pel"])

    def tail(self) -> int:
        return self.last_id

    def first_retained(self) -> int | None:
        return self.entries[0][0] if self.entries else None

    def read_after(self, entry_id: int) -> list[tuple[int, int, dict]]:
        return copy.deepcopy([e for e in self.entries if e[0] > entry_id])
