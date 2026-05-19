from __future__ import annotations

from berth.store import db
from berth.store import node_gpus as node_gpus_store
from berth.store import nodes as nodes_store


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.init_schema(conn)
    return conn


def _seed_node(conn) -> int:
    return nodes_store.insert(
        conn, label="local", fingerprint="local",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )


def test_upsert_and_list(tmp_path):
    conn = _fresh(tmp_path)
    nid = _seed_node(conn)
    node_gpus_store.upsert(
        conn, node_id=nid, gpu_index=0,
        name="H100", total_vram_mb=81920, driver_version="555.42",
    )
    node_gpus_store.upsert(
        conn, node_id=nid, gpu_index=1,
        name="H100", total_vram_mb=81920, driver_version="555.42",
    )
    gpus = node_gpus_store.list_for_node(conn, nid)
    assert [g.gpu_index for g in gpus] == [0, 1]
    assert gpus[0].name == "H100"


def test_upsert_updates_existing_row(tmp_path):
    conn = _fresh(tmp_path)
    nid = _seed_node(conn)
    node_gpus_store.upsert(
        conn, node_id=nid, gpu_index=0,
        name="old", total_vram_mb=1, driver_version=None,
    )
    node_gpus_store.upsert(
        conn, node_id=nid, gpu_index=0,
        name="new", total_vram_mb=2, driver_version="123",
    )
    gpus = node_gpus_store.list_for_node(conn, nid)
    assert len(gpus) == 1
    assert gpus[0].name == "new"
    assert gpus[0].total_vram_mb == 2
    assert gpus[0].driver_version == "123"


def test_delete_for_node(tmp_path):
    conn = _fresh(tmp_path)
    nid = _seed_node(conn)
    node_gpus_store.upsert(
        conn, node_id=nid, gpu_index=0,
        name="g", total_vram_mb=1, driver_version=None,
    )
    node_gpus_store.delete_for_node(conn, nid)
    assert node_gpus_store.list_for_node(conn, nid) == []
