from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from serve_engine.observability.gpu_stats import read_gpu_stats

# Cap the latency ring so a chatty deployment can't grow it unbounded
# between summarize_and_reset calls. 1024 covers 5 s @ 200 QPS comfortably.
_LATENCY_WINDOW = 1024


class InFlightCounter:
    """Per-deployment in-flight request counter. Process-local, not thread-safe;
    callers in the asyncio path can rely on the single-threaded event loop."""

    def __init__(self) -> None:
        self._counts: defaultdict[int, int] = defaultdict(int)

    def start(self, deployment_id: int) -> None:
        self._counts[deployment_id] += 1

    def finish(self, deployment_id: int) -> None:
        n = self._counts.get(deployment_id, 0)
        if n <= 1:
            self._counts.pop(deployment_id, None)
        else:
            self._counts[deployment_id] = n - 1

    def snapshot(self) -> dict[int, int]:
        return dict(self._counts)


@dataclass(frozen=True)
class DeploymentLatencySummary:
    requests_last_window: int
    errors_last_window: int
    latency_p50_ms: int
    latency_p95_ms: int


class LatencyRecorder:
    """Per-deployment recent-latency ring. summarize_and_reset drains it."""

    def __init__(self) -> None:
        self._samples: defaultdict[int, deque[tuple[int, bool]]] = defaultdict(
            lambda: deque(maxlen=_LATENCY_WINDOW)
        )

    def record(
        self, *, deployment_id: int, latency_ms: int, error: bool,
    ) -> None:
        self._samples[deployment_id].append((latency_ms, error))

    def summarize_and_reset(
        self, deployment_id: int,
    ) -> DeploymentLatencySummary:
        samples = list(self._samples.get(deployment_id, ()))
        self._samples.pop(deployment_id, None)
        if not samples:
            return DeploymentLatencySummary(0, 0, 0, 0)
        latencies = sorted(s[0] for s in samples)
        errors = sum(1 for s in samples if s[1])
        return DeploymentLatencySummary(
            requests_last_window=len(samples),
            errors_last_window=errors,
            latency_p50_ms=_percentile(latencies, 50),
            latency_p95_ms=_percentile(latencies, 95),
        )

    def known_deployments(self) -> list[int]:
        return list(self._samples.keys())


def _percentile(sorted_values: list[int], pct: int) -> int:
    """Nearest-rank percentile on a pre-sorted list. Returns 0 on empty."""
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = max(0, min(len(sorted_values) - 1,
                      int(round((pct / 100.0) * (len(sorted_values) - 1)))))
    return sorted_values[rank]


def build_snapshot(
    *,
    in_flight: InFlightCounter,
    latency: LatencyRecorder,
    deployment_models: dict[int, str],
    uptime_s: float,
) -> dict[str, Any]:
    """Assemble the dict carried in Heartbeat.metrics.

    deployment_models maps deployment_id → model name so the leader can
    label time series without consulting its own DB.
    """
    gpus = [
        {
            "index": g.index,
            "mem_used_mb": g.memory_used_mb,
            "mem_total_mb": g.memory_total_mb,
            "util_pct": g.gpu_util_pct,
            "temp_c": 0,
        }
        for g in read_gpu_stats()
    ]
    in_flight_map = in_flight.snapshot()
    deployment_ids = set(in_flight_map) | set(latency.known_deployments())
    deployments = []
    for dep_id in sorted(deployment_ids):
        summary = latency.summarize_and_reset(dep_id)
        deployments.append({
            "deployment_id": dep_id,
            "model_id": deployment_models.get(dep_id, ""),
            "in_flight": in_flight_map.get(dep_id, 0),
            "requests_last_window": summary.requests_last_window,
            "latency_p50_ms": summary.latency_p50_ms,
            "latency_p95_ms": summary.latency_p95_ms,
            "errors_last_window": summary.errors_last_window,
        })
    return {
        "gpus": gpus,
        "deployments": deployments,
        "node": {"uptime_s": uptime_s, "host_load_avg_1m": 0.0},
    }
