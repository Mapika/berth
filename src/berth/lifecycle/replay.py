"""LRU baseline simulator for `serve predict --replay`.

The predictor's success metric - >=30% cold-load reduction vs an LRU baseline  -
is hard to measure from production cold_loaded counters alone because they
already reflect whatever pre-warming was happening at recording time. The
replay tool answers "how would this trace have looked under naive LRU?" so
we can attribute the delta to the predictor.

Pure functions over a list of recorded events; zero daemon coupling.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class ReplayEvent:
    ts: str
    base: str
    adapter: str | None
    cold_loaded: bool


@dataclass(frozen=True)
class ReplayResult:
    total: int
    recorded_cold: int
    lru_cold: int
    slots_per_base: int

    @property
    def recorded_rate(self) -> float:
        return self.recorded_cold / self.total if self.total else 0.0

    @property
    def lru_rate(self) -> float:
        return self.lru_cold / self.total if self.total else 0.0

    @property
    def reduction_pct(self) -> float:
        """Percent cold-load reduction of recorded trace vs LRU baseline.
        Positive means the recorded system beat LRU (i.e. the predictor was
        doing useful pre-warming). Returns 0.0 if the LRU baseline is itself
        zero (degenerate trace with no cold loads to reduce)."""
        if self.lru_cold == 0:
            return 0.0
        return 100.0 * (self.lru_cold - self.recorded_cold) / self.lru_cold


def simulate_lru(
    events: list[ReplayEvent], *, slots_per_base: int = 4,
) -> ReplayResult:
    """Replay `events` through an LRU-only cache.

    Modeled per-base: each base has its own ordered cache of recently-used
    adapters with `slots_per_base` slots. A request is "cold" if the adapter
    wasn't already in that base's cache.

    We exclude bare-base events (adapter=None) from BOTH counters: v2.0's
    predictor only pre-warms adapters, and `cold_loaded` in usage_events
    likewise only flips on adapter hot-loads. Including bare-base traffic
    would dilute the comparison with traffic neither system can affect.
    `total` reflects the comparable subset.
    """
    if slots_per_base <= 0:
        raise ValueError("slots_per_base must be positive")

    adapter_events = [e for e in events if e.adapter is not None]
    sorted_events = sorted(adapter_events, key=lambda e: e.ts)
    caches: dict[str, OrderedDict[str, None]] = {}
    lru_cold = 0
    recorded_cold = 0

    for e in sorted_events:
        assert e.adapter is not None  # filtered above; reassure the type checker
        if e.cold_loaded:
            recorded_cold += 1
        cache = caches.setdefault(e.base, OrderedDict())
        if e.adapter in cache:
            cache.move_to_end(e.adapter)
        else:
            lru_cold += 1
            cache[e.adapter] = None
            if len(cache) > slots_per_base:
                cache.popitem(last=False)

    return ReplayResult(
        total=len(sorted_events),
        recorded_cold=recorded_cold,
        lru_cold=lru_cold,
        slots_per_base=slots_per_base,
    )
