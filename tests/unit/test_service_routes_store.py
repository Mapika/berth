import pytest

from berth.store import db
from berth.store import service_profiles as profile_store
from berth.store import service_routes as route_store


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.init_schema(conn)
    return conn


def _profile(conn, name: str, model_name: str):
    return profile_store.create(
        conn,
        name=name,
        model_name=model_name,
        hf_repo=f"org/{model_name}",
        revision="main",
        backend="vllm",
        image_tag="img:v1",
        gpu_ids=[0],
        tensor_parallel=1,
        max_model_len=4096,
        dtype="auto",
    )


def test_create_and_find_enabled_route(tmp_path):
    conn = _fresh(tmp_path)
    _profile(conn, "qwen-service", "qwen")

    route = route_store.create(
        conn,
        name="public-chat",
        match_model="chat",
        profile_name="qwen-service",
        priority=20,
    )

    found = route_store.find_enabled_for_model(conn, "chat")
    assert found is not None
    assert found.id == route.id
    assert found.profile_name == "qwen-service"
    assert found.target_model_name == "qwen"
    assert found.enabled is True
    assert found.priority == 20


def test_route_priority_picks_lowest_number(tmp_path):
    conn = _fresh(tmp_path)
    _profile(conn, "slow", "slow-model")
    _profile(conn, "fast", "fast-model")
    route_store.create(
        conn, name="slow-route", match_model="chat", profile_name="slow", priority=100,
    )
    route_store.create(
        conn, name="fast-route", match_model="chat", profile_name="fast", priority=10,
    )

    found = route_store.find_enabled_for_model(conn, "chat")
    assert found is not None
    assert found.name == "fast-route"
    assert found.target_model_name == "fast-model"


def test_disabled_route_is_ignored(tmp_path):
    conn = _fresh(tmp_path)
    _profile(conn, "qwen-service", "qwen")
    route_store.create(
        conn,
        name="off",
        match_model="chat",
        profile_name="qwen-service",
        enabled=False,
    )

    assert route_store.find_enabled_for_model(conn, "chat") is None


def test_route_with_fallback_profile(tmp_path):
    conn = _fresh(tmp_path)
    _profile(conn, "primary", "qwen-large")
    _profile(conn, "fallback", "qwen-small")

    route = route_store.create(
        conn,
        name="chat",
        match_model="chat",
        profile_name="primary",
        fallback_profile_name="fallback",
    )

    assert route.target_model_name == "qwen-large"
    assert route.fallback_profile_name == "fallback"
    assert route.fallback_model_name == "qwen-small"


def test_unknown_profile_raises(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(route_store.UnknownProfile):
        route_store.create(
            conn,
            name="broken",
            match_model="chat",
            profile_name="missing",
        )


def test_duplicate_route_raises(tmp_path):
    conn = _fresh(tmp_path)
    _profile(conn, "qwen-service", "qwen")
    route_store.create(
        conn, name="dup", match_model="chat", profile_name="qwen-service",
    )
    with pytest.raises(route_store.AlreadyExists):
        route_store.create(
            conn, name="dup", match_model="other", profile_name="qwen-service",
        )
