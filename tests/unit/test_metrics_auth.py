"""Auth on the /metrics endpoint of the public listener."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from serve_engine.backends.vllm import VLLMBackend
from serve_engine.daemon.app import build_apps
from serve_engine.store import api_keys as ak_store
from serve_engine.store import db


def _public_app(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    public_app, _cluster, _uds = build_apps(
        conn=conn, docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
    )
    return public_app, conn


@pytest.mark.asyncio
async def test_metrics_unauth_when_no_keys_exist_is_allowed(tmp_path):
    """Bootstrap window: before any key is minted, /metrics is still
    reachable (operator may be checking startup health). Once a key
    exists, auth kicks in."""
    app, _ = _public_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://1.2.3.4",
    ) as c:
        r = await c.get("/metrics")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_metrics_requires_auth_when_keys_exist(tmp_path):
    app, conn = _public_app(tmp_path)
    ak_store.create(conn, name="ops", tier="admin")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://1.2.3.4",
    ) as c:
        r = await c.get("/metrics")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_metrics_with_valid_key_returns_200(tmp_path):
    app, conn = _public_app(tmp_path)
    secret, _ = ak_store.create(conn, name="scraper", tier="standard")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://1.2.3.4",
    ) as c:
        r = await c.get(
            "/metrics",
            headers={"Authorization": f"Bearer {secret}"},
        )
    assert r.status_code == 200
    assert b"serve_" in r.content
