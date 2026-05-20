from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from berth.cluster.agent_registry import AgentRegistry
from berth.cluster.leader_hub import LeaderHub, _default_fingerprint_resolver
from berth.cluster.protocol import (
    Hello,
    Welcome,
    decode_frame,
    encode_frame,
)
from berth.store import db
from berth.store import nodes as nodes_store


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


class _FakeWebSocket:
    def __init__(self, *, client, headers):
        self.scope = {"extensions": {}}
        self.client = client
        self.headers = headers


def test_forwarded_fingerprint_rejects_untrusted_direct_peer(monkeypatch):
    monkeypatch.setenv("SERVE_TRUST_FORWARDED_FP", "1")
    monkeypatch.setenv("SERVE_FORWARDED_ALLOW_IPS", "127.0.0.1")
    ws = _FakeWebSocket(
        client=("198.51.100.10", 44444),
        headers={"x-serve-client-fingerprint": "sha256:aaa"},
    )

    assert _default_fingerprint_resolver(ws) is None


def test_forwarded_fingerprint_accepts_allowed_proxy_peer(monkeypatch):
    monkeypatch.setenv("SERVE_TRUST_FORWARDED_FP", "1")
    monkeypatch.setenv("SERVE_FORWARDED_ALLOW_IPS", "127.0.0.1")
    ws = _FakeWebSocket(
        client=("127.0.0.1", 44444),
        headers={"x-serve-client-fingerprint": "sha256:aaa"},
    )

    assert _default_fingerprint_resolver(ws) == "sha256:aaa"


def test_forwarded_fingerprint_supports_berth_env_names(monkeypatch):
    monkeypatch.setenv("BERTH_TRUST_FORWARDED_FP", "1")
    monkeypatch.setenv("BERTH_FORWARDED_ALLOW_IPS", "10.0.0.0/8")
    ws = _FakeWebSocket(
        client=("10.2.3.4", 44444),
        headers={"x-serve-client-fingerprint": "sha256:aaa"},
    )

    assert _default_fingerprint_resolver(ws) == "sha256:aaa"


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


def test_malformed_hello_inventory_is_policy_rejected(tmp_path):
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

    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/cluster/agent") as ws:
            ws.send_text(encode_frame(Hello(
                agent_version="9.9.9",
                host_info={
                    "cpu_count": "many",
                    "total_ram_mb": 64000,
                    "gpu_count": 2,
                    "total_vram_mb": 160000,
                    "gpus": [],
                },
            )))
            ws.receive_text()

    assert ei.value.code == 1008
    assert reg.get(nid) is None
    n = nodes_store.get(conn, nid)
    assert n.status != "ready"
