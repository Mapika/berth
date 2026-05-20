import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from berth.backends.vllm import VLLMBackend
from berth.daemon.app import build_app
from berth.lifecycle.docker_client import ContainerHandle
from berth.store import db


class FakeEngineApp:
    """A tiny ASGI app pretending to be the upstream engine."""

    def __init__(self, response_chunks: list[bytes], status_code: int = 200):
        self.chunks = response_chunks
        self.status_code = status_code
        self.last_request_body: bytes | None = None
        self.last_request_headers: dict[str, str] = {}

    async def __call__(self, scope, receive, send):
        assert scope["type"] == "http"
        self.last_request_headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope["headers"]
        }
        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if not event.get("more_body"):
                break
        self.last_request_body = body
        await send({
            "type": "http.response.start",
            "status": self.status_code,
            "headers": [(b"content-type", b"text/event-stream")],
        })
        for i, chunk in enumerate(self.chunks):
            await send({
                "type": "http.response.body",
                "body": chunk,
                "more_body": i < len(self.chunks) - 1,
            })


@pytest.fixture
def app_with_active_deployment(tmp_path, monkeypatch):
    from berth.lifecycle.topology import GPUInfo, Topology

    fake_engine = FakeEngineApp([b"data: hello\n\n", b"data: [DONE]\n\n"])

    def fake_async_client_factory(base_url):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fake_engine),
            base_url="http://engine",
        )
    monkeypatch.setattr(
        "berth.daemon.openai_proxy.make_engine_client",
        fake_async_client_factory,
    )

    monkeypatch.setattr(
        "berth.lifecycle.manager.wait_healthy",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "berth.lifecycle.manager.download_model_async",
        AsyncMock(return_value=str(tmp_path / "weights")),
    )
    monkeypatch.setattr(
        "berth.lifecycle.manager.estimate_vram_mb",
        lambda inp: 20_000,
    )
    (tmp_path / "weights").mkdir(exist_ok=True)

    docker_client = MagicMock()
    docker_client.run.return_value = ContainerHandle(
        id="cid", name="engine", address="127.0.0.1", port=8000,
    )
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)

    topology = Topology(
        gpus=[GPUInfo(index=0, name="H100", total_mb=80 * 1024)],
        _islands={0: frozenset({0})},
    )
    app = build_app(
        conn=conn,
        docker_client=docker_client,
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        topology=topology,
    )

    async def setup():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as c:
            r = await c.post("/admin/deployments", json={
                "model_name": "llama-1b",
                "hf_repo": "meta-llama/Llama-3.2-1B-Instruct",
                "image_tag": "img:v1",
                "gpu_ids": [0],
                "max_model_len": 8192,
            })
            assert r.status_code == 201
    asyncio.run(setup())
    return app, fake_engine


@pytest.mark.asyncio
async def test_proxy_streams_response(app_with_active_deployment):
    app, fake = app_with_active_deployment
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30
    ) as c:
        async with c.stream(
            "POST", "/v1/chat/completions",
            json={
                "model": "llama-1b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as r:
            chunks = [c async for c in r.aiter_bytes()]
    assert r.status_code == 200
    body = b"".join(chunks)
    assert b"hello" in body
    assert b"[DONE]" in body
    forwarded = json.loads(fake.last_request_body)
    assert forwarded["model"] == "llama-1b"


@pytest.mark.asyncio
async def test_proxy_does_not_forward_sensitive_client_headers(
    app_with_active_deployment,
):
    app, fake = app_with_active_deployment
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        async with c.stream(
            "POST",
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer sk-public",
                "Cookie": "session=secret",
                "Proxy-Authorization": "Basic secret",
                "X-Api-Key": "upstream-secret",
                "X-Request-Id": "req-123",
            },
            json={
                "model": "llama-1b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as r:
            _ = [chunk async for chunk in r.aiter_bytes()]

    assert r.status_code == 200
    assert "authorization" not in fake.last_request_headers
    assert "cookie" not in fake.last_request_headers
    assert "proxy-authorization" not in fake.last_request_headers
    assert "x-api-key" not in fake.last_request_headers
    assert fake.last_request_headers["x-request-id"] == "req-123"
    assert fake.last_request_headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_proxy_503_when_no_active(tmp_path, monkeypatch):
    docker_client = MagicMock()
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    app = build_app(
        conn=conn,
        docker_client=docker_client,
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "llama-1b", "messages": []},
        )
    assert r.status_code == 503
    assert "no ready deployment" in r.json()["detail"]


@pytest.mark.asyncio
async def test_proxy_routes_by_model_name(app_with_active_deployment):
    app, _ = app_with_active_deployment
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "no-such-model", "messages": []},
        )
    assert r.status_code == 503
    assert "no ready deployment" in r.json()["detail"]


