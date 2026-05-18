from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.cluster.leader_hub import LeaderHub
from serve_engine.cluster.protocol import (
    Hello,
    Welcome,
    decode_frame,
    encode_frame,
)
from serve_engine.store import db
from serve_engine.store import nodes as nodes_store


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.init_schema(conn)
    return conn


def _app_with_hub(conn, registry, fingerprint):
    hub = LeaderHub(
        conn=conn,
        registry=registry,
        fingerprint_resolver=lambda ws: fingerprint,
    )
    app = FastAPI()
    app.include_router(hub.router)
    return app


def test_handshake_registers_agent(tmp_path):
    conn = _fresh(tmp_path)
    nid = nodes_store.insert(
        conn, label="agent-a", fingerprint="sha256:aaa",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    reg = AgentRegistry()
    app = _app_with_hub(conn, reg, "sha256:aaa")
    client = TestClient(app)
    with client.websocket_connect("/cluster/agent") as ws:
        ws.send_text(encode_frame(Hello(
            agent_version="0.0.1",
            host_info={"cpu_count": 1, "total_ram_mb": 1,
                       "gpu_count": 0, "total_vram_mb": 0, "gpus": []},
        )))
        welcome = decode_frame(ws.receive_text())
        assert isinstance(welcome, Welcome)
        assert welcome.node_id == nid
        # While connected, the registry has a live link for this node.
        assert reg.get(nid) is not None
    # On context-manager exit the WS closes; hub should unregister.
    assert reg.get(nid) is None


def test_unknown_fingerprint_rejected(tmp_path):
    conn = _fresh(tmp_path)
    # No matching node row.
    reg = AgentRegistry()
    app = _app_with_hub(conn, reg, "sha256:unknown")
    client = TestClient(app)
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/cluster/agent") as ws:
            ws.receive_text()


def test_no_fingerprint_header_rejected(tmp_path):
    conn = _fresh(tmp_path)
    reg = AgentRegistry()
    app = _app_with_hub(conn, reg, None)
    client = TestClient(app)
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/cluster/agent") as ws:
            ws.receive_text()


def test_hello_updates_node_status_and_inventory(tmp_path):
    conn = _fresh(tmp_path)
    nid = nodes_store.insert(
        conn, label="agent-a", fingerprint="sha256:aaa",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    reg = AgentRegistry()
    app = _app_with_hub(conn, reg, "sha256:aaa")
    client = TestClient(app)
    with client.websocket_connect("/cluster/agent") as ws:
        ws.send_text(encode_frame(Hello(
            agent_version="9.9.9",
            host_info={"cpu_count": 16, "total_ram_mb": 64000,
                       "gpu_count": 2, "total_vram_mb": 160000, "gpus": []},
        )))
        ws.receive_text()  # consume welcome
        n = nodes_store.get(conn, nid)
        assert n.agent_version == "9.9.9"
        assert n.cpu_count == 16
        assert n.total_vram_mb == 160000
        assert n.status == "ready"
