from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

# 12 samples x 5 s heartbeat = 60 s rolling window. The aggregator does
# not enforce the 5 s interval — agents set it. The window length here
# is just "how many of the most recent we keep."
DEFAULT_WINDOW = 12


@dataclass(frozen=True)
class _StampedSample:
    ts: float
    sample: dict[str, Any]


class MetricsAggregator:
    """Leader-side ring of per-node metric samples.

    In-memory, stateless across restarts. Single lock — read and write
    paths both hold it briefly. The hot path is heartbeat ingest at
    O(nodes/5s), so contention is irrelevant in practice.
    """

    def __init__(self, *, window: int = DEFAULT_WINDOW) -> None:
        self._window = window
        self._by_node: dict[int, deque[_StampedSample]] = {}
        self._lock = threading.Lock()

    def ingest(self, *, node_id: int, sample: dict[str, Any], ts: float) -> None:
        with self._lock:
            buf = self._by_node.get(node_id)
            if buf is None:
                buf = deque(maxlen=self._window)
                self._by_node[node_id] = buf
            buf.append(_StampedSample(ts=ts, sample=sample))

    def drop_node(self, node_id: int) -> None:
        with self._lock:
            self._by_node.pop(node_id, None)

    def snapshot(self) -> dict[int, dict[str, Any]]:
        """Latest sample per node. Used by UI + admin."""
        with self._lock:
            return {
                nid: buf[-1].sample
                for nid, buf in self._by_node.items()
                if buf
            }

    def series(
        self, *, node_id: int, key: str, gpu: int | None = None,
    ) -> list[int]:
        """One-dimensional time series for sparklines.

        Known keys: 'gpu_util_pct', 'gpu_mem_used_mb' (require `gpu`),
        'request_rate' (sum of requests_last_window across deployments).
        Unknown keys return [].
        """
        with self._lock:
            buf = self._by_node.get(node_id)
            if not buf:
                return []
            samples = [s.sample for s in buf]
        return [_extract(s, key=key, gpu=gpu) for s in samples]

    def deployment_in_flight(
        self, *, node_id: int, deployment_id: int,
    ) -> int:
        with self._lock:
            buf = self._by_node.get(node_id)
            if not buf:
                return 0
            latest = buf[-1].sample
        for d in latest.get("deployments", []):
            if d.get("deployment_id") == deployment_id:
                return int(d.get("in_flight", 0))
        return 0

    def all_nodes(self) -> list[int]:
        with self._lock:
            return list(self._by_node.keys())


def _extract(sample: dict[str, Any], *, key: str, gpu: int | None) -> int:
    if key == "gpu_util_pct":
        for g in sample.get("gpus", []):
            if g.get("index") == gpu:
                return int(g.get("util_pct", 0))
        return 0
    if key == "gpu_mem_used_mb":
        for g in sample.get("gpus", []):
            if g.get("index") == gpu:
                return int(g.get("mem_used_mb", 0))
        return 0
    if key == "request_rate":
        return sum(
            int(d.get("requests_last_window", 0))
            for d in sample.get("deployments", [])
        )
    return 0
