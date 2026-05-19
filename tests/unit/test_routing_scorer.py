from __future__ import annotations

from serve_engine.routing.scorer import (
    DeploymentCandidate,
    NodeSignals,
    RoutingRequest,
    default_scorer,
)


def _cand(*, deployment_id, node_id, model_required_mb=8000):
    return DeploymentCandidate(
        deployment_id=deployment_id,
        node_id=node_id,
        model_required_mb=model_required_mb,
    )


def _signals(*, node_id, mem_free_mb=20000, in_flight=0, p95=100):
    return NodeSignals(
        node_id=node_id,
        mem_free_mb=mem_free_mb,
        in_flight=in_flight,
        latency_p95_ms=p95,
    )


def test_scorer_returns_empty_when_no_candidates():
    out = default_scorer(
        candidates=[], signals_by_node={},
        request=RoutingRequest(affinity_key=None),
    )
    assert out == []


def test_scorer_drops_candidates_without_memory_headroom():
    candidates = [
        _cand(deployment_id=1, node_id=10, model_required_mb=8000),
        _cand(deployment_id=2, node_id=11, model_required_mb=8000),
    ]
    signals = {
        10: _signals(node_id=10, mem_free_mb=2000),
        11: _signals(node_id=11, mem_free_mb=20000),
    }
    out = default_scorer(
        candidates=candidates, signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    assert [c.deployment_id for c in out] == [2]


def test_scorer_ranks_lower_in_flight_first():
    candidates = [
        _cand(deployment_id=1, node_id=10),
        _cand(deployment_id=2, node_id=11),
    ]
    signals = {
        10: _signals(node_id=10, in_flight=8),
        11: _signals(node_id=11, in_flight=2),
    }
    out = default_scorer(
        candidates=candidates, signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    assert [c.deployment_id for c in out] == [2, 1]


def test_scorer_ranks_lower_p95_when_in_flight_is_tied():
    candidates = [
        _cand(deployment_id=1, node_id=10),
        _cand(deployment_id=2, node_id=11),
    ]
    signals = {
        10: _signals(node_id=10, in_flight=4, p95=500),
        11: _signals(node_id=11, in_flight=4, p95=120),
    }
    out = default_scorer(
        candidates=candidates, signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    assert [c.deployment_id for c in out] == [2, 1]


def test_scorer_prefers_affinity_hit_over_lower_load():
    candidates = [
        _cand(deployment_id=1, node_id=10),
        _cand(deployment_id=2, node_id=11),
    ]
    signals = {
        10: _signals(node_id=10, in_flight=8),
        11: _signals(node_id=11, in_flight=0),
    }
    out = default_scorer(
        candidates=candidates, signals_by_node=signals,
        request=RoutingRequest(affinity_key="sess-abc", affinity_node_id=10),
    )
    assert [c.deployment_id for c in out] == [1, 2]


def test_scorer_treats_missing_signals_as_worst_case():
    candidates = [
        _cand(deployment_id=1, node_id=10),
        _cand(deployment_id=2, node_id=11),
    ]
    signals = {10: _signals(node_id=10, in_flight=4)}
    out = default_scorer(
        candidates=candidates, signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    # node 11 has no signal → worst-case mem_free=0 → fails hard filter → dropped.
    # node 10 fits.
    assert [c.deployment_id for c in out] == [1]
