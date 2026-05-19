from __future__ import annotations

from unittest.mock import MagicMock

from serve_engine.cluster.leader_hub import LeaderHub
from serve_engine.cluster.protocol import Heartbeat
from serve_engine.daemon.metrics_aggregator import MetricsAggregator


def test_leader_hub_feeds_aggregator_on_heartbeat():
    agg = MetricsAggregator()
    hub = LeaderHub(
        conn=MagicMock(),
        registry=MagicMock(),
        fingerprint_resolver=lambda ws: "fp",
        aggregator=agg,
    )
    sample = {"gpus": [{"index": 0, "mem_used_mb": 0, "mem_total_mb": 0,
                        "util_pct": 42, "temp_c": 0}],
              "deployments": [], "node": {}}
    hub._handle_heartbeat(node_id=5, frame=Heartbeat(ts=100.0, metrics=sample))
    assert agg.snapshot()[5]["gpus"][0]["util_pct"] == 42


def test_leader_hub_heartbeat_without_metrics_is_a_noop():
    agg = MetricsAggregator()
    hub = LeaderHub(
        conn=MagicMock(), registry=MagicMock(),
        fingerprint_resolver=lambda ws: "fp", aggregator=agg,
    )
    hub._handle_heartbeat(node_id=5, frame=Heartbeat(ts=100.0, metrics=None))
    assert agg.snapshot() == {}


def test_leader_hub_constructed_without_aggregator_remains_functional():
    """Aggregator is optional — pre-existing call sites that don't pass
    one must keep working."""
    hub = LeaderHub(
        conn=MagicMock(), registry=MagicMock(),
        fingerprint_resolver=lambda ws: "fp",
    )
    hub._handle_heartbeat(node_id=5, frame=Heartbeat(ts=100.0, metrics={"gpus": []}))
    # No exception, no aggregator side effects to assert — just doesn't crash.
