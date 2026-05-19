from serve_engine.store import adapters as ad_store
from serve_engine.store import db
from serve_engine.store import deployment_adapters as da_store
from serve_engine.store import deployments as dep_store
from serve_engine.store import models as model_store


def _setup(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="Qwen/Qwen3-7B")
    dep = dep_store.create(
        conn, model_id=base.id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
    )
    return conn, base, dep


def _make_adapter(conn, name: str) -> int:
    return ad_store.add(
        conn, name=name, base_model_name="qwen3-7b", hf_repo=f"o/{name}",
    ).id


def _set_attachment_time(conn, dep_id: int, adapter_id: int, ts: str) -> None:
    conn.execute(
        """
        UPDATE deployment_adapters
        SET loaded_at=?, last_used_at=?
        WHERE deployment_id=? AND adapter_id=?
        """,
        (ts, ts, dep_id, adapter_id),
    )


def test_attach_and_list(tmp_path):
    conn, _, dep = _setup(tmp_path)
    a1 = _make_adapter(conn, "tone-formal")
    a2 = _make_adapter(conn, "tone-casual")
    da_store.attach(conn, dep.id, a1)
    da_store.attach(conn, dep.id, a2)
    listed = da_store.list_for_deployment(conn, dep.id)
    assert {a.name for a in listed} == {"tone-formal", "tone-casual"}


def test_attach_idempotent_touches_loaded_at(tmp_path):
    conn, _, dep = _setup(tmp_path)
    a = _make_adapter(conn, "x")
    da_store.attach(conn, dep.id, a)
    first = conn.execute(
        "SELECT loaded_at FROM deployment_adapters WHERE deployment_id=? AND adapter_id=?",
        (dep.id, a),
    ).fetchone()["loaded_at"]
    _set_attachment_time(conn, dep.id, a, "2000-01-01 00:00:00")
    first = "2000-01-01 00:00:00"
    da_store.attach(conn, dep.id, a)
    second = conn.execute(
        "SELECT loaded_at FROM deployment_adapters WHERE deployment_id=? AND adapter_id=?",
        (dep.id, a),
    ).fetchone()["loaded_at"]
    assert second > first, f"re-attach should bump loaded_at: {first!r} -> {second!r}"


def test_detach_removes_row(tmp_path):
    conn, _, dep = _setup(tmp_path)
    a = _make_adapter(conn, "x")
    da_store.attach(conn, dep.id, a)
    da_store.detach(conn, dep.id, a)
    assert da_store.list_for_deployment(conn, dep.id) == []


def test_lru_for_deployment_orders_by_last_used(tmp_path):
    conn, _, dep = _setup(tmp_path)
    a1 = _make_adapter(conn, "first-loaded")
    a2 = _make_adapter(conn, "second-loaded")
    a3 = _make_adapter(conn, "third-loaded")
    da_store.attach(conn, dep.id, a1)
    da_store.attach(conn, dep.id, a2)
    da_store.attach(conn, dep.id, a3)
    _set_attachment_time(conn, dep.id, a1, "2000-01-01 00:00:00")
    _set_attachment_time(conn, dep.id, a2, "2000-01-01 00:00:01")
    _set_attachment_time(conn, dep.id, a3, "2000-01-01 00:00:02")
    da_store.touch(conn, dep.id, a1)
    lru = da_store.lru_for_deployment(conn, dep.id)
    assert lru is not None
    assert lru.name == "second-loaded"


def test_lru_for_deployment_empty_returns_none(tmp_path):
    conn, _, dep = _setup(tmp_path)
    assert da_store.lru_for_deployment(conn, dep.id) is None


def test_count_for_deployment(tmp_path):
    conn, _, dep = _setup(tmp_path)
    assert da_store.count_for_deployment(conn, dep.id) == 0
    a1 = _make_adapter(conn, "a")
    a2 = _make_adapter(conn, "b")
    da_store.attach(conn, dep.id, a1)
    da_store.attach(conn, dep.id, a2)
    assert da_store.count_for_deployment(conn, dep.id) == 2


def test_find_deployments_with_adapter(tmp_path):
    conn, base, dep = _setup(tmp_path)
    dep2 = dep_store.create(
        conn, model_id=base.id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
    )
    a = _make_adapter(conn, "x")
    da_store.attach(conn, dep.id, a)
    da_store.attach(conn, dep2.id, a)
    deps = da_store.find_deployments_with_adapter(conn, a)
    assert sorted(deps) == sorted([dep.id, dep2.id])


def test_cascade_on_deployment_delete(tmp_path):
    """Junction rows clean up automatically when a deployment is deleted."""
    conn, _, dep = _setup(tmp_path)
    a = _make_adapter(conn, "x")
    da_store.attach(conn, dep.id, a)
    conn.execute("PRAGMA foreign_keys=ON")  # already on per db.connect; assert
    conn.execute("DELETE FROM deployments WHERE id=?", (dep.id,))
    rows = conn.execute(
        "SELECT 1 FROM deployment_adapters WHERE deployment_id=?", (dep.id,),
    ).fetchall()
    assert rows == []


def test_detach_all_removes_every_row_for_deployment(tmp_path):
    conn, _, dep = _setup(tmp_path)
    a1 = _make_adapter(conn, "a")
    a2 = _make_adapter(conn, "b")
    da_store.attach(conn, dep.id, a1)
    da_store.attach(conn, dep.id, a2)
    da_store.detach_all(conn, dep.id)
    assert da_store.list_for_deployment(conn, dep.id) == []
