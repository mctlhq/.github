from __future__ import annotations


class FakeClock:
    """Deterministic clock in seconds."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def now_ms(self) -> int:
        return int(self.t * 1000)

    def advance(self, seconds: float) -> None:
        self.t += seconds
