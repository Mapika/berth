from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from berth.lifecycle.plan import _is_power_of_two
from berth.lifecycle.topology import Topology


@dataclass(frozen=True)
class AllocatedDeployment:
    id: int
    gpu_ids: list[int]
    vram_reserved_mb: int
    pinned: bool


@dataclass(frozen=True)
class PlacementRequest:
    tensor_parallel: int
    vram_reserved_mb: int
    model_name: str


@dataclass(frozen=True)
class Fit:
    gpu_ids: list[int]


@dataclass(frozen=True)
class EvictThenFit:
    evict_ids: list[int]
    gpu_ids: list[int]


@dataclass(frozen=True)
class NoRoom:
    reason: str


Decision = Fit | EvictThenFit | NoRoom


def _available_mb(topo: Topology, allocated: list[AllocatedDeployment]) -> dict[int, int]:
    avail = {g.index: g.total_mb for g in topo.gpus}
    for a in allocated:
        share = a.vram_reserved_mb // len(a.gpu_ids)
        for g in a.gpu_ids:
            avail[g] = max(0, avail.get(g, 0) - share)
    return avail


def _try_fit(
    topo: Topology,
    avail: dict[int, int],
    req: PlacementRequest,
) -> list[int] | None:
    if not _is_power_of_two(req.tensor_parallel):
        return None
    per_gpu = req.vram_reserved_mb // req.tensor_parallel

    # Single-GPU case: any free-enough GPU works.
    if req.tensor_parallel == 1:
        for g in sorted(avail, key=lambda i: -avail[i]):
            if avail[g] >= per_gpu:
                return [g]
        return None

    # TP > 1: need NVLink-connected GPUs.
    seen_islands: set[frozenset[int]] = set()
    for island_seed in avail:
        island = topo.nvlink_island(island_seed)
        if island in seen_islands:
            continue
        seen_islands.add(island)
        candidates = [g for g in sorted(island) if avail.get(g, 0) >= per_gpu]
        if len(candidates) < req.tensor_parallel:
            continue
        for combo in combinations(candidates, req.tensor_parallel):
            return list(combo)
    return None


def plan_placement(
    topo: Topology,
    *,
    allocated: list[AllocatedDeployment],
    request: PlacementRequest,
) -> Decision:
    if not _is_power_of_two(request.tensor_parallel):
        return NoRoom(
            reason=f"tensor_parallel={request.tensor_parallel} is not a power of 2"
        )
    if request.tensor_parallel > len(topo.gpus):
        return NoRoom(
            reason=(
                f"tensor_parallel={request.tensor_parallel} "
                f"exceeds GPU count {len(topo.gpus)}"
            )
        )

    avail = _available_mb(topo, allocated)
    fit = _try_fit(topo, avail, request)
    if fit is not None:
        return Fit(gpu_ids=fit)

    # Try evicting auto (non-pinned) deployments in the order given (caller orders LRU).
    evictable = [a for a in allocated if not a.pinned]
    evicted_ids: list[int] = []
    for victim in evictable:
        share = victim.vram_reserved_mb // len(victim.gpu_ids)
        for g in victim.gpu_ids:
            avail[g] = min(
                topo.gpus[g].total_mb if g < len(topo.gpus) else 0,
                avail.get(g, 0) + share,
            )
        evicted_ids.append(victim.id)
        fit = _try_fit(topo, avail, request)
        if fit is not None:
            return EvictThenFit(evict_ids=evicted_ids, gpu_ids=fit)

    return NoRoom(
        reason=(
            f"cannot place {request.model_name!r}: needs "
            f"{request.vram_reserved_mb} MB across {request.tensor_parallel} GPUs; "
            "no fit even after evicting all auto deployments"
        )
    )


def _max_free_mb(topo: Topology, allocated: list[AllocatedDeployment]) -> int:
    avail = _available_mb(topo, allocated)
    return max(avail.values(), default=0)


def plan_placement_multi(
    nodes: dict[int, tuple[Topology, list[AllocatedDeployment]]],
    request: PlacementRequest,
) -> tuple[int | None, Decision]:
    """Pick a (node_id, Decision) pair across multiple ready nodes.

    Strategy: rank candidate nodes by free VRAM headroom (descending) and
    try each via the single-node planner. Return the first Fit; otherwise
    fall back to the first EvictThenFit; otherwise NoRoom.
    """
    if not nodes:
        return None, NoRoom(reason="no nodes available")

    ordered = sorted(
        nodes.items(),
        key=lambda kv: -_max_free_mb(kv[1][0], kv[1][1]),
    )
    evict_fallback: tuple[int, EvictThenFit] | None = None
    last_no_room: NoRoom | None = None
    for node_id, (topo, allocated) in ordered:
        decision = plan_placement(topo, allocated=allocated, request=request)
        if isinstance(decision, Fit):
            return node_id, decision
        if isinstance(decision, EvictThenFit) and evict_fallback is None:
            evict_fallback = (node_id, decision)
        elif isinstance(decision, NoRoom):
            last_no_room = decision
    if evict_fallback is not None:
        return evict_fallback[0], evict_fallback[1]
    return None, last_no_room or NoRoom(reason="no node has room")
