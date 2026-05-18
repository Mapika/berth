"""End-to-end vertical-slice test for the multi-node tunneled path.

Exercises the chain:
  enrollment token → /admin/nodes/register → cert issued → hub accepts the
  matching fingerprint → AgentLink registered → router can dispatch
  start_deployment through it and stream HTTP chunks back.

WS transport is verified separately in tests/unit/test_leader_hub.py;
RemoteAgentLink semantics in tests/unit/test_remote_agent.py. This test
binds the pieces together using a fake WS adapter so we don't have to
make FastAPI's TestClient cooperate with asyncio.create_task.
"""
from __future__ import annotations

import asyncio
import base64
from unittest.mock import MagicMock

import httpx
import pytest

from serve_engine.backends.vllm import VLLMBackend
from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.cluster.leader_hub import _WSAdapter  # noqa: F401 — used implicitly
from serve_engine.cluster.protocol import (
    Hello,
    HttpChunk,
    HttpRequest,
    OpResult,
    StartDeployment,
    Welcome,
    decode_frame,
    encode_frame,
)
from serve_engine.cluster.remote_agent import RemoteAgentLink
from serve_engine.daemon.openai_proxy import _proxy_via_link
from serve_engine.daemon.app import build_app
from serve_engine.lifecycle.docker_client import ContainerHandle
from serve_engine.store import db
from serve_engine.store import nodes as nodes_store


class _FakeWS:
    """Duplex queues simulating a WebSocket between leader and agent."""

    def __init__(self):
        self._to_agent: asyncio.Queue[str | None] = asyncio.Queue()
        self._from_agent: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, msg: str) -> None:
        await self._to_agent.put(msg)

    async def recv(self) -> str | None:
        return await self._from_agent.get()

    async def leader_reads(self) -> str:
        m = await self._to_agent.get()
        assert m is not None
        return m

    async def agent_pushes(self, msg: str | None) -> None:
        await self._from_agent.put(msg)


@pytest.mark.asyncio
async def test_enrollment_then_router_routes_through_remote_link(tmp_path):
    """The slice end-to-end (control plane only — WS transport via fake)."""
    from serve_engine.lifecycle.topology import GPUInfo, Topology

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

    app = build_app(
        conn=conn,
        docker_client=docker_client,
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        topology=topology,
        serve_home=tmp_path,
    )

    # 1) Enroll a new agent — get back a one-time token.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        enroll = (await c.post("/admin/nodes/enroll",
                               json={"label": "agent-x"})).json()
        # 2) The agent presents the token and host info; backend issues a cert.
        reg = (await c.post("/admin/nodes/register", json={
            "token": enroll["token"],
            "host_info": {"cpu_count": 4, "total_ram_mb": 8000,
                          "gpu_count": 1, "total_vram_mb": 81920,
                          "gpus": [{"index": 0, "name": "H100",
                                    "total_vram_mb": 81920,
                                    "driver_version": "x"}]},
        })).json()
    assert "agent_cert" in reg

    # The node row now exists with the agent's fingerprint.
    node = nodes_store.get(conn, reg["node_id"])
    assert node is not None
    assert node.label == "agent-x"
    assert node.fingerprint.startswith("sha256:")

    # 3) Simulate the WS handshake that LeaderHub does in production —
    # construct a RemoteAgentLink with a fake duplex WS and register it.
    ws = _FakeWS()
    link = RemoteAgentLink(node_id=reg["node_id"], ws=ws)
    registry: AgentRegistry = app.state.agent_registry
    registry.register(link)
    consumer = asyncio.create_task(link.run())

    # 4) The leader's router (via _proxy_via_link) dispatches /v1/* via
    # this link; the fake "agent" answers with an HttpChunk stream.
    async def fake_agent_answer():
        sent = await ws.leader_reads()
        req = decode_frame(sent)
        assert isinstance(req, HttpRequest)
        sid = req.stream_id
        await ws.agent_pushes(encode_frame(HttpChunk(
            stream_id=sid, body_b64=base64.b64encode(b"hi").decode(),
            eof=False, status=200, headers={"content-type": "text/plain"},
        )))
        await ws.agent_pushes(encode_frame(HttpChunk(
            stream_id=sid, body_b64="", eof=True,
        )))

    agent_task = asyncio.create_task(fake_agent_answer())
    status_code, headers, chunks = await asyncio.wait_for(
        _proxy_via_link(
            registry=registry, node_id=node.id, container_id="cid-from-agent",
            method="POST", path="/v1/chat/completions",
            headers={"content-type": "application/json"},
            body=b'{"model":"foo"}',
        ),
        timeout=3.0,
    )

    assert status_code == 200
    assert headers["content-type"] == "text/plain"
    assert b"".join(chunks) == b"hi"

    await agent_task

    # 5) Drive a start_deployment from the leader side and answer it from
    # the fake agent — verifies the lifecycle RPC also works end-to-end.
    async def fake_agent_starts():
        sent = await ws.leader_reads()
        f = decode_frame(sent)
        assert isinstance(f, StartDeployment)
        await ws.agent_pushes(encode_frame(OpResult(
            request_id=f.request_id, ok=True,
            data={"container_id": "remote-cid", "address": "tunnel", "port": 0},
        )))

    asyncio.create_task(fake_agent_starts())
    started = await asyncio.wait_for(
        link.start_deployment({"image": "x", "name": "d"}), timeout=3.0,
    )
    assert started.container_id == "remote-cid"
    assert started.address == "tunnel"

    # Clean up
    link.shutdown()
    await ws.agent_pushes(None)
    await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
async def test_unreachable_node_503_via_helper(tmp_path):
    """If the registry has no AgentLink for the node, _proxy_via_link must
    raise RuntimeError('not connected') so the route handler can 503."""
    registry = AgentRegistry()
    with pytest.raises(RuntimeError, match="not connected"):
        await _proxy_via_link(
            registry=registry, node_id=777, container_id="cid",
            method="GET", path="/", headers={}, body=b"",
        )
