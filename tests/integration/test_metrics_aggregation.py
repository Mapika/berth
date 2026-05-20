from __future__ import annotations

from unittest.mock import MagicMock

from berth.cluster.leader_hub import LeaderHub
from berth.cluster.protocol import Heartbeat
from berth.daemon.admin import render_metrics_snapshot
from berth.daemon.metrics_aggregator import MetricsAggregator
from berth.observability.metrics import format_cluster_metrics


def _sample(util):
    return {
        "gpus": [{"index": 0, "mem_used_mb": 1024, "mem_total_mb": 81920,
                  "util_pct": util, "temp_c": 0}],
        "deployments": [{"deployment_id": 7, "model_id": "llama3-8b",
                         "in_flight": 2, "requests_last_window": 5,
                         "latency_p50_ms": 80, "latency_p95_ms": 120,
                         "errors_last_window": 0}],
        "node": {},
    }


def _node(node_id, label):
    n = MagicMock()
    n.id = node_id
    n.label = label
    return n


def test_two_agents_to_one_aggregator_through_hub_handler():
    """End-to-end smoke: heartbeats from two simulated agents land in
    one aggregator and surface through both the Prometheus exposition
    and the admin snapshot. Drives _handle_heartbeat directly instead
    of running a real WS — fast and deterministic."""
    agg = MetricsAggregator()
    hub = LeaderHub(
        conn=MagicMock(), registry=MagicMock(),
        fingerprint_resolver=lambda ws: "fp", aggregator=agg,
    )
    hub._handle_heartbeat(node_id=1, frame=Heartbeat(ts=10.0, metrics=_sample(20)))
    hub._handle_heartbeat(node_id=1, frame=Heartbeat(ts=15.0, metrics=_sample(30)))
    hub._handle_heartbeat(node_id=2, frame=Heartbeat(ts=11.0, metrics=_sample(50)))

    snap = agg.snapshot()
    assert set(snap.keys()) == {1, 2}
    assert snap[1]["gpus"][0]["util_pct"] == 30
    assert snap[2]["gpus"][0]["util_pct"] == 50

    prom = format_cluster_metrics(agg, node_labels={1: "a", 2: "b"})
    assert 'berth_node_gpu_util_pct{node="a",gpu="0"} 30' in prom
    assert 'berth_node_gpu_util_pct{node="b",gpu="0"} 50' in prom
    # Deployment-level series labelled correctly per node.
    assert 'berth_deployment_in_flight{node="a",deployment="7",model="llama3-8b"} 2' in prom
    assert 'berth_deployment_in_flight{node="b",deployment="7",model="llama3-8b"} 2' in prom

    body = render_metrics_snapshot(agg, nodes=[_node(1, "a"), _node(2, "b")])
    assert len(body["nodes"]) == 2
    assert body["nodes"][0]["series"]["gpu_util_pct"]["gpu0"] == [20, 30]


def test_node_disconnect_evicts_aggregator_entry():
    """When LeaderHub drops a node via its finally block, the aggregator
    entry must go too. We exercise the eviction path directly since the
    full WS-disconnect flow needs a real connection."""
    agg = MetricsAggregator()
    hub = LeaderHub(
        conn=MagicMock(), registry=MagicMock(),
        fingerprint_resolver=lambda ws: "fp", aggregator=agg,
    )
    hub._handle_heartbeat(node_id=1, frame=Heartbeat(ts=10.0, metrics=_sample(20)))
    assert agg.snapshot() != {}
    agg.drop_node(1)
    assert agg.snapshot() == {}
