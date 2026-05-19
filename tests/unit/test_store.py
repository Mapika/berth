import pytest

from serve_engine.store import db
from serve_engine.store import deployments as dep_store
from serve_engine.store import models as model_store


def test_init_schema_creates_tables(tmp_path):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    db.init_schema(conn)

    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = {r[0] for r in rows}
    assert "models" in table_names
    assert "deployments" in table_names
    assert "service_profiles" in table_names
    assert "service_routes" in table_names
    assert "_migrations" in table_names


def test_init_schema_is_idempotent(tmp_path):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    db.init_schema(conn)
    db.init_schema(conn)  # second call must not error

    applied = conn.execute(
        "SELECT COUNT(*) FROM _migrations WHERE filename='001_initial.sql'"
    ).fetchone()[0]
    assert applied == 1


def _fresh(tmp_path):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    db.init_schema(conn)
    return conn


def test_add_and_get_model(tmp_path):
    conn = _fresh(tmp_path)
    m = model_store.add(conn, name="llama-1b", hf_repo="meta-llama/Llama-3.2-1B-Instruct")
    assert m.id is not None
    assert m.name == "llama-1b"
    assert m.revision == "main"

    fetched = model_store.get_by_name(conn, "llama-1b")
    assert fetched is not None
    assert fetched.id == m.id


def test_add_duplicate_model_raises(tmp_path):
    conn = _fresh(tmp_path)
    model_store.add(conn, name="x", hf_repo="org/x")
    with pytest.raises(model_store.AlreadyExists):
        model_store.add(conn, name="x", hf_repo="org/x")


def test_list_models_empty(tmp_path):
    conn = _fresh(tmp_path)
    assert model_store.list_all(conn) == []


def test_list_models_returns_in_creation_order(tmp_path):
    conn = _fresh(tmp_path)
    model_store.add(conn, name="a", hf_repo="org/a")
    model_store.add(conn, name="b", hf_repo="org/b")
    rows = model_store.list_all(conn)
    assert [m.name for m in rows] == ["a", "b"]


def test_set_local_path(tmp_path):
    conn = _fresh(tmp_path)
    m = model_store.add(conn, name="x", hf_repo="org/x")
    model_store.set_local_path(conn, m.id, "/var/x")
    fetched = model_store.get_by_name(conn, "x")
    assert fetched.local_path == "/var/x"


def test_create_deployment(tmp_path):
    conn = _fresh(tmp_path)
    m = model_store.add(conn, name="x", hf_repo="org/x")
    d = dep_store.create(
        conn,
        model_id=m.id,
        backend="vllm",
        image_tag="vllm/vllm-openai:v0.7.3",
        gpu_ids=[0],
        tensor_parallel=1,
        max_model_len=8192,
        dtype="bf16",
    )
    assert d.id is not None
    assert d.status == "pending"
    assert d.gpu_ids == [0]


def test_update_deployment_status(tmp_path):
    conn = _fresh(tmp_path)
    m = model_store.add(conn, name="x", hf_repo="org/x")
    d = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=8192, dtype="auto",
    )
    dep_store.update_status(conn, d.id, "loading")
    refreshed = dep_store.get_by_id(conn, d.id)
    assert refreshed.status == "loading"


def test_set_container_info(tmp_path):
    conn = _fresh(tmp_path)
    m = model_store.add(conn, name="x", hf_repo="org/x")
    d = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=8192, dtype="auto",
    )
    dep_store.set_container(
        conn, d.id, container_id="abc", container_name="vllm-x",
        container_port=8000, container_address="127.0.0.1",
    )
    refreshed = dep_store.get_by_id(conn, d.id)
    assert refreshed.container_id == "abc"
    assert refreshed.container_port == 8000
    assert refreshed.container_address == "127.0.0.1"


def test_find_active(tmp_path):
    conn = _fresh(tmp_path)
    m = model_store.add(conn, name="x", hf_repo="org/x")
    assert dep_store.find_active(conn) is None
    d = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=8192, dtype="auto",
    )
    dep_store.update_status(conn, d.id, "ready")
    found = dep_store.find_active(conn)
    assert found is not None and found.id == d.id


def test_find_ready_by_model_name(tmp_path):
    conn = _fresh(tmp_path)
    m = model_store.add(conn, name="qwen", hf_repo="org/qwen")
    assert dep_store.find_ready_by_model_name(conn, "qwen") is None
    d = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=8192, dtype="auto",
    )
    dep_store.update_status(conn, d.id, "ready")
    dep_store.set_container(
        conn, d.id, container_id="cid", container_name="x",
        container_port=49152, container_address="127.0.0.1",
    )
    found = dep_store.find_ready_by_model_name(conn, "qwen")
    assert found is not None
    assert found.id == d.id
    assert found.container_address == "127.0.0.1"


def test_list_evictable_sorts_lru(tmp_path):
    conn = _fresh(tmp_path)
    m1 = model_store.add(conn, name="a", hf_repo="org/a")
    m2 = model_store.add(conn, name="b", hf_repo="org/b")
    m3 = model_store.add(conn, name="c", hf_repo="org/c")

    def _make(model_id: int, pinned: bool = False) -> int:
        d = dep_store.create(
            conn, model_id=model_id, backend="vllm", image_tag="img:v1",
            gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
            pinned=pinned,
        )
        dep_store.update_status(conn, d.id, "ready")
        return d.id

    da = _make(m1.id, pinned=True)
    db = _make(m2.id)
    dc = _make(m3.id)

    conn.execute(
        "UPDATE deployments SET last_request_at=? WHERE id=?",
        ("2000-01-01 00:00:00", dc),
    )
    conn.execute(
        "UPDATE deployments SET last_request_at=? WHERE id=?",
        ("2000-01-01 00:00:01", db),
    )

    rows = dep_store.list_evictable(conn)
    assert [r.id for r in rows] == [dc, db]
    ids = [r.id for r in rows]
    assert da not in ids


def test_set_pinned(tmp_path):
    conn = _fresh(tmp_path)
    m = model_store.add(conn, name="x", hf_repo="org/x")
    d = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=8192, dtype="auto",
    )
    assert dep_store.get_by_id(conn, d.id).pinned is False
    dep_store.set_pinned(conn, d.id, True)
    assert dep_store.get_by_id(conn, d.id).pinned is True
