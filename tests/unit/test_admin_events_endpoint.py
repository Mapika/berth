import asyncio
import json

import pytest

from berth.daemon.app import build_app
from berth.observability.events import Event
from berth.store import db


@pytest.fixture
def app_with_bus(tmp_path):
    from unittest.mock import MagicMock

    from berth.backends.vllm import VLLMBackend
    from berth.lifecycle.docker_client import ContainerHandle
    from berth.lifecycle.topology import GPUInfo, Topology

    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    docker_client = MagicMock()
    docker_client.run.return_value = ContainerHandle(
        id="cid", name="x", address="127.0.0.1", port=49152
    )
    topology = Topology(
        gpus=[GPUInfo(index=0, name="H100", total_mb=80 * 1024)],
        _islands={0: frozenset({0})},
    )
    return build_app(
        conn=conn,
        docker_client=docker_client,
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        topology=topology,
    )


@pytest.mark.asyncio
async def test_events_endpoint_streams_published_events(app_with_bus):
    """Test that the SSE generator yields the initial heartbeat and event data."""
    bus = app_with_bus.state.event_bus

    # Collect chunks from the SSE generator directly, stopping after we see the event.
    collected: list[str] = []

    async def run_generator():
        async with bus.subscribe() as queue:
            # initial heartbeat
            collected.append(":ok\n\n")
            # wait for one event with short timeout
            try:
                e = await asyncio.wait_for(queue.get(), timeout=2.0)
                payload = json.dumps({"kind": e.kind, "payload": e.payload, "ts": e.ts})
                collected.append(f"data: {payload}\n\n")
            except TimeoutError:
                pass

    gen_task = asyncio.create_task(run_generator())
    await asyncio.sleep(0.05)
    await bus.publish(Event(kind="test.fired", payload={"x": 1}))
    await asyncio.wait_for(gen_task, timeout=3.0)

    assert any("test.fired" in chunk for chunk in collected)


async def _take_chunks(response, n: int, per_chunk_s: float = 1.5) -> str:
    """Pull up to n chunks from a StreamingResponse.body_iterator with a
    per-chunk timeout. Closes the iterator afterward. Avoids the ASGI
    cleanup hangs that come from streaming forever-running SSE handlers
    via httpx.ASGITransport."""
    aiter = response.body_iterator.__aiter__()
    chunks: list[str] = []
    for _ in range(n):
        try:
            raw = await asyncio.wait_for(aiter.__anext__(), timeout=per_chunk_s)
        except (TimeoutError, StopAsyncIteration):
            break
        if isinstance(raw, bytes):
            chunks.append(raw.decode("utf-8", errors="replace"))
        else:
            chunks.append(str(raw))
    if hasattr(aiter, "aclose"):
        await aiter.aclose()
    return "".join(chunks)


@pytest.mark.asyncio
async def test_events_handler_returns_streaming_response(app_with_bus):
    """Regression: invoking admin.events() must return a real StreamingResponse.

    The previous test exercises the bus subscribe/publish logic but never
    invokes admin.events() itself. That gap let `_SSE` (an undefined symbol
    at admin.py:395) ship to production - `/admin/events` raised NameError
    on first request, breaking the dashboard event feed. This test calls
    the handler directly and pulls chunks from the body iterator, so any
    name-resolution or import bug surfaces here.
    """
    from unittest.mock import MagicMock

    from fastapi.responses import StreamingResponse

    from berth.daemon.admin import events

    request = MagicMock()
    request.app.state.event_bus = app_with_bus.state.event_bus

    response = await events(request)
    assert isinstance(response, StreamingResponse)
    assert response.media_type == "text/event-stream"

    bus = app_with_bus.state.event_bus
    aiter = response.body_iterator.__aiter__()
    # First yield is the initial `:ok` heartbeat - pulling it ensures the
    # bus subscription is registered before we publish.
    head = await asyncio.wait_for(aiter.__anext__(), timeout=1.5)
    assert ":ok" in (head.decode() if isinstance(head, bytes) else head)

    await bus.publish(Event(kind="test.fired", payload={"a": 1}))
    blob = ""
    for _ in range(5):
        try:
            chunk = await asyncio.wait_for(aiter.__anext__(), timeout=1.5)
        except (TimeoutError, StopAsyncIteration):
            break
        blob += chunk.decode() if isinstance(chunk, bytes) else chunk
        if "test.fired" in blob:
            break
    if hasattr(aiter, "aclose"):
        await aiter.aclose()
    assert "test.fired" in blob, f"event not delivered; got {blob!r}"


@pytest.mark.asyncio
async def test_engine_logs_handler_returns_streaming_response(tmp_path):
    """Regression: /admin/deployments/{id}/logs/stream - same coverage gap.

    The dashboard's engine-logs page (commit 1eb0f3c) hits this endpoint;
    it would have crashed with NameError under the same `_SSE` bug.
    """
    from unittest.mock import MagicMock

    from fastapi.responses import StreamingResponse

    from berth.daemon.admin import stream_engine_logs_sse
    from berth.store import deployments as dep_store
    from berth.store import models as model_store

    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)

    m = model_store.add(conn, name="m", hf_repo="x/y", revision="main")
    dep = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
        pinned=False, idle_timeout_s=None, vram_reserved_mb=1000,
    )
    dep_store.set_container(
        conn, dep.id,
        container_id="cid", container_name="x",
        container_port=49152, container_address="127.0.0.1",
    )

    docker_client = MagicMock()
    docker_client.stream_logs.return_value = iter([
        "first log line\n",
        "second log line\n",
    ])

    request = MagicMock()
    request.app.state.conn = conn
    request.app.state.manager._docker = docker_client

    response = await stream_engine_logs_sse(dep.id, request)
    assert isinstance(response, StreamingResponse)
    assert response.media_type == "text/event-stream"

    blob = await _take_chunks(response, n=10)
    assert ":ok" in blob
    assert "first log line" in blob
    assert "second log line" in blob
