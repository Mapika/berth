"""End-to-end-ish proxy tests: client POSTs `model=<adapter>` to
/v1/chat/completions; verify the dispatched upstream request has the
right `model` field, hits the right deployment, and that the engine's
hot-load endpoint is called when the adapter wasn't preloaded."""
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from berth.backends.vllm import VLLMBackend
from berth.daemon.app import build_app
from berth.daemon.openai_proxy import _UsageTracker
from berth.lifecycle.docker_client import ContainerHandle
from berth.store import adapters as ad_store
from berth.store import db
from berth.store import deployment_adapters as da_store
from berth.store import deployments as dep_store
from berth.store import models as model_store
from berth.store import service_profiles as profile_store
from berth.store import service_routes as route_store
from berth.store import usage_events as ue_store


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


def _seed(app, *, max_loras: int = 4):
    conn = app.state.conn
    base = model_store.add(conn, name="qwen3-test", hf_repo="o/qwen3")
    dep = dep_store.create(
        conn, model_id=base.id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
        max_loras=max_loras,
    )
    dep_store.set_container(
        conn, dep.id, container_id="cid", container_name="x",
        container_port=49152, container_address="127.0.0.1",
    )
    dep_store.update_status(conn, dep.id, "ready")
    return base, dep


def test_usage_tracker_ignores_malformed_json_usage_numbers():
    tracker = _UsageTracker(is_sse=False)
    tracker.feed(
        b'{"id":"x","usage":{"prompt_tokens":"many","completion_tokens":{}}}',
    )

    assert tracker.extract() == (0, 0)


def test_usage_tracker_ignores_malformed_sse_usage_numbers():
    tracker = _UsageTracker(is_sse=True)
    tracker.feed(
        b'data: {"id":"x","choices":[],"usage":'
        b'{"prompt_tokens":"many","completion_tokens":{}}}\n\n'
    )

    assert tracker.extract() == (0, 0)


