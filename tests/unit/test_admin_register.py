from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from berth.backends.vllm import VLLMBackend
from berth.daemon.app import build_app
from berth.lifecycle.docker_client import ContainerHandle
from berth.store import db
from berth.store import nodes as nodes_store


@pytest.fixture
def app(tmp_path, monkeypatch):
    from berth.lifecycle.topology import GPUInfo, Topology

    monkeypatch.setattr(
        "berth.lifecycle.manager.wait_healthy",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "berth.lifecycle.manager.download_model_async",
        AsyncMock(return_value=str(tmp_path / "weights")),
    )
    (tmp_path / "weights").mkdir(exist_ok=True)

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
        conn=conn,
        docker_client=docker_client,
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        topology=topology,
        serve_home=tmp_path,
    )


@pytest.mark.asyncio
async def test_register_with_valid_token_issues_cert(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        enroll = (await c.post("/admin/nodes/enroll",
                               json={"label": "agent-a"})).json()
        r = await c.post("/admin/nodes/register", json={
            "token": enroll["token"],
            "host_info": {
                "agent_version": "0.0.1",
                "cpu_count": 8, "total_ram_mb": 32000,
                "gpu_count": 1, "total_vram_mb": 81920,
                "gpus": [{"index": 0, "name": "H100",
                          "total_vram_mb": 81920, "driver_version": "555.42"}],
            },
        })
    assert r.status_code == 200, r.text
    data = r.json()
    assert "node_id" in data
    assert "BEGIN CERTIFICATE" in data["agent_cert"]
    assert "BEGIN PRIVATE KEY" in data["agent_key"]
    assert "BEGIN CERTIFICATE" in data["ca_cert"]


@pytest.mark.asyncio
async def test_register_persists_node_with_fingerprint(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        enroll = (await c.post("/admin/nodes/enroll",
                               json={"label": "agent-z"})).json()
        await c.post("/admin/nodes/register", json={
            "token": enroll["token"],
            "host_info": {"cpu_count": 1, "total_ram_mb": 1,
                          "gpu_count": 0, "total_vram_mb": 0, "gpus": []},
        })
    conn = app.state.conn
    n = nodes_store.find_by_label(conn, "agent-z")
    assert n is not None
    assert n.fingerprint.startswith("sha256:")


@pytest.mark.asyncio
async def test_register_with_bad_token_rejected(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/admin/nodes/register", json={
            "token": "garbage",
            "host_info": {"cpu_count": 1, "total_ram_mb": 1,
                          "gpu_count": 0, "total_vram_mb": 0, "gpus": []},
        })
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_register_token_is_single_use(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        enroll = (await c.post("/admin/nodes/enroll",
                               json={"label": "agent-once"})).json()
        body = {
            "token": enroll["token"],
            "host_info": {"cpu_count": 1, "total_ram_mb": 1,
                          "gpu_count": 0, "total_vram_mb": 0, "gpus": []},
        }
        first = await c.post("/admin/nodes/register", json=body)
        second = await c.post("/admin/nodes/register", json=body)
    assert first.status_code == 200
    assert second.status_code == 403


@pytest.mark.asyncio
async def test_register_replaces_fingerprint_on_re_enrollment(app):
    """Re-enrolling under the same label rotates the cert / fingerprint."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        body = {"host_info": {"cpu_count": 1, "total_ram_mb": 1,
                              "gpu_count": 0, "total_vram_mb": 0, "gpus": []}}
        t1 = (await c.post("/admin/nodes/enroll",
                           json={"label": "agent-roll"})).json()["token"]
        r1 = (await c.post("/admin/nodes/register",
                           json={"token": t1, **body})).json()
        t2 = (await c.post("/admin/nodes/enroll",
                           json={"label": "agent-roll"})).json()["token"]
        r2 = (await c.post("/admin/nodes/register",
                           json={"token": t2, **body})).json()
    assert r1["node_id"] == r2["node_id"]
    assert r1["agent_cert"] != r2["agent_cert"]
