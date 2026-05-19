from __future__ import annotations

import asyncio
import base64

import pytest

from serve_engine.cluster.protocol import (
    HttpCancel,
    HttpChunk,
    HttpRequest,
    OpResult,
    StartDeployment,
    StopDeployment,
    decode_frame,
    encode_frame,
)
from serve_engine.cluster.remote_agent import RemoteAgentLink


class _FakeWS:
    """In-memory duplex WS for tests. The 'agent side' uses push_from_agent
    to deliver frames into recv(); the 'leader side' reads what was sent via
    pop_to_agent."""

    def __init__(self):
        self._to_agent: asyncio.Queue[str | None] = asyncio.Queue()
        self._from_agent: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, msg: str) -> None:
        await self._to_agent.put(msg)

    async def recv(self) -> str | None:
        m = await self._from_agent.get()
        return m

    async def pop_to_agent(self) -> str:
        m = await self._to_agent.get()
        assert m is not None
        return m

    async def push_from_agent(self, frame_str: str | None) -> None:
        await self._from_agent.put(frame_str)


@pytest.mark.asyncio
async def test_start_deployment_roundtrip():
    ws = _FakeWS()
    link = RemoteAgentLink(node_id=7, ws=ws)
    run_task = asyncio.create_task(link.run())

    async def caller():
        return await link.start_deployment({"image": "x", "name": "d"})

    caller_task = asyncio.create_task(caller())

    sent = await ws.pop_to_agent()
    f = decode_frame(sent)
    assert isinstance(f, StartDeployment)
    await ws.push_from_agent(encode_frame(OpResult(
        request_id=f.request_id, ok=True,
        data={"container_id": "cid-77", "address": "tunnel", "port": 0},
    )))

    started = await asyncio.wait_for(caller_task, timeout=2.0)
    assert started.container_id == "cid-77"
    assert started.address == "tunnel"

    link.shutdown()
    await ws.push_from_agent(None)
    await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_start_deployment_failure_propagates():
    ws = _FakeWS()
    link = RemoteAgentLink(node_id=7, ws=ws)
    run_task = asyncio.create_task(link.run())

    async def caller():
        return await link.start_deployment({})

    caller_task = asyncio.create_task(caller())

    sent = await ws.pop_to_agent()
    f = decode_frame(sent)
    await ws.push_from_agent(encode_frame(OpResult(
        request_id=f.request_id, ok=False, error="docker exploded",
    )))

    with pytest.raises(RuntimeError, match="docker exploded"):
        await asyncio.wait_for(caller_task, timeout=2.0)

    link.shutdown()
    await ws.push_from_agent(None)
    await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_deployment_roundtrip():
    ws = _FakeWS()
    link = RemoteAgentLink(node_id=7, ws=ws)
    run_task = asyncio.create_task(link.run())

    async def caller():
        await link.stop_deployment("cid-1")

    caller_task = asyncio.create_task(caller())

    sent = await ws.pop_to_agent()
    f = decode_frame(sent)
    assert isinstance(f, StopDeployment)
    assert f.container_id == "cid-1"
    await ws.push_from_agent(encode_frame(OpResult(request_id=f.request_id, ok=True)))

    await asyncio.wait_for(caller_task, timeout=2.0)

    link.shutdown()
    await ws.push_from_agent(None)
    await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_proxy_request_streams_chunks():
    ws = _FakeWS()
    link = RemoteAgentLink(node_id=7, ws=ws)
    run_task = asyncio.create_task(link.run())

    async def consumer():
        chunks = []
        async for c in link.proxy_request(
            container_id="cid", method="GET", path="/v1/models",
            headers={}, body=b"",
        ):
            chunks.append(c)
        return chunks

    consumer_task = asyncio.create_task(consumer())

    sent = await ws.pop_to_agent()
    req = decode_frame(sent)
    assert isinstance(req, HttpRequest)
    assert req.headers["x-serve-container-id"] == "cid"
    sid = req.stream_id

    await ws.push_from_agent(encode_frame(HttpChunk(
        stream_id=sid, body_b64=base64.b64encode(b"hi").decode(),
        eof=False, status=200, headers={"content-type": "text/plain"},
    )))
    await ws.push_from_agent(encode_frame(HttpChunk(
        stream_id=sid, body_b64=base64.b64encode(b"!").decode(), eof=False,
    )))
    await ws.push_from_agent(encode_frame(HttpChunk(
        stream_id=sid, body_b64="", eof=True,
    )))

    chunks = await asyncio.wait_for(consumer_task, timeout=2.0)
    assert chunks[0].status == 200
    assert b"".join(c.body for c in chunks) == b"hi!"
    assert chunks[-1].eof is True

    link.shutdown()
    await ws.push_from_agent(None)
    await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_proxy_request_does_not_cancel_after_clean_eof():
    ws = _FakeWS()
    link = RemoteAgentLink(node_id=7, ws=ws)
    run_task = asyncio.create_task(link.run())

    async def consumer():
        async for chunk in link.proxy_request(
            container_id="cid", method="GET", path="/v1/models",
            headers={}, body=b"",
        ):
            if chunk.eof:
                break

    consumer_task = asyncio.create_task(consumer())

    sent = await ws.pop_to_agent()
    req = decode_frame(sent)
    assert isinstance(req, HttpRequest)

    await ws.push_from_agent(encode_frame(HttpChunk(
        stream_id=req.stream_id, body_b64="", eof=True, status=200, headers={},
    )))

    await asyncio.wait_for(consumer_task, timeout=2.0)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ws.pop_to_agent(), timeout=0.05)

    link.shutdown()
    await ws.push_from_agent(None)
    await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_proxy_request_sends_cancel_when_consumer_stops_early():
    ws = _FakeWS()
    link = RemoteAgentLink(node_id=7, ws=ws)
    run_task = asyncio.create_task(link.run())

    async def consumer():
        async for _ in link.proxy_request(
            container_id="cid", method="GET", path="/v1/models",
            headers={}, body=b"",
        ):
            break

    consumer_task = asyncio.create_task(consumer())

    sent = await ws.pop_to_agent()
    req = decode_frame(sent)
    assert isinstance(req, HttpRequest)
    await ws.push_from_agent(encode_frame(HttpChunk(
        stream_id=req.stream_id,
        body_b64=base64.b64encode(b"partial").decode(),
        eof=False,
        status=200,
        headers={},
    )))

    await asyncio.wait_for(consumer_task, timeout=2.0)
    cancel = decode_frame(await ws.pop_to_agent())
    assert isinstance(cancel, HttpCancel)
    assert cancel.stream_id == req.stream_id

    link.shutdown()
    await ws.push_from_agent(None)
    await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_unblocks_pending_ops():
    ws = _FakeWS()
    link = RemoteAgentLink(node_id=7, ws=ws)
    run_task = asyncio.create_task(link.run())

    caller_task = asyncio.create_task(link.start_deployment({}))
    # Wait until the frame is on the wire so the future is registered.
    await ws.pop_to_agent()

    link.shutdown()
    await ws.push_from_agent(None)
    await asyncio.gather(run_task, return_exceptions=True)

    with pytest.raises(ConnectionError):
        await asyncio.wait_for(caller_task, timeout=2.0)