def _make_engine_intercept(monkeypatch, app):
    """Mock the engine HTTP layer so we can capture upstream calls without
    pinging real ports. Captures: load_lora_adapter calls, chat-completion
    POSTs (returning a fake JSON response). All other httpx calls
    (ASGITransport to our own app) pass through unmocked."""
    captured = {"loads": [], "chats": []}

    # Patch the engine-stream factory so /v1/chat/completions returns
    # a controlled body without needing a real engine. Need a duck-typed
    # response that supports aiter_raw() since the proxy iterates over it
    # - full httpx.Response with `content=` consumes the stream eagerly.
    class FakeResponse:
        def __init__(self, body: bytes, content_type: str = "application/json"):
            self.status_code = 200
            self.headers = {"content-type": content_type}
            self._body = body

        async def aiter_raw(self):
            yield self._body

    class FakeStreamCM:
        async def __aenter__(self):
            return FakeResponse(
                b'{"id":"x","object":"chat.completion","choices":'
                b'[{"message":{"role":"assistant","content":"hi"}}],'
                b'"usage":{"prompt_tokens":3,"completion_tokens":1}}',
            )

        async def __aexit__(self, *args):
            return None

    class FakeEngineClient:
        def __init__(self, base_url):
            self.base_url = base_url

        def stream(self, method, path, *, content=None, headers=None):
            # Capture the body sent to the engine
            try:
                import json as _json
                captured["chats"].append({
                    "url": str(self.base_url) + path,
                    "body": _json.loads(content) if content else None,
                })
            except Exception:
                pass
            return FakeStreamCM()

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "berth.daemon.openai_proxy.make_engine_client",
        lambda base_url: FakeEngineClient(base_url),
    )

    # Intercept dynamic LoRA load HTTP calls
    original_post = httpx.AsyncClient.post

    async def fake_post(self, url, *, json=None, **kw):
        if "49152" in str(url):
            captured["loads"].append({"url": str(url), "json": json})
            return httpx.Response(200, json={"message": "ok"})
        return await original_post(self, url, json=json, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return captured


def _seed_with_rank(app, *, deployment_max_lora_rank: int):
    """Like _seed() but sets the deployment's max_lora_rank - what the
    operator would have passed via -x '--max-lora-rank=N'."""
    conn = app.state.conn
    base = model_store.add(conn, name="qwen3-test", hf_repo="o/qwen3")
    dep = dep_store.create(
        conn, model_id=base.id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
        max_loras=4, max_lora_rank=deployment_max_lora_rank,
    )
    dep_store.set_container(
        conn, dep.id, container_id="cid", container_name="x",
        container_port=49152, container_address="127.0.0.1",
    )
    dep_store.update_status(conn, dep.id, "ready")
    return base, dep


@pytest.mark.asyncio
async def test_proxy_populates_usage_events_tokens_in_streaming_mode(app, monkeypatch):
    """In SSE streaming mode, OpenAI/vLLM/SGLang emit the usage chunk
    BEFORE the terminal `data: [DONE]` frame. The tracker must keep the
    usage-bearing event, not the [DONE] sentinel - otherwise the
    predictor sees 0/0 for every streaming request."""
    _seed(app, max_loras=0)
    captured = {"chats": []}

    class FakeStreamResp:
        def __init__(self):
            self.status_code = 200
            self.headers = {"content-type": "text/event-stream"}

        async def aiter_raw(self):
            yield (
                b'data: {"id":"x","choices":[{"delta":{"content":"hi"}}]}\n\n'
            )
            yield (
                b'data: {"id":"x","choices":[],"usage":'
                b'{"prompt_tokens":7,"completion_tokens":3}}\n\n'
            )
            yield b'data: [DONE]\n\n'

    class FakeStreamCM:
        async def __aenter__(self):
            return FakeStreamResp()

        async def __aexit__(self, *args):
            return None

    class FakeEngineClient:
        def __init__(self, base_url):
            self.base_url = base_url

        def stream(self, method, path, *, content=None, headers=None):
            captured["chats"].append({"path": path})
            return FakeStreamCM()

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "berth.daemon.openai_proxy.make_engine_client",
        lambda base_url: FakeEngineClient(base_url),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with c.stream(
            "POST", "/v1/chat/completions",
            json={
                "model": "qwen3-test",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        ) as r:
            async for _ in r.aiter_raw():
                pass
            assert r.status_code == 200

    events = ue_store.list_recent(app.state.conn, limit=10)
    assert len(events) == 1
    assert events[0].tokens_in == 7
    assert events[0].tokens_out == 3


@pytest.mark.asyncio
async def test_proxy_populates_usage_events_tokens_after_response(app, monkeypatch):
    """Usage events get inserted at dispatch (tokens unknown) and patched
    with upstream's `usage.prompt_tokens` / `usage.completion_tokens`
    in the stream's finally block. Without this patch the predictor and
    any future billing read 0/0 for every event."""
    _seed(app, max_loras=0)
    cap = _make_engine_intercept(monkeypatch, app)  # returns body with prompt=3,completion=1

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "qwen3-test", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200, r.text
    assert cap["chats"][0]["body"]["model"] == "qwen3-test"

    events = ue_store.list_recent(app.state.conn, limit=10)
    assert len(events) == 1
    assert events[0].tokens_in == 3
    assert events[0].tokens_out == 1


@pytest.mark.asyncio
async def test_proxy_routes_model_alias_to_service_profile(app, monkeypatch):
    _seed(app, max_loras=0)
    conn = app.state.conn
    profile_store.create(
        conn,
        name="qwen-service",
        model_name="qwen3-test",
        hf_repo="o/qwen3",
        revision="main",
        backend="vllm",
        image_tag="vllm:test",
        gpu_ids=[0],
        tensor_parallel=1,
        max_model_len=4096,
        dtype="auto",
    )
    route_store.create(
        conn,
        name="public-chat",
        match_model="chat",
        profile_name="qwen-service",
    )
    cap = _make_engine_intercept(monkeypatch, app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200, r.text
    assert cap["chats"][0]["body"]["model"] == "qwen3-test"

    events = ue_store.list_recent(conn, limit=10)
    assert len(events) == 1
    assert events[0].model_name == "chat"
    assert events[0].base_name == "qwen3-test"


@pytest.mark.asyncio
async def test_proxy_uses_service_route_fallback_when_primary_not_ready(app, monkeypatch):
    _seed(app, max_loras=0)
    conn = app.state.conn
    profile_store.create(
        conn,
        name="cold-primary",
        model_name="cold-model",
        hf_repo="o/cold",
        revision="main",
        backend="vllm",
        image_tag="vllm:test",
        gpu_ids=[0],
        tensor_parallel=1,
        max_model_len=4096,
        dtype="auto",
    )
    profile_store.create(
        conn,
        name="warm-fallback",
        model_name="qwen3-test",
        hf_repo="o/qwen3",
        revision="main",
        backend="vllm",
        image_tag="vllm:test",
        gpu_ids=[0],
        tensor_parallel=1,
        max_model_len=4096,
        dtype="auto",
    )
    route_store.create(
        conn,
        name="public-chat",
        match_model="chat",
        profile_name="cold-primary",
        fallback_profile_name="warm-fallback",
    )
    cap = _make_engine_intercept(monkeypatch, app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200, r.text
    assert cap["chats"][0]["body"]["model"] == "qwen3-test"

    events = ue_store.list_recent(conn, limit=10)
    assert len(events) == 1
    assert events[0].model_name == "chat"
    assert events[0].base_name == "qwen3-test"


@pytest.mark.asyncio
async def test_proxy_rejects_adapter_exceeding_deployment_max_lora_rank(
    app, monkeypatch, tmp_path,
):
    """When the adapter's lora_rank exceeds the deployment's max_lora_rank,
    the proxy must fail fast with an actionable message instead of letting
    the engine produce a cryptic 500 on /v1/load_lora_adapter. The engine
    load endpoint must NOT be called.
    """
    _, _ = _seed_with_rank(app, deployment_max_lora_rank=16)
    a = ad_store.add(
        app.state.conn, name="big-lora", base_model_name="qwen3-test",
        hf_repo="o/big-lora",
    )
    adir = tmp_path / "snap"
    adir.mkdir()
    ad_store.set_local_path(app.state.conn, a.id, str(adir))
    ad_store.set_lora_rank(app.state.conn, a.id, 64)

    cap = _make_engine_intercept(monkeypatch, app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "big-lora", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code >= 400
    body = r.json()
    assert "lora_rank" in body["detail"] or "max-lora-rank" in body["detail"]
    assert "64" in body["detail"]
    assert "16" in body["detail"]
    # Engine was never asked to load the adapter.
    assert cap["loads"] == []


@pytest.mark.asyncio
async def test_proxy_rejects_adapter_exceeding_default_engine_rank(
    app, monkeypatch, tmp_path,
):
    """If the operator didn't pass --max-lora-rank (deployment.max_lora_rank=0),
    we fall back to the engine default of 16. An adapter with r=64 must
    still be caught before hitting the engine."""
    _, _ = _seed_with_rank(app, deployment_max_lora_rank=0)  # operator didn't set
    a = ad_store.add(
        app.state.conn, name="big-lora", base_model_name="qwen3-test",
        hf_repo="o/big-lora",
    )
    adir = tmp_path / "snap"
    adir.mkdir()
    ad_store.set_local_path(app.state.conn, a.id, str(adir))
    ad_store.set_lora_rank(app.state.conn, a.id, 64)

    cap = _make_engine_intercept(monkeypatch, app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "big-lora", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code >= 400
    assert cap["loads"] == []


@pytest.mark.asyncio
async def test_proxy_allows_adapter_within_max_lora_rank(
    app, monkeypatch, tmp_path,
):
    """Compatible adapter (rank <= deployment's max_lora_rank) loads
    normally - this is the regression check that the pre-flight doesn't
    over-trigger."""
    _, _ = _seed_with_rank(app, deployment_max_lora_rank=64)
    a = ad_store.add(
        app.state.conn, name="r32-lora", base_model_name="qwen3-test",
        hf_repo="o/lora",
    )
    adir = tmp_path / "snap"
    adir.mkdir()
    ad_store.set_local_path(app.state.conn, a.id, str(adir))
    ad_store.set_lora_rank(app.state.conn, a.id, 32)

    cap = _make_engine_intercept(monkeypatch, app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "r32-lora", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200, r.text
    assert len(cap["loads"]) == 1


@pytest.mark.asyncio
async def test_proxy_dispatches_bare_base_unchanged(app, monkeypatch):
    _seed(app, max_loras=0)
    cap = _make_engine_intercept(monkeypatch, app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "qwen3-test", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    # No adapter load triggered
    assert cap["loads"] == []
    # Upstream `model` field unchanged
    assert cap["chats"][0]["body"]["model"] == "qwen3-test"


@pytest.mark.asyncio
async def test_proxy_resolves_bare_adapter_name_and_rewrites_model(app, monkeypatch, tmp_path):
    """Client says model='tone-formal' (an adapter); proxy must
    (a) hot-load the adapter, (b) rewrite upstream model to 'tone-formal'."""
    _, dep = _seed(app, max_loras=4)
    a = ad_store.add(
        app.state.conn, name="tone-formal", base_model_name="qwen3-test",
        hf_repo="o/lora",
    )
    adir = tmp_path / "models--o--lora" / "snapshots" / "abc"
    adir.mkdir(parents=True)
    ad_store.set_local_path(app.state.conn, a.id, str(adir))

    cap = _make_engine_intercept(monkeypatch, app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "tone-formal", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200, r.text
    # Adapter was hot-loaded into the engine
    assert len(cap["loads"]) == 1
    assert cap["loads"][0]["json"]["lora_name"] == "tone-formal"
    # Upstream `model` was rewritten (no-op here since name was already the adapter)
    assert cap["chats"][0]["body"]["model"] == "tone-formal"
    # Junction row created
    assert da_store.list_for_deployment(app.state.conn, dep.id)[0].name == "tone-formal"


@pytest.mark.asyncio
async def test_proxy_composite_form_rewrites_model_to_adapter(app, monkeypatch, tmp_path):
    """Client says model='qwen3-test:tone-formal'; proxy must rewrite
    upstream `model` to just 'tone-formal' (engines don't understand the
    composite syntax)."""
    _, _ = _seed(app, max_loras=4)
    a = ad_store.add(
        app.state.conn, name="tone-formal", base_model_name="qwen3-test",
        hf_repo="o/lora",
    )
    adir = tmp_path / "models--o--lora" / "snapshots" / "abc"
    adir.mkdir(parents=True)
    ad_store.set_local_path(app.state.conn, a.id, str(adir))

    cap = _make_engine_intercept(monkeypatch, app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={
                "model": "qwen3-test:tone-formal",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert r.status_code == 200, r.text
    assert cap["chats"][0]["body"]["model"] == "tone-formal"


@pytest.mark.asyncio
async def test_proxy_503_for_adapter_with_no_lora_deployment(app, monkeypatch):
    """Adapter exists but no deployment has --max-loras > 0 -> clear 503."""
    _seed(app, max_loras=0)
    ad_store.add(
        app.state.conn, name="x", base_model_name="qwen3-test", hf_repo="o/lora",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 503
    assert "max-loras" in r.json()["detail"]


@pytest.mark.asyncio
async def test_proxy_404_for_unknown_composite_adapter(app):
    _seed(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "qwen3-test:nope", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_v1_models_lists_adapters_alongside_bases(app):
    _, _ = _seed(app)
    ad_store.add(
        app.state.conn, name="tone-formal", base_model_name="qwen3-test",
        hf_repo="o/lora",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/v1/models")
    assert r.status_code == 200
    ids = {x["id"] for x in r.json()["data"]}
    assert "qwen3-test" in ids
    assert "tone-formal" in ids
    adapter_entry = next(x for x in r.json()["data"] if x["id"] == "tone-formal")
    assert adapter_entry["base"] == "qwen3-test"
