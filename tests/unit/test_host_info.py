from __future__ import annotations

from berth.cluster import host_info as hi
from berth.cluster.host_info import HostInfo, collect_host_info


def test_collect_host_info_returns_populated_struct(monkeypatch):
    monkeypatch.setattr(
        hi, "_collect_gpus",
        lambda: [
            hi.GpuInfo(index=0, name="Mock", total_vram_mb=1024, driver_version="x"),
        ],
    )
    info = collect_host_info()
    assert isinstance(info, HostInfo)
    assert info.cpu_count >= 1
    assert info.total_ram_mb > 0
    assert info.gpu_count == 1
    assert info.total_vram_mb == 1024
    assert info.gpus[0].name == "Mock"


def test_collect_host_info_handles_no_gpus(monkeypatch):
    monkeypatch.setattr(hi, "_collect_gpus", lambda: [])
    info = collect_host_info()
    assert info.gpu_count == 0
    assert info.total_vram_mb == 0
    assert info.gpus == []


def test_collect_host_info_sums_multiple_gpus(monkeypatch):
    monkeypatch.setattr(
        hi, "_collect_gpus",
        lambda: [
            hi.GpuInfo(index=0, name="A", total_vram_mb=1000, driver_version=None),
            hi.GpuInfo(index=1, name="B", total_vram_mb=2000, driver_version=None),
        ],
    )
    info = collect_host_info()
    assert info.gpu_count == 2
    assert info.total_vram_mb == 3000
