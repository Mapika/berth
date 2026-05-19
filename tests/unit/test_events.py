import asyncio

import pytest

from berth.observability.events import Event, EventBus


@pytest.mark.asyncio
async def test_subscribe_receives_published():
    bus = EventBus()
    received: list[Event] = []
    async with bus.subscribe() as queue:
        await bus.publish(Event(kind="load.started", payload={"dep_id": 1}))
        e = await asyncio.wait_for(queue.get(), timeout=1.0)
        received.append(e)
    assert received[0].kind == "load.started"
    assert received[0].payload == {"dep_id": 1}


@pytest.mark.asyncio
async def test_multiple_subscribers_each_receive():
    bus = EventBus()
    async with bus.subscribe() as q1, bus.subscribe() as q2:
        await bus.publish(Event(kind="x", payload={}))
        e1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        e2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    assert e1.kind == "x" and e2.kind == "x"


@pytest.mark.asyncio
async def test_subscriber_unsubscribes_on_exit():
    bus = EventBus()
    async with bus.subscribe() as _:
        assert bus.subscriber_count() == 1
    assert bus.subscriber_count() == 0
