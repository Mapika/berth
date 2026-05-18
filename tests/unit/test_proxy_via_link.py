from __future__ import annotations

import pytest

from serve_engine.cluster.agent_link import ProxyResponseChunk
from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.daemon.openai_proxy import _proxy_via_link


class _RecordingLink:
    def __init__(self, nid: int, chunks: list[ProxyResponseChunk]):
        self._nid = nid
        self.calls: list[dict] = []
        self._chunks = chunks

    @property
    def node_id(self) -> int:
        return self._nid

    @property
    def is_ready(self) -> bool:
        return True

    async def start_deployment(self, plan): raise NotImplementedError
    async def stop_deployment(self, c, *, remove=True): raise NotImplementedError

    async def proxy_request(
        self, *, container_id, method, path, headers, body,
    ):
        self.calls.append({
            "container_id": container_id, "method": method,
            "path": path, "headers": headers, "body": body,
        })
        for c in self._chunks:
            yield c


@pytest.mark.asyncio
async def test_proxy_via_link_streams_chunks_with_first_status():
    chunks = [
        ProxyResponseChunk(status=200, headers={"content-type": "text/plain"},
                           body=b"hello ", eof=False),
        ProxyResponseChunk(status=None, headers=None, body=b"world", eof=False),
        ProxyResponseChunk(status=None, headers=None, body=b"", eof=True),
    ]
    link = _RecordingLink(7, chunks)
    reg = AgentRegistry()
    reg.register(link)
    status_code, headers, body_chunks = await _proxy_via_link(
        registry=reg, node_id=7, container_id="cid-x",
        method="POST", path="/v1/chat/completions",
        headers={"authorization": "Bearer x"}, body=b'{"model":"y"}',
    )
    assert status_code == 200
    assert headers["content-type"] == "text/plain"
    assert b"".join(body_chunks) == b"hello world"
    assert link.calls[0]["container_id"] == "cid-x"
    assert link.calls[0]["path"] == "/v1/chat/completions"


@pytest.mark.asyncio
async def test_proxy_via_link_raises_when_node_unreachable():
    reg = AgentRegistry()
    with pytest.raises(RuntimeError, match="not connected"):
        await _proxy_via_link(
            registry=reg, node_id=99, container_id="cid",
            method="GET", path="/", headers={}, body=b"",
        )


@pytest.mark.asyncio
async def test_proxy_via_link_strips_content_length_from_upstream_headers():
    chunks = [
        ProxyResponseChunk(
            status=200,
            headers={"content-type": "text/plain", "content-length": "11"},
            body=b"hello world", eof=False,
        ),
        ProxyResponseChunk(status=None, headers=None, body=b"", eof=True),
    ]
    reg = AgentRegistry()
    reg.register(_RecordingLink(1, chunks))
    status_code, headers, _ = await _proxy_via_link(
        registry=reg, node_id=1, container_id="cid",
        method="GET", path="/", headers={}, body=b"",
    )
    assert "content-length" not in headers
    assert status_code == 200
