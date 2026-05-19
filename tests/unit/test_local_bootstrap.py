from __future__ import annotations

from berth.cluster import host_info as hi
from berth.cluster.local_bootstrap import ensure_local_node
from berth.store import db
from berth.store import node_gpus as node_gpus_store
from berth.store import nodes as nodes_store


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.init_schema(conn)
    return conn


def test_inserts_local_node_with_status_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hi, "collect_host_info",
        lambda: hi.HostInfo(
            cpu_count=4, total_ram_mb=8000, gpu_count=1, total_vram_mb=1024,
            gpus=[hi.GpuInfo(index=0, name="Mock",
                             total_vram_mb=1024, driver_version="x")],
        ),
    )
    conn = _fresh(tmp_path)
    nid = ensure_local_node(conn, agent_version="0.0.1-test")
    n = nodes_store.find_by_label(conn, "local")
    assert n is not None
    assert n.id == nid
    assert n.status == "ready"
    assert n.cpu_count == 4
    assert n.total_vram_mb == 1024
    gpus = node_gpus_store.list_for_node(conn, n.id)
    assert len(gpus) == 1
    assert gpus[0].name == "Mock"


def test_idempotent_on_second_call(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hi, "collect_host_info",
        lambda: hi.HostInfo(
            cpu_count=4, total_ram_mb=8000, gpu_count=0, total_vram_mb=0, gpus=[],
        ),
    )
    conn = _fresh(tmp_path)
    nid1 = ensure_local_node(conn, agent_version="v1")
    nid2 = ensure_local_node(conn, agent_version="v2")
    assert nid1 == nid2
    rows = nodes_store.list_all(conn)
    assert len(rows) == 1
    assert rows[0].agent_version == "v2"


def test_gpu_inventory_refreshed_on_second_call(tmp_path, monkeypatch):
    state = {"gpus": [hi.GpuInfo(index=0, name="A",
                                 total_vram_mb=1000, driver_version=None)]}

    def _info():
        return hi.HostInfo(
            cpu_count=1, total_ram_mb=1, gpu_count=len(state["gpus"]),
            total_vram_mb=sum(g.total_vram_mb for g in state["gpus"]),
            gpus=list(state["gpus"]),
        )

    monkeypatch.setattr(hi, "collect_host_info", _info)
    conn = _fresh(tmp_path)
    nid = ensure_local_node(conn, agent_version="v1")
    assert len(node_gpus_store.list_for_node(conn, nid)) == 1

    # Swap to two GPUs and re-bootstrap.
    state["gpus"] = [
        hi.GpuInfo(index=0, name="A", total_vram_mb=1000, driver_version=None),
        hi.GpuInfo(index=1, name="B", total_vram_mb=2000, driver_version=None),
    ]
    ensure_local_node(conn, agent_version="v1")
    gpus = node_gpus_store.list_for_node(conn, nid)
    assert [g.gpu_index for g in gpus] == [0, 1]
