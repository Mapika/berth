from __future__ import annotations

import pytest

from berth.cluster.agent_registry import AgentRegistry
from berth.daemon.openai_proxy import _proxy_via_link
from berth.lifecycle.adapter_router import _filter_by_reachable_nodes


class _ReadyLink:
    def __init__(self, nid: int):
        self._nid = nid
    @property
    def node_id(self) -> int: return self._nid
    @property
    def is_ready(self) -> bool: return True


class _UnreadyLink:
    def __init__(self, nid: int):
        self._nid = nid
    @property
    def node_id(self) -> int: return self._nid
    @property
    def is_ready(self) -> bool: return False


def _dep(dep_id: int, node_id: int):
    """Minimal stand-in for a Deployment row — only the fields the filter reads."""
    return type("Dep", (), {"id": dep_id, "node_id": node_id})()


def test_filter_keeps_only_reachable():
    reg = AgentRegistry()
    reg.register(_ReadyLink(1))
    candidates = [_dep(10, 1), _dep(11, 2), _dep(12, 1)]
    filtered = _filter_by_reachable_nodes(candidates, reg)
    assert [d.id for d in filtered] == [10, 12]


def test_filter_drops_unready_links():
    reg = AgentRegistry()
    reg.register(_UnreadyLink(1))
    candidates = [_dep(10, 1)]
    assert _filter_by_reachable_nodes(candidates, reg) == []


def test_filter_passthrough_when_no_registry():
    candidates = [_dep(10, 1), _dep(11, 2)]
    assert _filter_by_reachable_nodes(candidates, None) == candidates


@pytest.mark.asyncio
async def test_proxy_via_link_503_when_node_missing():
    reg = AgentRegistry()
    with pytest.raises(RuntimeError, match="not connected"):
        await _proxy_via_link(
            registry=reg, node_id=99, container_id="cid",
            method="GET", path="/v1/models", headers={}, body=b"",
        )
