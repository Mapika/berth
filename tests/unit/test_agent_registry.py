from __future__ import annotations

from berth.cluster.agent_registry import AgentRegistry


class _Stub:
    def __init__(self, nid: int, ready: bool = True):
        self._nid = nid
        self._ready = ready

    @property
    def node_id(self) -> int:
        return self._nid

    @property
    def is_ready(self) -> bool:
        return self._ready


def test_register_get_unregister():
    r = AgentRegistry()
    r.register(_Stub(7))
    got = r.get(7)
    assert got is not None and got.node_id == 7
    assert {link.node_id for link in r.all()} == {7}
    r.unregister(7)
    assert r.get(7) is None


def test_get_missing_returns_none():
    r = AgentRegistry()
    assert r.get(99) is None


def test_register_replaces_existing_link_for_same_node():
    r = AgentRegistry()
    a = _Stub(7, ready=False)
    b = _Stub(7, ready=True)
    r.register(a)
    r.register(b)
    got = r.get(7)
    assert got is b


def test_all_returns_snapshot_not_live_view():
    r = AgentRegistry()
    r.register(_Stub(1))
    snap = r.all()
    r.register(_Stub(2))
    assert {link.node_id for link in snap} == {1}