@pytest.mark.asyncio
async def test_proxy_routes_ready_deployment_even_when_gpu_free_memory_is_low(
    app_with_active_deployment,
):
    """A ready engine may reserve most GPU memory.

    The request router should not apply placement-time VRAM requirements
    again, or it will reject the exact deployment that is already serving.
    """
    from berth.store import nodes as node_store

    app, _ = app_with_active_deployment
    local_node = node_store.find_by_label(app.state.conn, "local")
    assert local_node is not None
    app.state.metrics_aggregator.ingest(
        node_id=local_node.id,
        sample={
            "gpus": [
                {"index": 0, "mem_total_mb": 81920, "mem_used_mb": 81420},
            ],
            "deployments": [],
        },
        ts=1.0,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "llama-1b", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert r.status_code == 200


@pytest.mark.asyncio
async def test_proxy_400_when_no_model_field(app_with_active_deployment):
    app, _ = app_with_active_deployment
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"messages": []},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_proxy_400_when_model_field_is_not_string(app_with_active_deployment):
    app, fake = app_with_active_deployment
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": ["llama-1b"], "messages": []},
        )

    assert r.status_code == 400
    assert "model" in r.json()["detail"]
    assert fake.last_request_body is None


@pytest.mark.asyncio
async def test_proxy_forwards_upstream_500(tmp_path, monkeypatch):
    """Engine returns 500 -> proxy must return 500 (not silently 200)."""
    from unittest.mock import AsyncMock, MagicMock

    from berth.backends.vllm import VLLMBackend
    from berth.lifecycle.docker_client import ContainerHandle
    from berth.lifecycle.topology import GPUInfo, Topology

    fake_engine = FakeEngineApp([b'{"error":"cuda oom"}'], status_code=500)
    def fake_async_client_factory(base_url):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fake_engine),
            base_url="http://engine",
        )
    monkeypatch.setattr(
        "berth.daemon.openai_proxy.make_engine_client",
        fake_async_client_factory,
    )
    monkeypatch.setattr(
        "berth.lifecycle.manager.wait_healthy",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "berth.lifecycle.manager.download_model_async",
        AsyncMock(return_value=str(tmp_path / "w")),
    )
    monkeypatch.setattr(
        "berth.lifecycle.manager.estimate_vram_mb",
        lambda inp: 20_000,
    )
    (tmp_path / "w").mkdir(exist_ok=True)

    docker_client = MagicMock()
    docker_client.run.return_value = ContainerHandle(
        id="cid", name="engine", address="127.0.0.1", port=8000,
    )
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    topology = Topology(
        gpus=[GPUInfo(index=0, name="H100", total_mb=80 * 1024)],
        _islands={0: frozenset({0})},
    )
    app = build_app(
        conn=conn, docker_client=docker_client,
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path, topology=topology,
    )

    # Set up a deployment
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        r = await c.post("/admin/deployments", json={
            "model_name": "llama-1b",
            "hf_repo": "meta-llama/Llama-3.2-1B-Instruct",
            "image_tag": "img:v1",
            "gpu_ids": [0],
            "max_model_len": 8192,
        })
        assert r.status_code == 201

        # Now the proxy hit should return 500, not 200
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "llama-1b", "messages": [{"role":"user","content":"hi"}]},
        )
    assert r.status_code == 500
    assert b"cuda oom" in r.content


@pytest.mark.asyncio
async def test_proxy_records_in_flight_and_latency(app_with_active_deployment):
    """After a successful request the in-flight counter must be zero
    and the latency recorder must have one sample for the dispatched
    deployment."""
    app, _ = app_with_active_deployment
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        async with c.stream(
            "POST", "/v1/chat/completions",
            json={
                "model": "llama-1b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as r:
            assert r.status_code == 200
            _ = [chunk async for chunk in r.aiter_bytes()]

    from berth.store import deployments as _dep
    dep = _dep.find_ready_by_model_name(app.state.conn, "llama-1b")
    assert dep is not None
    # In-flight released on completion.
    assert app.state.in_flight.snapshot() == {}
    # Latency recorder captured exactly one sample for this deployment.
    summary = app.state.latency.summarize_and_reset(dep.id)
    assert summary.requests_last_window == 1
    assert summary.errors_last_window == 0
