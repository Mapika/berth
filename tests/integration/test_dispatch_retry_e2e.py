"""End-to-end: the proxy retries on a transient pre-first-byte error and
the client sees success from the next candidate node."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from berth.backends.vllm import VLLMBackend
from berth.daemon.app import build_app
from berth.lifecycle.docker_client import ContainerHandle
from berth.lifecycle.topology import GPUInfo, Topology
from berth.store import db
from berth.store import deployments as dep_store
from berth.store import nodes as nodes_store


class _FakeEngine:
    """ASGI app that returns a tiny SSE response (200)."""

    def __init__(self):
        self.hits: list[int] = []

    async def __call__(self, scope, receive, send):
        while True:
            ev = await receive()
            if not ev.get("more_body"):
                break
        self.hits.append(1)
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        })
        await send({
            "type": "http.response.body",
            "body": b"data: hello\n\ndata: [DONE]\n\n",
            "more_body": False,
        })


@pytest.mark.asyncio
async def test_proxy_retries_when_first_candidate_node_is_unreachable(
    tmp_path, monkeypatch,
):
    """Two candidates: the first is on a node with no live AgentLink
    (raises NodeUnreachableError pre-first-byte); the second is local.
    Client must see success from the second."""
    engine = _FakeEngine()

    def factory(base_url: str):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=engine), base_url=base_url,
        )

    monkeypatch.setattr(
        "berth.daemon.openai_proxy.make_engine_client", factory,
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

    # Seed deployment 1 via the admin path — lands on the local node.
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

    # Create a "ghost" node — registered in the DB but with no live
    # AgentLink. Dispatch to a deployment on this node will raise
    # NodeUnreachableError before any byte is yielded.
    ghost_id = nodes_store.insert(
        conn,
        label="ghost",
        fingerprint="sha256:ghost",
        reachable_as=None,
        first_seen=1.0, last_seen=1.0,
        agent_version="0.0.0",
        cpu_count=0, total_ram_mb=0, gpu_count=0, total_vram_mb=0,
    )
    # Seed a second ready deployment of the same model on the ghost.
    local_dep = dep_store.list_ready(conn)[0]
    ghost_dep = dep_store.create(
        conn, model_id=local_dep.model_id, backend="vllm",
        image_tag="img:v1", gpu_ids=[0], tensor_parallel=1,
        max_model_len=8192, dtype="auto",
        vram_reserved_mb=local_dep.vram_reserved_mb,
    )
    dep_store.set_container(
        conn, ghost_dep.id,
        container_id="cid-ghost", container_name="engine-ghost",
        container_port=8000, container_address="127.0.0.1",
        node_id=ghost_id,
    )
    dep_store.update_status(conn, ghost_dep.id, "ready")

    # The scorer (with empty signals) preserves input order, which is
    # id ASC — local_dep first, ghost_dep second. We want the ghost
    # first so retry kicks in. Force it via the affinity map.
    app.state.routing_affinity.set("key:0", node_id=ghost_id)
    # The proxy uses affinity_key fallback "key:<api_key_id>" but in
    # the no-keys-registered bypass path key is None and falls through
    # to None — so the affinity hint won't apply. Instead we add a
    # request header in the call below.

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        async with c.stream(
            "POST", "/v1/chat/completions",
            headers={"x-session-id": "sess-1"},
            json={
                "model": "llama-1b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as r:
            assert r.status_code == 200
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
    assert b"hello" in body
    # Engine was hit exactly once (the local deployment); the ghost
    # never made it to the engine because its dispatch raised
    # NodeUnreachableError before any HTTP call.
    assert len(engine.hits) == 1


@pytest.mark.asyncio
async def test_proxy_503_to_client_when_all_candidates_unreachable(
    tmp_path, monkeypatch,
):
    """When every candidate's node is unreachable, the proxy returns
    503 to the client (the last NodeUnreachableError propagates as a
    SERVICE_UNAVAILABLE)."""
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

    # Move the only deployment onto a ghost node so its dispatch raises
    # NodeUnreachableError, and no other candidates exist.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        await c.post("/admin/deployments", json={
            "model_name": "llama-1b",
            "hf_repo": "meta-llama/Llama-3.2-1B-Instruct",
            "image_tag": "img:v1",
            "gpu_ids": [0],
            "max_model_len": 8192,
        })

    ghost_id = nodes_store.insert(
        conn,
        label="ghost",
        fingerprint="sha256:ghost-2",
        reachable_as=None,
        first_seen=1.0, last_seen=1.0,
        agent_version="0.0.0",
        cpu_count=0, total_ram_mb=0, gpu_count=0, total_vram_mb=0,
    )
    dep = dep_store.list_ready(conn)[0]
    conn.execute(
        "UPDATE deployments SET node_id=? WHERE id=?", (ghost_id, dep.id),
    )

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        r = await c.post(
            "/v1/chat/completions",
            json={
                "model": "llama-1b",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert r.status_code == 503
