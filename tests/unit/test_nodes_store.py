from __future__ import annotations

from berth.store import db
from berth.store import nodes as nodes_store


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.init_schema(conn)
    return conn


def test_insert_get_list_node(tmp_path):
    conn = _fresh(tmp_path)
    node_id = nodes_store.insert(
        conn,
        label="agent-a",
        fingerprint="sha256:aaaa",
        reachable_as=None,
        first_seen=1000.0,
        last_seen=1000.0,
        agent_version="0.0.1",
        cpu_count=8,
        total_ram_mb=32000,
        gpu_count=1,
        total_vram_mb=80000,
    )
    n = nodes_store.get(conn, node_id)
    assert n is not None
    assert n.label == "agent-a"
    assert n.fingerprint == "sha256:aaaa"
    assert n.status == "unreachable"
    rows = nodes_store.list_all(conn)
    assert [r.id for r in rows] == [node_id]


def test_set_status_and_last_seen(tmp_path):
    conn = _fresh(tmp_path)
    nid = nodes_store.insert(
        conn, label="x", fingerprint="fp",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    nodes_store.set_status(conn, nid, status="ready", last_seen=12.0)
    n = nodes_store.get(conn, nid)
    assert n.status == "ready"
    assert n.last_seen == 12.0


def test_update_inventory(tmp_path):
    conn = _fresh(tmp_path)
    nid = nodes_store.insert(
        conn, label="x", fingerprint="fp",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    nodes_store.update_inventory(
        conn, nid,
        agent_version="0.0.1", cpu_count=16, total_ram_mb=64000,
        gpu_count=2, total_vram_mb=160000,
    )
    n = nodes_store.get(conn, nid)
    assert n.agent_version == "0.0.1"
    assert n.cpu_count == 16
    assert n.total_vram_mb == 160000


def test_delete_node(tmp_path):
    conn = _fresh(tmp_path)
    nid = nodes_store.insert(
        conn, label="x", fingerprint="fp",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    nodes_store.delete(conn, nid)
    assert nodes_store.get(conn, nid) is None


def test_find_by_fingerprint(tmp_path):
    conn = _fresh(tmp_path)
    nid = nodes_store.insert(
        conn, label="x", fingerprint="fp-xyz",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    n = nodes_store.find_by_fingerprint(conn, "fp-xyz")
    assert n is not None and n.id == nid
    assert nodes_store.find_by_fingerprint(conn, "nope") is None


def test_find_by_label(tmp_path):
    conn = _fresh(tmp_path)
    nid = nodes_store.insert(
        conn, label="local", fingerprint="local",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    n = nodes_store.find_by_label(conn, "local")
    assert n is not None and n.id == nid
    assert nodes_store.find_by_label(conn, "absent") is None
