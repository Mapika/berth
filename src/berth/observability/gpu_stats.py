from __future__ import annotations

from dataclasses import dataclass

try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        import pynvml  # type: ignore[import-untyped]
except Exception:  # pragma: no cover
    pynvml = None  # type: ignore[assignment]


@dataclass(frozen=True)
class GPUSnapshot:
    index: int
    memory_used_mb: int
    memory_total_mb: int
    gpu_util_pct: int
    power_w: int


def read_gpu_stats() -> list[GPUSnapshot]:
    """Live per-GPU memory + utilization + power. Empty list if pynvml absent."""
    if pynvml is None:
        return []
    try:
        pynvml.nvmlInit()
    except Exception:
        return []
    out: list[GPUSnapshot] = []
    for i in range(pynvml.nvmlDeviceGetCount()):
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        try:
            power_mw = pynvml.nvmlDeviceGetPowerUsage(h)
        except Exception:
            power_mw = 0
        out.append(GPUSnapshot(
            index=i,
            memory_used_mb=int(mem.used) // 1024 // 1024,
            memory_total_mb=int(mem.total) // 1024 // 1024,
            gpu_util_pct=int(util.gpu),
            power_w=int(power_mw) // 1000,
        ))
    return out


def read_compute_process_vram() -> dict[int, int]:
    """Return {host_pid: vram_mb_total} summed across visible GPUs.

    Uses NVML's CUDA-running-processes view. A process on multiple GPUs
    appears once per GPU and contributes to one entry by sum. Returns an
    empty dict if pynvml is unavailable.
    """
    if pynvml is None:
        return {}
    try:
        pynvml.nvmlInit()
    except Exception:
        return {}
    totals: dict[int, int] = {}
    for i in range(pynvml.nvmlDeviceGetCount()):
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
        try:
            procs = pynvml.nvmlDeviceGetComputeRunningProcesses(h)
        except Exception:
            continue
        for p in procs:
            used = getattr(p, "usedGpuMemory", None) or 0
            # NVML uses a sentinel for "no data"; older drivers expose it
            # as a very large int, which we treat as 0.
            if used > (1 << 60):
                used = 0
            totals[int(p.pid)] = totals.get(int(p.pid), 0) + int(used) // 1024 // 1024
    return totals
