from berth.lifecycle.placement import (
    AllocatedDeployment,
    EvictThenFit,
    Fit,
    NoRoom,
    PlacementRequest,
    plan_placement,
)
from berth.lifecycle.topology import GPUInfo, Topology


def _topo(n: int, nvlink: bool = True) -> Topology:
    gpus = [GPUInfo(index=i, name="H100", total_mb=80 * 1024) for i in range(n)]
    if nvlink and n > 1:
        islands = {i: frozenset(range(n)) for i in range(n)}
    else:
        islands = {i: frozenset({i}) for i in range(n)}
    return Topology(gpus=gpus, _islands=islands)


def test_fit_on_free_gpu():
    req = PlacementRequest(
        tensor_parallel=1, vram_reserved_mb=20_000, model_name="x",
    )
    decision = plan_placement(_topo(2), allocated=[], request=req)
    assert isinstance(decision, Fit)
    assert decision.gpu_ids == [0]


def test_fit_tp2_on_nvlink_pair():
    req = PlacementRequest(
        tensor_parallel=2, vram_reserved_mb=70_000, model_name="x",
    )
    decision = plan_placement(_topo(4), allocated=[], request=req)
    assert isinstance(decision, Fit)
    assert len(decision.gpu_ids) == 2


def test_no_room_when_total_vram_insufficient():
    req = PlacementRequest(
        tensor_parallel=1, vram_reserved_mb=200_000, model_name="x",
    )
    decision = plan_placement(_topo(1), allocated=[], request=req)
    assert isinstance(decision, NoRoom)


def test_evict_then_fit_lru_first():
    topo = _topo(2)
    alloc = [
        AllocatedDeployment(id=1, gpu_ids=[0], vram_reserved_mb=70_000, pinned=False),
    ]
    req = PlacementRequest(
        tensor_parallel=1, vram_reserved_mb=70_000, model_name="x",
    )
    # GPU 1 free -> fits there
    decision = plan_placement(topo, allocated=alloc, request=req)
    assert isinstance(decision, Fit)
    assert decision.gpu_ids == [1]

    # Now both GPUs are full
    alloc2 = [
        *alloc,
        AllocatedDeployment(id=2, gpu_ids=[1], vram_reserved_mb=70_000, pinned=False),
    ]
    decision = plan_placement(topo, allocated=alloc2, request=req)
    assert isinstance(decision, EvictThenFit)
    assert decision.evict_ids == [1]
    assert decision.gpu_ids == [0]


def test_pinned_blocks_eviction():
    topo = _topo(1)
    alloc = [
        AllocatedDeployment(id=1, gpu_ids=[0], vram_reserved_mb=70_000, pinned=True),
    ]
    req = PlacementRequest(
        tensor_parallel=1, vram_reserved_mb=70_000, model_name="x",
    )
    decision = plan_placement(topo, allocated=alloc, request=req)
    assert isinstance(decision, NoRoom)


def test_tp_not_power_of_two():
    topo = _topo(4)
    req = PlacementRequest(
        tensor_parallel=3, vram_reserved_mb=20_000, model_name="x",
    )
    decision = plan_placement(topo, allocated=[], request=req)
    assert isinstance(decision, NoRoom)
    assert "power of 2" in decision.reason.lower()


def test_tp_exceeds_gpu_count():
    topo = _topo(1)
    req = PlacementRequest(
        tensor_parallel=2, vram_reserved_mb=10_000, model_name="x",
    )
    decision = plan_placement(topo, allocated=[], request=req)
    assert isinstance(decision, NoRoom)
    assert "exceeds gpu count" in decision.reason.lower()
