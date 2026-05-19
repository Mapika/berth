from __future__ import annotations

from berth.cluster.agent_registry import AgentRegistry
from berth.cluster.health_watcher import sweep
from berth.store import db
from berth.store import nodes as nodes_store


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.init_schema(conn)
    return conn


def test_sweep_marks_stale_node_unreachable(tmp_path):
    conn = _fresh(tmp_path)
    now = 1000.0
    nid = nodes_store.insert(
        conn, label="agent-a", fingerprint="fp",
        reachable_as=None, first_seen=now, last_seen=now,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    nodes_store.set_status(conn, nid, status="ready", last_seen=now)
    reg = AgentRegistry()
    sweep(conn, reg, now=now + 30, stale_after_s=15)
    n = nodes_store.get(conn, nid)
    assert n.status == "unreachable"


def test_sweep_does_not_touch_fresh_node(tmp_path):
    conn = _fresh(tmp_path)
    now = 1000.0
    nid = nodes_store.insert(
        conn, label="agent-a", fingerprint="fp",
        reachable_as=None, first_seen=now, last_seen=now,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    nodes_store.set_status(conn, nid, status="ready", last_seen=now + 5)
    reg = AgentRegistry()
    sweep(conn, reg, now=now + 10, stale_after_s=15)
    n = nodes_store.get(conn, nid)
    assert n.status == "ready"


def test_sweep_skips_local_node(tmp_path):
    conn = _fresh(tmp_path)
    now = 1000.0
    nid = nodes_store.insert(
        conn, label="local", fingerprint="local",
        reachable_as=None, first_seen=now, last_seen=now - 1000,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    nodes_store.set_status(conn, nid, status="ready", last_seen=now - 1000)
    reg = AgentRegistry()
    sweep(conn, reg, now=now, stale_after_s=15)
    n = nodes_store.get(conn, nid)
    assert n.status == "ready"


def test_sweep_unregisters_stale_node_from_registry(tmp_path):
    conn = _fresh(tmp_path)
    now = 1000.0
    nid = nodes_store.insert(
        conn, label="agent-a", fingerprint="fp",
        reachable_as=None, first_seen=now, last_seen=now,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    nodes_store.set_status(conn, nid, status="ready", last_seen=now)

    class _Stub:
        def __init__(self, n): self._n = n
        @property
        def node_id(self): return self._n
        @property
        def is_ready(self): return True

    reg = AgentRegistry()
    reg.register(_Stub(nid))
    sweep(conn, reg, now=now + 30, stale_after_s=15)
    assert reg.get(nid) is None


def test_sweep_does_not_touch_unreachable_nodes(tmp_path):
    """Already-unreachable nodes don't need to be touched again."""
    conn = _fresh(tmp_path)
    now = 1000.0
    nid = nodes_store.insert(
        conn, label="agent-a", fingerprint="fp",
        reachable_as=None, first_seen=now, last_seen=now,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    # status defaults to 'unreachable' on insert
    reg = AgentRegistry()
    sweep(conn, reg, now=now + 1000, stale_after_s=15)
    n = nodes_store.get(conn, nid)
    assert n.status == "unreachable"
