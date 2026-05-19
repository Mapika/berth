from __future__ import annotations

from serve_engine.routing.affinity import RoutingAffinity


def test_empty_lookup_returns_none():
    a = RoutingAffinity(capacity=4)
    assert a.lookup("sess-a") is None


def test_set_then_lookup():
    a = RoutingAffinity(capacity=4)
    a.set("sess-a", node_id=10)
    assert a.lookup("sess-a") == 10


def test_lru_eviction_when_full():
    a = RoutingAffinity(capacity=2)
    a.set("k1", node_id=10)
    a.set("k2", node_id=11)
    a.set("k3", node_id=12)
    assert a.lookup("k1") is None
    assert a.lookup("k2") == 11
    assert a.lookup("k3") == 12


def test_lookup_promotes_recency():
    a = RoutingAffinity(capacity=2)
    a.set("k1", node_id=10)
    a.set("k2", node_id=11)
    _ = a.lookup("k1")
    a.set("k3", node_id=12)
    assert a.lookup("k1") == 10
    assert a.lookup("k2") is None


def test_evict_node_drops_all_pointing_entries():
    a = RoutingAffinity(capacity=4)
    a.set("k1", node_id=10)
    a.set("k2", node_id=10)
    a.set("k3", node_id=11)
    a.evict_node(10)
    assert a.lookup("k1") is None
    assert a.lookup("k2") is None
    assert a.lookup("k3") == 11
