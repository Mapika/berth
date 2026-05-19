"""Upstream response headers must be allowlisted, not blocklisted.
A compromised engine that emits Set-Cookie / CORS / Link headers
must not have them passed through to public clients."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from serve_engine.backends.vllm import VLLMBackend
from serve_engine.daemon.app import build_app
from serve_engine.lifecycle.docker_client import ContainerHandle
from serve_engine.lifecycle.topology import GPUInfo, Topology
from serve_engine.store import db


class _NoisyEngine:
    """Engine that emits a bunch of headers our allowlist must drop."""

    async def __call__(self, scope, receive, send):
        while True:
            ev = await receive()
            if not ev.get("more_body"):
                break
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream"),
                (b"set-cookie", b"sid=abc; Secure"),
                (b"access-control-allow-origin", b"*"),
                (b"link", b"</preload>; rel=preload"),
                (b"x-frame-options", b"ALLOW-FROM https://evil.example"),
                (b"x-request-id", b"req-123"),  # allowlisted
                (b"cache-control", b"no-store"),  # allowlisted
            ],
        })
        await send({
            "type": "http.response.body",
            "body": b"data: hi\n\ndata: [DONE]\n\n",
            "more_body": False,
        })


@pytest.mark.asyncio
async def test_upstream_dangerous_headers_are_dropped(tmp_path, monkeypatch):
    engine = _NoisyEngine()

    def factory(base_url: str):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=engine), base_url=base_url,
        )

    monkeypatch.setattr(
        "serve_engine.daemon.openai_proxy.make_engine_client", factory,
    )
    monkeypatch.setattr(
        "serve_engine.lifecycle.manager.wait_healthy",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "serve_engine.lifecycle.manager.download_model_async",
        AsyncMock(return_value=str(tmp_path / "w")),
    )
    monkeypatch.setattr(
        "serve_engine.lifecycle.manager.estimate_vram_mb",
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

        async with c.stream(
            "POST", "/v1/chat/completions",
            json={
                "model": "llama-1b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            _ = [ch async for ch in resp.aiter_bytes()]
            response_headers = {k.lower(): v for k, v in resp.headers.items()}

    # Dangerous headers MUST be dropped.
    assert "set-cookie" not in response_headers
    assert "access-control-allow-origin" not in response_headers
    assert "link" not in response_headers
    assert "x-frame-options" not in response_headers
    # Allowlisted headers pass through.
    assert response_headers.get("x-request-id") == "req-123"
    assert response_headers.get("cache-control") == "no-store"
    # content-type comes through via media_type.
    assert "event-stream" in response_headers.get("content-type", "")
