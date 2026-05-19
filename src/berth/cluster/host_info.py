from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GpuInfo:
    index: int
    name: str
    total_vram_mb: int
    driver_version: str | None


@dataclass(frozen=True)
class HostInfo:
    cpu_count: int
    total_ram_mb: int
    gpu_count: int
    total_vram_mb: int
    gpus: list[GpuInfo]


def _collect_ram_mb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def _collect_gpus() -> list[GpuInfo]:
    try:
        import pynvml  # type: ignore[import-untyped]
    except ImportError:
        return []
    try:
        pynvml.nvmlInit()
    except pynvml.NVMLError:
        return []
    try:
        driver = pynvml.nvmlSystemGetDriverVersion()
        if isinstance(driver, bytes):
            driver = driver.decode()
        out: list[GpuInfo] = []
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode()
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            out.append(GpuInfo(
                index=i, name=name,
                total_vram_mb=int(mem.total // (1024 * 1024)),
                driver_version=driver,
            ))
        return out
    finally:
        try:
            pynvml.nvmlShutdown()
        except pynvml.NVMLError:
            pass


def collect_host_info() -> HostInfo:
    gpus = _collect_gpus()
    return HostInfo(
        cpu_count=os.cpu_count() or 1,
        total_ram_mb=_collect_ram_mb(),
        gpu_count=len(gpus),
        total_vram_mb=sum(g.total_vram_mb for g in gpus),
        gpus=gpus,
    )
