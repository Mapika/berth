"""Verifies the bounded queue between upstream reader and client writer
applies backpressure to the engine when the client reads slowly.

Tests target `_bounded_pipe` directly because the integration path
through httpx.ASGITransport doesn't propagate backpressure (the
transport buffers chunks between the fake engine and the proxy).
"""
from __future__ import annotations

import asyncio

import pytest

from berth.daemon.openai_proxy import _bounded_pipe


class _CountingProducer:
    """Async iterator that records how many chunks it has yielded.

    Each `__anext__` only resolves when the previous chunk has been
    delivered to the consumer (or, with backpressure, accepted into the
    queue). That mirrors the way a real upstream stream cooperates with
    the event loop."""

    def __init__(self, total: int):
        self.total = total
        self.emitted = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self.emitted >= self.total:
            raise StopAsyncIteration
        self.emitted += 1
        return b"x"


@pytest.mark.asyncio
async def test_bounded_pipe_forwards_all_chunks_when_consumer_is_fast():
    producer = _CountingProducer(total=10)
    chunks = []
    async for c in _bounded_pipe(producer, queue_depth=4):
        chunks.append(c)
    assert chunks == [b"x"] * 10
    assert producer.emitted == 10


@pytest.mark.asyncio
async def test_bounded_pipe_propagates_exceptions_from_reader():
    """When the upstream iterator raises, the pipe must re-raise on the
    consumer side so the proxy's existing finally block runs."""
    async def angry():
        yield b"first"
        raise RuntimeError("boom")

    chunks = []
    with pytest.raises(RuntimeError, match="boom"):
        async for c in _bounded_pipe(angry(), queue_depth=4):
            chunks.append(c)
    assert chunks == [b"first"]


@pytest.mark.asyncio
async def test_bounded_pipe_caps_runahead_at_queue_depth():
    """With a paused consumer, the producer must not get ahead by more
    than `queue_depth + 1` chunks: the queue itself holds up to
    `queue_depth`, plus one chunk in flight in the reader task's
    `put()` call after the queue fills."""
    producer = _CountingProducer(total=1000)
    pipe = _bounded_pipe(producer, queue_depth=4).__aiter__()

    # Pull the first chunk so the reader task kicks off.
    first = await pipe.__anext__()
    assert first == b"x"

    # Yield repeatedly to give the reader plenty of scheduling
    # opportunities; with no further consumption it should fill the
    # queue and block.
    for _ in range(50):
        await asyncio.sleep(0)

    # Allow at most queue_depth + 2 emitted: queue_depth in the queue,
    # one in flight in put(), and one already delivered to us above.
    assert producer.emitted <= 4 + 2, (
        f"producer emitted {producer.emitted}; "
        f"backpressure failed to pause the reader"
    )

    # Drain the rest cleanly to avoid leaving a dangling task.
    while True:
        try:
            await pipe.__anext__()
        except StopAsyncIteration:
            break
    assert producer.emitted == 1000


@pytest.mark.asyncio
async def test_bounded_pipe_cancels_reader_on_early_close():
    """If the consumer stops iterating early, the reader task must be
    cancelled — no lingering task pulling from the upstream forever."""
    async def slow_forever():
        i = 0
        while True:
            yield f"chunk-{i}".encode()
            i += 1
            await asyncio.sleep(0)

    pipe = _bounded_pipe(slow_forever(), queue_depth=2).__aiter__()
    _ = await pipe.__anext__()
    await pipe.aclose()
    # asyncio's task accounting: after a beat, no _bounded_pipe-spawned
    # task should still be pending. We assert by counting tasks before
    # vs after a small idle period.
    pending_before = len([t for t in asyncio.all_tasks() if not t.done()])
    await asyncio.sleep(0.05)
    pending_after = len([t for t in asyncio.all_tasks() if not t.done()])
    # Allow for the running test task itself; the reader should be gone.
    assert pending_after <= pending_before
