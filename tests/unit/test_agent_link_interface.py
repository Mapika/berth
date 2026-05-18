from __future__ import annotations

import inspect

from serve_engine.cluster.agent_link import (
    AgentLink,
    ProxyResponseChunk,
    StartedContainer,
)


def test_agentlink_required_methods():
    expected = {
        "start_deployment",
        "stop_deployment",
        "proxy_request",
        "is_ready",
        "node_id",
    }
    members = {n for n, _ in inspect.getmembers(AgentLink) if not n.startswith("_")}
    assert expected.issubset(members)


def test_started_container_shape():
    c = StartedContainer(container_id="cid", address="tunnel", port=0)
    assert c.container_id == "cid"
    assert c.address == "tunnel"
    assert c.port == 0


def test_proxy_response_chunk_shape():
    c = ProxyResponseChunk(status=200, headers={"x": "y"},
                           body=b"hi", eof=False)
    assert c.status == 200
    assert c.headers["x"] == "y"
    assert c.body == b"hi"
    assert c.eof is False


def test_proxy_response_chunk_eof():
    c = ProxyResponseChunk(status=None, headers=None, body=b"", eof=True)
    assert c.eof is True
