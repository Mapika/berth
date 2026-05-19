"""Body-size cap on the public listener."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from berth.backends.vllm import VLLMBackend
from berth.daemon.app import build_apps
from berth.store import db


def _public_app(tmp_path):
    docker_client = MagicMock()
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    public_app, _cluster, _uds = build_apps(
        conn=conn, docker_client=docker_client,
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
    )
    return public_app


@pytest.mark.asyncio
async def test_oversize_post_returns_413(tmp_path):
    """Content-Length above the cap → 413 without the proxy ever
    buffering the body."""
    app = _public_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    big = b"x" * (12 * 1024 * 1024)  # 12 MB; default cap is 10 MB
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            content=big,
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 413
    assert "exceeds" in r.json()["detail"]


@pytest.mark.asyncio
async def test_under_cap_post_reaches_proxy(tmp_path):
    """A body well under the cap is not blocked by the middleware.
    The 503 we get is from the no-deployment path, which proves the
    request actually reached the proxy."""
    app = _public_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    payload = '{"model": "llama-1b", "messages": []}'
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            content=payload,
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 503, r.text
    assert "no ready deployment" in r.json()["detail"]


@pytest.mark.asyncio
async def test_uds_app_has_no_body_size_cap(tmp_path):
    """uds_app deliberately omits the cap; operator endpoints accept
    larger uploads. Hit the same endpoint on uds_app with an oversize
    body → not blocked by the middleware (the proxy will reject it
    with 503 for no-deployment, not 413)."""
    docker_client = MagicMock()
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    _public, _cluster, uds_app = build_apps(
        conn=conn, docker_client=docker_client,
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
    )
    transport = httpx.ASGITransport(app=uds_app)
    big = b"x" * (12 * 1024 * 1024)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/chat/completions",
            content=big,
            headers={"content-type": "application/json"},
        )
    assert r.status_code != 413
