from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from serve_engine.daemon.dispatch import open_upstream_stream
from serve_engine.daemon.dispatch_errors import NodeUnreachableError


@dataclass
class FakeDep:
    id: int
    node_id: int
    container_id: str | None
    container_address: str | None
    container_port: int | None


@pytest.mark.asyncio
async def test_remote_without_live_link_raises_node_unreachable():
    registry = MagicMock()
    registry.get.return_value = None  # no link
    dep = FakeDep(id=1, node_id=99, container_id="c", container_address=None,
                  container_port=None)
    with pytest.raises(NodeUnreachableError) as exc:
        await open_upstream_stream(
            deployment=dep, local_node_id=0, registry=registry,
            method="POST", path="/v1/x", headers={}, body=b"{}",
        )
    assert exc.value.node_id == 99


@pytest.mark.asyncio
async def test_remote_with_link_not_ready_raises_node_unreachable():
    link = MagicMock()
    link.is_ready = False
    registry = MagicMock()
    registry.get.return_value = link
    dep = FakeDep(id=1, node_id=99, container_id="c", container_address=None,
                  container_port=None)
    with pytest.raises(NodeUnreachableError) as exc:
        await open_upstream_stream(
            deployment=dep, local_node_id=0, registry=registry,
            method="POST", path="/v1/x", headers={}, body=b"{}",
        )
    assert exc.value.node_id == 99


@pytest.mark.asyncio
async def test_remote_with_no_container_id_raises_node_unreachable():
    link = MagicMock()
    link.is_ready = True
    registry = MagicMock()
    registry.get.return_value = link
    dep = FakeDep(id=1, node_id=99, container_id=None, container_address=None,
                  container_port=None)
    with pytest.raises(NodeUnreachableError):
        await open_upstream_stream(
            deployment=dep, local_node_id=0, registry=registry,
            method="POST", path="/v1/x", headers={}, body=b"{}",
        )


@pytest.mark.asyncio
async def test_local_decision_when_node_id_matches_local():
    """When deployment.node_id equals local_node_id, dispatch goes local
    (no AgentLink lookup). We exercise this by passing a registry whose
    .get would error if called — confirming we never call it."""
    registry = MagicMock()
    registry.get.side_effect = AssertionError("should not consult registry")
    dep = FakeDep(id=1, node_id=5, container_id="c",
                  container_address="127.0.0.1", container_port=8000)
    # Stub the local-open to avoid an httpx round-trip. Return a 200
    # UpstreamOpen so the wrapper's status check passes through.
    from serve_engine.daemon import dispatch
    async def noop_body():
        if False:
            yield b""
    async def noop_aclose(): return None
    fake_open = dispatch.UpstreamOpen(
        status=200, headers={}, body_iter=noop_body(), aclose=noop_aclose,
    )
    async def fake_local(*args, **kw):
        return fake_open
    saved = dispatch._open_local
    dispatch._open_local = fake_local
    try:
        result = await open_upstream_stream(
            deployment=dep, local_node_id=5, registry=registry,
            method="POST", path="/v1/x", headers={}, body=b"{}",
        )
    finally:
        dispatch._open_local = saved
    assert result.status == 200


@pytest.mark.asyncio
async def test_retryable_5xx_status_raises_upstream_http_error():
    """A pre-first-byte 503 from the upstream must surface as
    UpstreamHttpError so dispatch_with_retry can fall through."""
    from serve_engine.daemon import dispatch
    from serve_engine.daemon.dispatch_errors import UpstreamHttpError

    async def empty_body():
        if False:
            yield b""

    closed = [False]
    async def aclose():
        closed[0] = True

    fake_open = dispatch.UpstreamOpen(
        status=503, headers={}, body_iter=empty_body(), aclose=aclose,
    )
    async def fake_local(*args, **kw):
        return fake_open
    saved = dispatch._open_local
    dispatch._open_local = fake_local
    try:
        with pytest.raises(UpstreamHttpError) as exc:
            await open_upstream_stream(
                deployment=FakeDep(1, 5, "c", "127.0.0.1", 8000),
                local_node_id=5, registry=MagicMock(),
                method="POST", path="/v1/x", headers={}, body=b"{}",
            )
        assert exc.value.status == 503
    finally:
        dispatch._open_local = saved
    assert closed[0]
