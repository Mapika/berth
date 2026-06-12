from berth.cluster.leader_hub import reconcile_adopted
from berth.store import db, node_gpus
from berth.store import deployments as dep_store
from berth.store import models as model_store
from berth.store import nodes as nodes_store


def _conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def _seed_node(conn) -> int:
    return nodes_store.insert(
        conn, label="agent-node", fingerprint="sha256:test",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )


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


def test_reconcile_registers_model_under_served_name(tmp_path):
    conn = _conn(tmp_path)
    reconcile_adopted(conn, node_id=3, endpoints=[_ep(
        model_name="operator-label", served_model_name="real/served-name")])
    # Routing/registry uses the SERVED name, not the operator's label.
    assert model_store.get_by_name(conn, "real/served-name") is not None
    assert model_store.get_by_name(conn, "operator-label") is None
    dep = dep_store.find_ready_by_model_name(conn, "real/served-name")
    assert dep is not None and dep.container_id == "cid-1"


def test_reconcile_defaults_vram_to_full_gpu_size(tmp_path):
    """When an adopted endpoint reports vram_reserved_mb=0, the leader fills it
    in from the node's known per-GPU VRAM so placement treats that GPU as
    fully occupied."""
    conn = _conn(tmp_path)
    node_id = _seed_node(conn)
    node_gpus.upsert(
        conn, node_id=node_id, gpu_index=7, name="NVIDIA H100",
        total_vram_mb=275040, driver_version="550.54.15",
    )
    reconcile_adopted(conn, node_id=node_id, endpoints=[_ep(gpu_ids=[7], vram_reserved_mb=0)])
    rows = dep_store.list_adopted_for_node(conn, node_id)
    assert len(rows) == 1
    assert rows[0].vram_reserved_mb == 275040


def test_reconcile_respects_explicit_vram(tmp_path):
    """When an adopted endpoint reports a non-zero vram_reserved_mb, the leader
    keeps it as-is (operator override wins)."""
    conn = _conn(tmp_path)
    node_id = _seed_node(conn)
    node_gpus.upsert(
        conn, node_id=node_id, gpu_index=7, name="NVIDIA H100",
        total_vram_mb=275040, driver_version="550.54.15",
    )
    reconcile_adopted(conn, node_id=node_id, endpoints=[_ep(gpu_ids=[7], vram_reserved_mb=1000)])
    rows = dep_store.list_adopted_for_node(conn, node_id)
    assert len(rows) == 1
    assert rows[0].vram_reserved_mb == 1000


def test_reconcile_defaults_vram_sum_for_multi_gpu(tmp_path):
    """For a multi-GPU endpoint with vram_reserved_mb=0, the leader sums the
    total_vram_mb of all the endpoint's gpu_ids."""
    conn = _conn(tmp_path)
    node_id = _seed_node(conn)
    node_gpus.upsert(
        conn, node_id=node_id, gpu_index=6, name="NVIDIA H100",
        total_vram_mb=275040, driver_version="550.54.15",
    )
    node_gpus.upsert(
        conn, node_id=node_id, gpu_index=7, name="NVIDIA H100",
        total_vram_mb=275040, driver_version="550.54.15",
    )
    reconcile_adopted(
        conn, node_id=node_id,
        endpoints=[_ep(gpu_ids=[6, 7], vram_reserved_mb=0)],
    )
    rows = dep_store.list_adopted_for_node(conn, node_id)
    assert len(rows) == 1
    assert rows[0].vram_reserved_mb == 550080


def test_reconcile_prunes_deployment_with_usage_history(tmp_path):
    """An adopted deployment that served requests has usage_events rows whose
    deployment_id references it (migration 006, no ON DELETE action). The
    prune must detach that history and still delete the row — this exact case
    silently leaked rows in production (FK error swallowed by the report-level
    exception handler)."""
    from berth.store import usage_events

    conn = _conn(tmp_path)
    reconcile_adopted(conn, node_id=3, endpoints=[_ep()])
    dep = dep_store.list_adopted_for_node(conn, 3)[0]
    usage_events.record(
        conn, model_name="nvidia/MiniMax-M2.7-NVFP4",
        base_name="nvidia/MiniMax-M2.7-NVFP4", deployment_id=dep.id,
        tokens_in=10, tokens_out=5)

    reconcile_adopted(conn, node_id=3, endpoints=[])
    assert dep_store.list_adopted_for_node(conn, 3) == []
    # History survives, detached.
    row = conn.execute(
        "SELECT deployment_id FROM usage_events").fetchone()
    assert row is not None and row["deployment_id"] is None


def test_reconcile_prune_continues_past_undeletable_row(tmp_path, monkeypatch):
    """One row that fails to delete must not shield the stale rows behind it."""
    conn = _conn(tmp_path)
    reconcile_adopted(conn, node_id=3, endpoints=[
        _ep(), _ep(container_id="cid-2", port=30012, gpu_ids=[6])])

    import sqlite3 as _sqlite3
    real_delete = dep_store.delete_adopted

    def flaky_delete(c, dep_id):
        first = dep_store.list_adopted_for_node(c, 3)[0]
        if dep_id == first.id:
            raise _sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        real_delete(c, dep_id)

    monkeypatch.setattr(
        "berth.cluster.leader_hub.dep_store.delete_adopted", flaky_delete)
    reconcile_adopted(conn, node_id=3, endpoints=[])
    survivors = dep_store.list_adopted_for_node(conn, 3)
    assert [d.container_id for d in survivors] == ["cid-1"]
