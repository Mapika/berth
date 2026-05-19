import pytest

from berth.store import adapters as ad_store
from berth.store import db
from berth.store import models as model_store


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def test_add_happy_path(tmp_path):
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="Qwen/Qwen3-7B")
    a = ad_store.add(
        conn, name="tone-formal", base_model_name="qwen3-7b",
        hf_repo="org/qwen3-7b-tone-formal-lora",
    )
    assert a.id > 0
    assert a.name == "tone-formal"
    assert a.base_model.id == base.id
    assert a.local_path is None
    assert a.size_mb is None
    assert a.source_peer_id is None  # locally registered


def test_add_rejects_collision_with_base_model_name(tmp_path):
    conn = _fresh(tmp_path)
    model_store.add(conn, name="qwen3-7b", hf_repo="Qwen/Qwen3-7B")
    model_store.add(conn, name="my-model", hf_repo="o/x")
    with pytest.raises(ad_store.NameCollision, match="base model"):
        ad_store.add(
            conn, name="my-model", base_model_name="qwen3-7b",
            hf_repo="o/lora",
        )


def test_add_rejects_collision_with_existing_adapter(tmp_path):
    conn = _fresh(tmp_path)
    model_store.add(conn, name="qwen3-7b", hf_repo="Qwen/Qwen3-7B")
    ad_store.add(
        conn, name="tone-formal", base_model_name="qwen3-7b", hf_repo="o/lora1",
    )
    with pytest.raises(ad_store.NameCollision, match="adapter"):
        ad_store.add(
            conn, name="tone-formal", base_model_name="qwen3-7b", hf_repo="o/lora2",
        )


def test_add_rejects_missing_base(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(ad_store.BaseNotFound):
        ad_store.add(
            conn, name="x", base_model_name="does-not-exist", hf_repo="o/lora",
        )


def test_get_by_name_returns_resolved_base(tmp_path):
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="Qwen/Qwen3-7B")
    ad_store.add(
        conn, name="tone-formal", base_model_name="qwen3-7b", hf_repo="o/lora",
    )
    a = ad_store.get_by_name(conn, "tone-formal")
    assert a is not None
    assert a.base_model.id == base.id
    assert a.base_model.name == "qwen3-7b"


def test_get_by_name_missing_returns_none(tmp_path):
    conn = _fresh(tmp_path)
    assert ad_store.get_by_name(conn, "nope") is None


def test_list_for_base_filters_by_base(tmp_path):
    conn = _fresh(tmp_path)
    base1 = model_store.add(conn, name="qwen3-7b", hf_repo="Qwen/Qwen3-7B")
    base2 = model_store.add(conn, name="llama-8b", hf_repo="meta/L8B")
    ad_store.add(conn, name="qwen-formal", base_model_name="qwen3-7b", hf_repo="o/a")
    ad_store.add(conn, name="qwen-casual", base_model_name="qwen3-7b", hf_repo="o/b")
    ad_store.add(conn, name="llama-formal", base_model_name="llama-8b", hf_repo="o/c")
    qwens = ad_store.list_for_base(conn, base1.id)
    assert {a.name for a in qwens} == {"qwen-formal", "qwen-casual"}
    llamas = ad_store.list_for_base(conn, base2.id)
    assert {a.name for a in llamas} == {"llama-formal"}


def test_set_local_path_and_size(tmp_path):
    conn = _fresh(tmp_path)
    model_store.add(conn, name="qwen3-7b", hf_repo="Qwen/Qwen3-7B")
    a = ad_store.add(
        conn, name="x", base_model_name="qwen3-7b", hf_repo="o/lora",
    )
    ad_store.set_local_path(conn, a.id, "/cache/models--o--lora/snapshots/abc")
    ad_store.set_size_mb(conn, a.id, 87)
    refreshed = ad_store.get_by_id(conn, a.id)
    assert refreshed.local_path == "/cache/models--o--lora/snapshots/abc"
    assert refreshed.size_mb == 87


def test_delete(tmp_path):
    conn = _fresh(tmp_path)
    model_store.add(conn, name="qwen3-7b", hf_repo="Qwen/Qwen3-7B")
    a = ad_store.add(
        conn, name="x", base_model_name="qwen3-7b", hf_repo="o/lora",
    )
    ad_store.delete(conn, a.id)
    assert ad_store.get_by_name(conn, "x") is None


def test_base_model_added_after_adapter_with_same_name_is_rejected(tmp_path):
    """Disjoint-namespace symmetry: model_store.add must refuse a name
    that already exists as an adapter, and vice versa. Routing is
    `model='x'` -> look up; collisions would make this ambiguous."""
    conn = _fresh(tmp_path)
    model_store.add(conn, name="qwen3-7b", hf_repo="Qwen/Qwen3-7B")
    ad_store.add(
        conn, name="my-thing", base_model_name="qwen3-7b", hf_repo="o/lora",
    )
    with pytest.raises(model_store.AlreadyExists, match="adapter"):
        model_store.add(conn, name="my-thing", hf_repo="o/x")
