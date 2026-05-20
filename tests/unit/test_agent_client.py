from __future__ import annotations

import asyncio
import base64

import pytest

from berth.cluster.agent_client import AgentFrameDispatcher, SerializedSender, _DockerAdapter
from berth.cluster.protocol import (
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


class _RunResult:
    id = "cid-remote"
    address = "127.0.0.1"
    port = 32768


class _DockerRunStub:
    def __init__(self):
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return _RunResult()


@pytest.mark.asyncio
async def test_serialized_sender_prevents_concurrent_writes():
    active = False
    sent: list[str] = []

    async def raw_send(message: str) -> None:
        nonlocal active
        assert active is False
        active = True
        await asyncio.sleep(0.01)
        sent.append(message)
        active = False

    sender = SerializedSender(raw_send)

    await asyncio.gather(
        sender.send("heartbeat"),
        sender.send("http-chunk"),
        sender.send("log-chunk"),
    )

    assert sent == ["heartbeat", "http-chunk", "log-chunk"]


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
        headers={"x-berth-container-id": "cid-1"}, body_b64="",
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
        headers={"x-berth-container-id": "missing"}, body_b64="",
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
        headers={"x-berth-container-id": "cid-1"}, body_b64="",
    ))
    await asyncio.sleep(0.01)
    chunks = [decode_frame(s) for s in sent]
    last = chunks[-1]
    assert isinstance(last, HttpChunk)
    assert last.status == 502
    assert last.eof is True


@pytest.mark.asyncio
async def test_remote_docker_adapter_emits_status_and_quiets_download(
    tmp_path, monkeypatch,
):
    models_dir = tmp_path / "models"
    snapshot = models_dir / "snapshots" / "qwen"
    snapshot.mkdir(parents=True)
    events: list[tuple[str, dict]] = []
    captured_download: dict = {}

    def fake_download_model(**kwargs):
        captured_download.update(kwargs)
        return str(snapshot)

    monkeypatch.setattr(
        "berth.lifecycle.downloader.download_model",
        fake_download_model,
    )
    docker = _DockerRunStub()
    adapter = _DockerAdapter(
        docker,
        models_dir=models_dir,
        configs_dir=tmp_path / "configs",
        status_cb=lambda event, payload: events.append((event, payload)),
        quiet_downloads=True,
    )

    result = await adapter.start({
        "model_hf_repo": "Qwen/Qwen2.5-0.5B-Instruct",
        "model_revision": "main",
        "model_sentinel": "__MODEL__",
        "command": ["vllm", "serve", "__MODEL__"],
        "image": "vllm/vllm-openai:test",
        "name": "berth-vllm-qwen",
        "environment": {},
        "kwargs": {},
        "internal_port": 8000,
    })

    assert result == ("cid-remote", "127.0.0.1", 32768)
    assert captured_download["quiet"] is True
    assert docker.calls[0]["command"] == [
        "vllm", "serve", "/cache/snapshots/qwen",
    ]
    assert [event for event, _ in events] == [
        "deployment.download_started",
        "deployment.download_finished",
        "deployment.container_starting",
        "deployment.container_started",
    ]


def test_build_heartbeat_frame_without_collectors_is_bare():
    from berth.cluster.agent_client import build_heartbeat_frame
    from berth.cluster.protocol import Heartbeat

    frame = build_heartbeat_frame(
        in_flight=None, latency=None,
        deployment_models={}, uptime_s=0.0,
    )
    assert isinstance(frame, Heartbeat)
    assert frame.metrics is None


def test_build_heartbeat_frame_with_collectors_carries_metrics(monkeypatch):
    from berth.cluster import metrics_collector as mc
    from berth.cluster.agent_client import build_heartbeat_frame
    from berth.cluster.protocol import Heartbeat, decode_frame, encode_frame

    # Stub out NVML so the test doesn't depend on a GPU host.
    monkeypatch.setattr(mc, "read_gpu_stats", lambda: [])

    in_flight = mc.InFlightCounter()
    in_flight.start(7)
    latency = mc.LatencyRecorder()
    latency.record(deployment_id=7, latency_ms=120, error=False)

    frame = build_heartbeat_frame(
        in_flight=in_flight, latency=latency,
        deployment_models={7: "llama3-8b"}, uptime_s=10.0,
    )
    assert isinstance(frame, Heartbeat)
    assert frame.metrics is not None
    assert frame.metrics["deployments"][0]["deployment_id"] == 7
    assert frame.metrics["deployments"][0]["in_flight"] == 1
    assert frame.metrics["deployments"][0]["model_id"] == "llama3-8b"

    # Survives the wire format.
    decoded = decode_frame(encode_frame(frame))
    assert isinstance(decoded, Heartbeat)
    assert decoded.metrics["deployments"][0]["deployment_id"] == 7
