from berth.cluster.leader_hub import reconcile_adopted
from berth.store import db
from berth.store import deployments as dep_store
from berth.store import models as model_store


def _conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def _ep(**over):
    base = dict(model_name="nvidia/MiniMax-M2.7-NVFP4",
               served_model_name="nvidia/MiniMax-M2.7-NVFP4",
               address="127.0.0.1", port=30011, container_id="cid-1",
               gpu_ids=[7], vram_reserved_mb=268000, alive=True)
    base.update(over)
    return base


def test_reconcile_creates_then_prunes(tmp_path):
    conn = _conn(tmp_path)
    reconcile_adopted(conn, node_id=3, endpoints=[_ep()])
    rows = dep_store.list_adopted_for_node(conn, 3)
    assert len(rows) == 1 and rows[0].status == "ready"
    assert model_store.get_by_name(conn, "nvidia/MiniMax-M2.7-NVFP4") is not None

    reconcile_adopted(conn, node_id=3, endpoints=[])
    assert dep_store.list_adopted_for_node(conn, 3) == []


def test_reconcile_marks_down_when_not_alive(tmp_path):
    conn = _conn(tmp_path)
    reconcile_adopted(conn, node_id=3, endpoints=[_ep(alive=False)])
    rows = dep_store.list_adopted_for_node(conn, 3)
    assert rows[0].status == "failed"


def test_reconcile_skips_gpu_conflict_with_managed(tmp_path):
    conn = _conn(tmp_path)
    m = model_store.add(conn, name="managed-x", hf_repo="org/x")
    managed = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[7], tensor_parallel=1, max_model_len=4096, dtype="auto")
    dep_store.update_status(conn, managed.id, "ready")
    reconcile_adopted(conn, node_id=3, endpoints=[_ep()])  # also wants gpu 7
    assert dep_store.list_adopted_for_node(conn, 3) == []
