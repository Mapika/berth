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
    link = _Stub(7)
    assert r.register(link) is None
    got = r.get(7)
    assert got is not None and got.node_id == 7
    assert {link.node_id for link in r.all()} == {7}
    assert r.unregister(link) is True
    assert r.get(7) is None


def test_get_missing_returns_none():
    r = AgentRegistry()
    assert r.get(99) is None


def test_register_returns_displaced_link_for_same_node():
    """Eviction-on-collision: when a second link claims the same node_id,
    the old link is returned so the caller can close its transport."""
    r = AgentRegistry()
    a = _Stub(7, ready=False)
    b = _Stub(7, ready=True)
    assert r.register(a) is None
    displaced = r.register(b)
    assert displaced is a
    assert r.get(7) is b


def test_unregister_only_pops_matching_link_identity():
    """Identity-checked unregister: a stale link's finally block must not
    pop a newer link that displaced it. Without this guard, link A's exit
    would set the node unreachable even though link B is still online."""
    r = AgentRegistry()
    a = _Stub(7)
    b = _Stub(7)
    r.register(a)
    r.register(b)  # displaces a in the registry
    # A's finally now runs and tries to unregister itself. It must be a no-op.
    assert r.unregister(a) is False
    assert r.get(7) is b
    # B's finally still cleans up correctly.
    assert r.unregister(b) is True
    assert r.get(7) is None


def test_all_returns_snapshot_not_live_view():
    r = AgentRegistry()
    r.register(_Stub(1))
    snap = r.all()
    r.register(_Stub(2))
    assert {link.node_id for link in snap} == {1}
