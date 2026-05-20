from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from berth.backends.vllm import VLLMBackend
from berth.daemon.app import build_apps
from berth.store import db


def _apps(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return build_apps(
        conn=conn,
        docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        serve_home=tmp_path,
    )


@pytest.mark.asyncio
async def test_public_admin_requires_auth_when_no_keys_exist(tmp_path):
    public_app, _cluster_app, _uds_app = _apps(tmp_path)
    transport = httpx.ASGITransport(app=public_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/admin/keys")

    assert r.status_code == 401


@pytest.mark.asyncio
async def test_control_surface_can_create_first_key_when_no_keys_exist(tmp_path):
    _public_app, _cluster_app, uds_app = _apps(tmp_path)
    transport = httpx.ASGITransport(app=uds_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/admin/keys", json={"name": "root", "tier": "admin"})

    assert r.status_code == 201
    assert r.json()["secret"].startswith("sk-")
