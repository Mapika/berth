from __future__ import annotations

from dataclasses import dataclass

# Anything below this is treated as "doesn't fit" — leaves headroom for
# CUDA fragmentation and activation memory that's hard to predict from
# weight size alone.
SAFETY_MARGIN_MB = 1024


@dataclass(frozen=True)
class DeploymentCandidate:
    deployment_id: int
    node_id: int
    model_required_mb: int


@dataclass(frozen=True)
class NodeSignals:
    """Aggregator-derived snapshot for one node, at scoring time."""
    node_id: int
    mem_free_mb: int
    in_flight: int
    latency_p95_ms: int


@dataclass(frozen=True)
class RoutingRequest:
    affinity_key: str | None
    affinity_node_id: int | None = None


def default_scorer(
    *,
    candidates: list[DeploymentCandidate],
    signals_by_node: dict[int, NodeSignals],
    request: RoutingRequest,
) -> list[DeploymentCandidate]:
    """Return candidates ranked best-first.

    Hard filter: drop candidates whose model wouldn't fit in
    (mem_free - SAFETY_MARGIN_MB) on their node. Missing signals are
    treated as worst-case (mem_free=0, in_flight=∞, p95=∞), which means
    no-signal nodes fail the memory filter and get dropped.

    Rank by (affinity_hit, -in_flight, -p95_latency_ms) lexicographically,
    larger-is-better.
    """
    if not candidates:
        return []

    scored: list[tuple[tuple, DeploymentCandidate]] = []
    for c in candidates:
        s = signals_by_node.get(c.node_id)
        if s is None:
            mem_free = 0
            in_flight = 10**9
            p95 = 10**9
        else:
            mem_free = s.mem_free_mb
            in_flight = s.in_flight
            p95 = s.latency_p95_ms
        if mem_free - SAFETY_MARGIN_MB < c.model_required_mb:
            continue
        affinity_hit = int(
            request.affinity_node_id is not None
            and c.node_id == request.affinity_node_id
        )
        key = (affinity_hit, -in_flight, -p95)
        scored.append((key, c))

    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [c for _, c in scored]
