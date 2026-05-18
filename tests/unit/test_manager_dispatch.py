from __future__ import annotations

import pytest

from serve_engine.cluster.agent_link import StartedContainer
from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.lifecycle.manager import _dispatch_start, _dispatch_stop


class _FakeLink:
    def __init__(self, nid: int):
        self._nid = nid
        self.started: list[dict] = []
        self.stopped: list[str] = []

    @property
    def node_id(self) -> int:
        return self._nid

    @property
    def is_ready(self) -> bool:
        return True

    async def start_deployment(self, plan):
        self.started.append(plan)
        return StartedContainer(
            container_id=f"cid-{self._nid}", address="tunnel", port=0,
        )

    async def stop_deployment(self, cid, *, remove=True):
        self.stopped.append(cid)


class _UnreadyLink(_FakeLink):
    @property
    def is_ready(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_dispatch_start_calls_link_for_chosen_node():
    reg = AgentRegistry()
    link_a = _FakeLink(1)
    link_b = _FakeLink(2)
    reg.register(link_a)
    reg.register(link_b)
    plan = {"image": "x", "name": "d-1", "command": [], "environment": {},
            "kwargs": {}, "volumes": {}, "internal_port": 9000}
    started = await _dispatch_start(reg, node_id=2, plan=plan)
    assert started.container_id == "cid-2"
    assert link_b.started == [plan]
    assert link_a.started == []


@pytest.mark.asyncio
async def test_dispatch_start_raises_when_node_missing():
    reg = AgentRegistry()
    reg.register(_FakeLink(1))
    with pytest.raises(RuntimeError, match="not connected"):
        await _dispatch_start(reg, node_id=99, plan={})


@pytest.mark.asyncio
async def test_dispatch_start_raises_when_node_not_ready():
    reg = AgentRegistry()
    reg.register(_UnreadyLink(1))
    with pytest.raises(RuntimeError, match="not connected"):
        await _dispatch_start(reg, node_id=1, plan={})


@pytest.mark.asyncio
async def test_dispatch_stop_routes_to_node():
    reg = AgentRegistry()
    link = _FakeLink(7)
    reg.register(link)
    await _dispatch_stop(reg, node_id=7, container_id="cid-7")
    assert link.stopped == ["cid-7"]


@pytest.mark.asyncio
async def test_dispatch_stop_raises_when_node_missing():
    reg = AgentRegistry()
    with pytest.raises(RuntimeError, match="not connected"):
        await _dispatch_stop(reg, node_id=42, container_id="cid")
