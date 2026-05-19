"""Proxy-layer enforcement of the optional per-key model allowlist
(migration 013).

The check sits in `_proxy` after the request body's `model` is parsed and
BEFORE any route lookup or deployment resolution. Auth tier is unrelated:
this is purely about which named models a key can dispatch to.

The fake-engine pattern mirrors test_proxy_adapter_dispatch.py - a tiny
FakeEngineClient yields a one-shot JSON response so we don't need a real
upstream container."""
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from berth.backends.vllm import VLLMBackend
from berth.daemon.app import build_app
from berth.lifecycle.docker_client import ContainerHandle
from berth.store import api_keys as ak_store
from berth.store import db
from berth.store import deployments as dep_store
from berth.store import models as model_store


@pytest.fixture
def app(tmp_path, monkeypatch):
    from berth.lifecycle.topology import GPUInfo, Topology

    monkeypatch.setattr(
        "berth.lifecycle.manager.wait_healthy",
        AsyncMock(return_value=True),
    )
    docker_client = MagicMock()
    docker_client.run.return_value = ContainerHandle(
        id="cid", name="x", address="127.0.0.1", port=49152,
    )
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    topology = Topology(
        gpus=[GPUInfo(index=0, name="H100", total_mb=80 * 1024)],
        _islands={0: frozenset({0})},
    )
    return build_app(
        conn=conn, docker_client=docker_client,
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
        topology=topology,
    )


def _seed_deployment(app, *, name: str = "qwen3-test"):
    """Register a ready deployment so the proxy resolves past the allowlist
    check on the happy path. The allowlist gate fires before resolution, so
    a 403 case never needs a real deployment - but the 200 case does."""
    conn = app.state.conn
    base = model_store.add(conn, name=name, hf_repo=f"o/{name}")
    dep = dep_store.create(
        conn, model_id=base.id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
        max_loras=0,
    )
    dep_store.set_container(
        conn, dep.id, container_id="cid", container_name="x",
        container_port=49152, container_address="127.0.0.1",
    )
    dep_store.update_status(conn, dep.id, "ready")
    return base, dep


def _patch_engine(monkeypatch):
    """Mock the upstream engine factory with a fake that returns a single
    chat-completion JSON object. Mirrors the pattern in
    test_proxy_adapter_dispatch.py:_make_engine_intercept."""

    class FakeResponse:
        def __init__(self):
            self.status_code = 200
            self.headers = {"content-type": "application/json"}

        async def aiter_raw(self):
            yield (
                b'{"id":"x","object":"chat.completion","choices":'
                b'[{"message":{"role":"assistant","content":"hi"}}],'
                b'"usage":{"prompt_tokens":1,"completion_tokens":1}}'
            )

    class FakeStreamCM:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *args):
            return None

    class FakeEngineClient:
        def __init__(self, base_url):
            self.base_url = base_url

        def stream(self, method, path, *, content=None, headers=None):
            return FakeStreamCM()

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "berth.daemon.openai_proxy.make_engine_client",
        lambda base_url: FakeEngineClient(base_url),
    )


@pytest.mark.asyncio
async def test_proxy_allows_model_when_key_has_no_allowlist(app, monkeypatch):
    """Unrestricted key (allowed_models is None) bypasses the check.

    This is the default state for every key created without the new field,
    and it's the regression check that the allowlist is *opt-in*.
    """
    _seed_deployment(app, name="qwen3-test")
    _patch_engine(monkeypatch)

    # Two keys so the auth bypass (no-keys-registered) doesn't activate.
    ak_store.create(app.state.conn, name="root", tier="admin")
    secret, _ = ak_store.create(
        app.state.conn, name="user", tier="standard", allowed_models=None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": "qwen3-test",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_proxy_allows_model_in_allowlist(app, monkeypatch):
    """Model name appears in the key's allowlist -> request proceeds."""
    _seed_deployment(app, name="qwen3-test")
    _patch_engine(monkeypatch)

    ak_store.create(app.state.conn, name="root", tier="admin")
    secret, _ = ak_store.create(
        app.state.conn, name="user", tier="standard",
        allowed_models=["qwen3-test", "other-model"],
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": "qwen3-test",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_proxy_blocks_model_not_in_allowlist(app, monkeypatch):
    """Requested model is not in the allowlist -> 403 with the key name
    and the requested model in the detail."""
    _seed_deployment(app, name="qwen3-test")
    _patch_engine(monkeypatch)

    ak_store.create(app.state.conn, name="root", tier="admin")
    secret, _ = ak_store.create(
        app.state.conn, name="restricted", tier="standard",
        allowed_models=["other-model-only"],
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": "qwen3-test",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'restricted'" in detail
    assert "'qwen3-test'" in detail


@pytest.mark.asyncio
async def test_proxy_empty_allowlist_denies_all(app, monkeypatch):
    """allowed_models = [] is "deny-all", not "allow-all" (security default).

    Anyone caught by surprise here should be: the field exists explicitly
    as a list, so an empty list is an explicit "no models". The footgun
    interpretation ([] = allow everything) is not implemented.
    """
    _seed_deployment(app, name="qwen3-test")
    _patch_engine(monkeypatch)

    ak_store.create(app.state.conn, name="root", tier="admin")
    secret, _ = ak_store.create(
        app.state.conn, name="denyall", tier="standard", allowed_models=[],
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": "qwen3-test",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_proxy_bypasses_check_when_no_keys_registered(app, monkeypatch):
    """No keys exist at all -> the auth dep returns None and we hit the
    proxy with key=None. The allowlist check must not fire (there is no
    key to read allowed_models off of)."""
    _seed_deployment(app, name="qwen3-test")
    _patch_engine(monkeypatch)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={
                "model": "qwen3-test",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert r.status_code == 200, r.text
