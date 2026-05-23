"""End-to-end: a stub OpenAI server is adopted on a node; reconcile creates a
ready adopted row; the leader can resolve the model to that node+endpoint."""
from __future__ import annotations

from berth.cluster.leader_hub import reconcile_adopted
from berth.store import db
from berth.store import deployments as dep_store


def test_reconcile_makes_model_routable(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    reconcile_adopted(conn, node_id=3, endpoints=[{
        "model_name": "nvidia/MiniMax-M2.7-NVFP4",
        "served_model_name": "nvidia/MiniMax-M2.7-NVFP4",
        "address": "127.0.0.1", "port": 30011, "container_id": "cid-1",
        "gpu_ids": [7], "vram_reserved_mb": 268000, "alive": True,
    }])
    dep = dep_store.find_ready_by_model_name(conn, "nvidia/MiniMax-M2.7-NVFP4")
    assert dep is not None
    assert dep.node_id == 3
    assert dep.container_id == "cid-1"
    assert (dep.container_address, dep.container_port) == ("127.0.0.1", 30011)
