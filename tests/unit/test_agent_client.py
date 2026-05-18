from __future__ import annotations

import asyncio
import base64

import pytest

from serve_engine.cluster.agent_client import AgentFrameDispatcher
from serve_engine.cluster.protocol import (
    HttpChunk,
    HttpRequest,
    OpResult,
    StartDeployment,
    StopDeployment,
    decode_frame,
)


class _DockerStub:
    def __init__(self):
        self.started: list[dict] = []
        self.stopped: list[str] = []

    async def start(self, plan):
        self.started.append(plan)
        return ("cid-1", "127.0.0.1", 9000)

    async def stop(self, cid, *, remove):
        self.stopped.append(cid)


class _HttpOk:
    async def stream(self, method, url, headers, body):
        async def gen():
            yield (200, {"content-type": "text/plain"}, b"hi", False)
            yield (None, None, b"!", False)
            yield (None, None, b"", True)
        return gen()


class _HttpFail:
    async def stream(self, method, url, headers, body):
        raise RuntimeError("connection refused")


@pytest.mark.asyncio
async def test_start_then_op_result():
    sent: list[str] = []

    async def sender(s):
        sent.append(s)

    disp = AgentFrameDispatcher(docker=_DockerStub(), http=_HttpOk(), send=sender)
    await disp.handle(StartDeployment(request_id="r1", plan={"image": "x"}))
    assert any(isinstance(decode_frame(s), OpResult) for s in sent)
    res = decode_frame(sent[-1])
    assert isinstance(res, OpResult)
    assert res.ok is True
    assert res.data["container_id"] == "cid-1"


@pytest.mark.asyncio
async def test_start_failure_returns_op_result_with_error():
    sent: list[str] = []

    async def sender(s):
        sent.append(s)

    class _DockerFails:
        async def start(self, plan):
            raise RuntimeError("boom")
        async def stop(self, cid, *, remove): pass

    disp = AgentFrameDispatcher(
        docker=_DockerFails(), http=_HttpOk(), send=sender,
    )
    await disp.handle(StartDeployment(request_id="r2", plan={}))
    res = decode_frame(sent[-1])
    assert isinstance(res, OpResult)
    assert res.ok is False
    assert "boom" in (res.error or "")


@pytest.mark.asyncio
async def test_stop_calls_docker_and_returns_ok():
    sent: list[str] = []

    async def sender(s):
        sent.append(s)

    docker = _DockerStub()
    disp = AgentFrameDispatcher(docker=docker, http=_HttpOk(), send=sender)
    await disp.handle(StopDeployment(request_id="r3", container_id="cid-X"))
    res = decode_frame(sent[-1])
    assert isinstance(res, OpResult)
    assert res.ok is True
    assert docker.stopped == ["cid-X"]


@pytest.mark.asyncio
async def test_http_request_streams_chunks_back():
    sent: list[str] = []

    async def sender(s):
        sent.append(s)

    disp = AgentFrameDispatcher(docker=_DockerStub(), http=_HttpOk(), send=sender)
    disp.register_endpoint(container_id="cid-1", address="127.0.0.1", port=9000)
    await disp.handle(HttpRequest(
        stream_id="s1", method="GET", path="/",
        headers={"x-serve-container-id": "cid-1"}, body_b64="",
    ))
    # Allow the inflight task to drain
    await asyncio.sleep(0.01)
    chunks = [decode_frame(s) for s in sent]
    assert any(isinstance(c, HttpChunk) and c.eof for c in chunks)
    body = b"".join(
        base64.b64decode(c.body_b64) for c in chunks
        if isinstance(c, HttpChunk) and c.body_b64
    )
    assert b"hi!" in body
    # The first chunk carries status/headers
    first = next(c for c in chunks if isinstance(c, HttpChunk))
    assert first.status == 200
    assert first.headers["content-type"] == "text/plain"


@pytest.mark.asyncio
async def test_http_request_with_unknown_container_returns_502():
    sent: list[str] = []

    async def sender(s):
        sent.append(s)

    disp = AgentFrameDispatcher(docker=_DockerStub(), http=_HttpOk(), send=sender)
    await disp.handle(HttpRequest(
        stream_id="sX", method="GET", path="/",
        headers={"x-serve-container-id": "missing"}, body_b64="",
    ))
    await asyncio.sleep(0.01)
    chunks = [decode_frame(s) for s in sent]
    assert any(isinstance(c, HttpChunk) and c.status == 502 for c in chunks)


@pytest.mark.asyncio
async def test_http_request_with_engine_failure_returns_502():
    sent: list[str] = []

    async def sender(s):
        sent.append(s)

    disp = AgentFrameDispatcher(
        docker=_DockerStub(), http=_HttpFail(), send=sender,
    )
    disp.register_endpoint(container_id="cid-1", address="127.0.0.1", port=9000)
    await disp.handle(HttpRequest(
        stream_id="sFail", method="GET", path="/",
        headers={"x-serve-container-id": "cid-1"}, body_b64="",
    ))
    await asyncio.sleep(0.01)
    chunks = [decode_frame(s) for s in sent]
    last = chunks[-1]
    assert isinstance(last, HttpChunk)
    assert last.status == 502
    assert last.eof is True
