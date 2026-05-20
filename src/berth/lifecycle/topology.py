from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import cache

try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        import pynvml  # type: ignore[import-untyped]
except Exception:  # pragma: no cover
    pynvml = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GPUInfo:
    index: int
    name: str
    total_mb: int
    # CUDA compute capability as "{major}.{minor}" (e.g., "9.0" for H100,
    # "12.0" for Blackwell).
    # Default is the empty marker for tests / mocks that don't care.
    compute_cap: str = ""


@dataclass(frozen=True)
class Topology:
    gpus: list[GPUInfo]
    # Map gpu_index -> frozenset of NVLink-peer indices (including self).
    _islands: dict[int, frozenset[int]] = field(default_factory=dict)

    def nvlink_island(self, index: int) -> frozenset[int]:
        return self._islands.get(index, frozenset({index}))


def _build_islands(count: int) -> dict[int, frozenset[int]]:
    """Group GPUs into NVLink-connected sets via union-find."""
    parent = list(range(count))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    if pynvml is None:
        return {i: frozenset({i}) for i in range(count)}

    handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(count)]
    for i in range(count):
        for j in range(i + 1, count):
            try:
                rel = pynvml.nvmlDeviceGetTopologyCommonAncestor(handles[i], handles[j])
            except Exception:
                continue  # nosec
            if rel == pynvml.NVML_TOPOLOGY_NVLINK:
                union(i, j)

    islands: dict[int, set[int]] = {}
    for i in range(count):
        islands.setdefault(find(i), set()).add(i)
    out: dict[int, frozenset[int]] = {}
    for members in islands.values():
        s = frozenset(members)
        for m in members:
            out[m] = s
    return out


@cache
def read_topology() -> Topology:
    """Enumerate GPUs and detect NVLink islands. Cached for the process lifetime."""
    if pynvml is None:
        log.warning("pynvml unavailable - no GPUs visible")
        return Topology(gpus=[], _islands={})

    try:
        pynvml.nvmlInit()
    except Exception as e:
        log.warning("nvml unavailable - no GPUs visible: %s", e)
        return Topology(gpus=[], _islands={})
    count = pynvml.nvmlDeviceGetCount()
    gpus: list[GPUInfo] = []
    for i in range(count):
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
        name_raw = pynvml.nvmlDeviceGetName(h)
        name = name_raw.decode() if isinstance(name_raw, bytes) else str(name_raw)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        try:
            major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(h)
            cap = f"{major}.{minor}"
        except Exception:
            cap = ""
        gpus.append(GPUInfo(
            index=i, name=name, total_mb=int(mem.total) // 1024 // 1024,
            compute_cap=cap,
        ))
    islands = _build_islands(count)
    return Topology(gpus=gpus, _islands=islands)


def reset_cache() -> None:
    """For tests / re-detection after driver hot-plug."""
    read_topology.cache_clear()
