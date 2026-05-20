from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from berth.backends.vllm import VLLMBackend
from berth.daemon.app import build_app
from berth.lifecycle.docker_client import ContainerHandle
from berth.store import db


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
        berth_home=tmp_path,
    )


@pytest.mark.asyncio
async def test_enroll_mints_one_time_token(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/admin/nodes/enroll", json={"label": "agent-a"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "token" in data and len(data["token"]) >= 32
    assert "ca_cert" in data
    assert "BEGIN CERTIFICATE" in data["ca_cert"]
    assert "leader_url" in data


@pytest.mark.asyncio
async def test_enroll_different_calls_yield_different_tokens(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r1 = await c.post("/admin/nodes/enroll", json={"label": "agent-a"})
        r2 = await c.post("/admin/nodes/enroll", json={"label": "agent-a"})
    assert r1.json()["token"] != r2.json()["token"]


@pytest.mark.asyncio
async def test_enroll_rejects_reserved_local_label(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/admin/nodes/enroll", json={"label": "local"})
    assert r.status_code == 400
    assert "reserved" in r.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label",
    [
        "",
        " ",
        " local",
        "local ",
        "agent\nx",
        "../agent",
        "agent/x",
        "a" * 64,
    ],
)
async def test_enroll_rejects_unsafe_labels(app, label):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/admin/nodes/enroll", json={"label": label})
    assert r.status_code == 400
    assert "label" in r.json()["detail"]


@pytest.mark.asyncio
async def test_list_includes_local_node(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/admin/nodes")
    assert r.status_code == 200, r.text
    labels = {n["label"] for n in r.json()["nodes"]}
    assert "local" in labels


@pytest.mark.asyncio
async def test_show_local_node(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        ls = (await c.get("/admin/nodes")).json()["nodes"]
        local = next(n for n in ls if n["label"] == "local")
        r = await c.get(f"/admin/nodes/{local['id']}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["node"]["label"] == "local"
    assert "gpus" in data


@pytest.mark.asyncio
async def test_show_missing_node_404(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/admin/nodes/999")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cannot_remove_local_node(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        ls = (await c.get("/admin/nodes")).json()["nodes"]
        local = next(n for n in ls if n["label"] == "local")
        r = await c.delete(f"/admin/nodes/{local['id']}")
    assert r.status_code == 400
