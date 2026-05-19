import pytest

from berth.store import db
from berth.store import service_profiles as profile_store


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.init_schema(conn)
    return conn


def test_create_and_get_service_profile(tmp_path):
    conn = _fresh(tmp_path)
    profile = profile_store.create(
        conn,
        name="qwen-chat-small",
        model_name="qwen-0_5b",
        hf_repo="Qwen/Qwen2.5-0.5B-Instruct",
        revision="main",
        backend="vllm",
        image_tag="vllm/vllm-openai:v0.7.3",
        gpu_ids=[0],
        tensor_parallel=1,
        max_model_len=8192,
        dtype="auto",
        pinned=True,
        idle_timeout_s=60,
        target_concurrency=32,
        extra_args={"--reasoning-parser": "qwen3"},
    )

    fetched = profile_store.get_by_name(conn, "qwen-chat-small")
    assert fetched is not None
    assert fetched.id == profile.id
    assert fetched.gpu_ids == [0]
    assert fetched.pinned is True
    assert fetched.idle_timeout_s == 60
    assert fetched.target_concurrency == 32
    assert fetched.extra_args == {"--reasoning-parser": "qwen3"}


def test_list_service_profiles_returns_creation_order(tmp_path):
    conn = _fresh(tmp_path)
    for name in ("a", "b"):
        profile_store.create(
            conn,
            name=name,
            model_name=name,
            hf_repo=f"org/{name}",
            revision="main",
            backend="vllm",
            image_tag="img:v1",
            gpu_ids=[0],
            tensor_parallel=1,
            max_model_len=4096,
            dtype="auto",
        )

    assert [p.name for p in profile_store.list_all(conn)] == ["a", "b"]


def test_duplicate_service_profile_raises(tmp_path):
    conn = _fresh(tmp_path)
    kwargs = dict(
        name="dup",
        model_name="qwen",
        hf_repo="org/qwen",
        revision="main",
        backend="vllm",
        image_tag="img:v1",
        gpu_ids=[0],
        tensor_parallel=1,
        max_model_len=4096,
        dtype="auto",
    )
    profile_store.create(conn, **kwargs)
    with pytest.raises(profile_store.AlreadyExists):
        profile_store.create(conn, **kwargs)


def test_delete_service_profile(tmp_path):
    conn = _fresh(tmp_path)
    profile = profile_store.create(
        conn,
        name="gone",
        model_name="qwen",
        hf_repo="org/qwen",
        revision="main",
        backend="vllm",
        image_tag="img:v1",
        gpu_ids=[0],
        tensor_parallel=1,
        max_model_len=4096,
        dtype="auto",
    )

    profile_store.delete(conn, profile.id)
    assert profile_store.get_by_name(conn, "gone") is None
