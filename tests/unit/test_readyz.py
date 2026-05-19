"""/healthz and /readyz distinction."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from serve_engine.backends.vllm import VLLMBackend
from serve_engine.daemon.app import build_app, build_apps
from serve_engine.store import db


@pytest.mark.asyncio
async def test_healthz_returns_200_even_before_lifespan(tmp_path):
    """liveness probe — 200 unconditionally, used by kubelet/Caddy to
    decide whether to restart the daemon."""
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    app = build_app(
        conn=conn, docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/healthz")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_readyz_returns_503_before_lifespan_flips_flag(tmp_path):
    """/readyz starts at 503 (lifespan hasn't run reconcile yet) and
    only flips when ready=True is set."""
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    app = build_app(
        conn=conn, docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
    )
    # ready defaults to False
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False


@pytest.mark.asyncio
async def test_readyz_returns_200_when_ready_flag_set(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    app = build_app(
        conn=conn, docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
    )
    app.state.ready = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/readyz")
    assert r.status_code == 200
    assert r.json()["ready"] is True


@pytest.mark.asyncio
async def test_readyz_503_when_db_unreachable(tmp_path):
    """readyz fails closed if SELECT 1 raises (db connection broken)."""
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    app = build_app(
        conn=conn, docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
    )
    app.state.ready = True
    # Replace the conn with one that raises on execute.
    bad = MagicMock()
    bad.execute.side_effect = RuntimeError("db gone")
    app.state.conn = bad
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    assert "db" in r.json()["reason"].lower()
