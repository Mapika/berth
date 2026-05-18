from __future__ import annotations

from serve_engine.lifecycle.placement import (
    AllocatedDeployment,
    EvictThenFit,
    Fit,
    NoRoom,
    PlacementRequest,
    plan_placement_multi,
)
from serve_engine.lifecycle.topology import GPUInfo, Topology


def _topo(gpus_mb: list[int]) -> Topology:
    return Topology(
        gpus=[GPUInfo(index=i, name="g", total_mb=mb) for i, mb in enumerate(gpus_mb)],
        _islands={i: frozenset([i]) for i in range(len(gpus_mb))},
    )


def test_picks_first_node_that_fits():
    nodes = {
        1: (_topo([10_000]), []),
        2: (_topo([100_000]), []),
    }
    req = PlacementRequest(tensor_parallel=1, vram_reserved_mb=50_000, model_name="m")
    node_id, decision = plan_placement_multi(nodes, req)
    assert node_id == 2
    assert isinstance(decision, Fit)
    assert decision.gpu_ids == [0]


def test_no_room_when_nothing_fits():
    nodes = {
        1: (_topo([1_000]), []),
        2: (_topo([1_000]), []),
    }
    req = PlacementRequest(tensor_parallel=1, vram_reserved_mb=50_000, model_name="m")
    node_id, decision = plan_placement_multi(nodes, req)
    assert node_id is None
    assert isinstance(decision, NoRoom)


def test_prefers_node_with_more_headroom_on_tie():
    nodes = {
        1: (_topo([60_000]), []),
        2: (_topo([100_000]), []),
    }
    req = PlacementRequest(tensor_parallel=1, vram_reserved_mb=50_000, model_name="m")
    node_id, _ = plan_placement_multi(nodes, req)
    assert node_id == 2


def test_falls_back_to_evict_then_fit_when_no_direct_fit():
    # Node 1 is full but has an evictable deployment that frees enough.
    # Node 2 is dead-on-arrival (everything pinned, no room).
    pinned = AllocatedDeployment(
        id=99, gpu_ids=[0], vram_reserved_mb=60_000, pinned=True,
    )
    evictable = AllocatedDeployment(
        id=42, gpu_ids=[0], vram_reserved_mb=60_000, pinned=False,
    )
    nodes = {
        1: (_topo([80_000]), [evictable]),
        2: (_topo([80_000]), [pinned]),
    }
    req = PlacementRequest(tensor_parallel=1, vram_reserved_mb=50_000, model_name="m")
    node_id, decision = plan_placement_multi(nodes, req)
    assert node_id == 1
    assert isinstance(decision, EvictThenFit)
    assert 42 in decision.evict_ids


def test_empty_nodes_yields_no_room():
    req = PlacementRequest(tensor_parallel=1, vram_reserved_mb=1, model_name="m")
    node_id, decision = plan_placement_multi({}, req)
    assert node_id is None
    assert isinstance(decision, NoRoom)
