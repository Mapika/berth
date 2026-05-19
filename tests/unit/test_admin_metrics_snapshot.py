from __future__ import annotations

from unittest.mock import MagicMock

from serve_engine.daemon.admin import render_metrics_snapshot
from serve_engine.daemon.metrics_aggregator import MetricsAggregator


def _sample(util=42, in_flight=3):
    return {
        "gpus": [{"index": 0, "mem_used_mb": 1024, "mem_total_mb": 81920,
                  "util_pct": util, "temp_c": 0}],
        "deployments": [{"deployment_id": 7, "model_id": "llama3-8b",
                         "in_flight": in_flight, "requests_last_window": 5,
                         "latency_p50_ms": 80, "latency_p95_ms": 120,
                         "errors_last_window": 0}],
        "node": {},
    }


def _node(node_id, label):
    n = MagicMock()
    n.id = node_id
    n.label = label
    return n


def test_snapshot_returns_known_nodes():
    a = MetricsAggregator()
    a.ingest(node_id=1, sample=_sample(util=10), ts=10.0)
    a.ingest(node_id=1, sample=_sample(util=20), ts=20.0)

    body = render_metrics_snapshot(a, nodes=[_node(1, "worker-a")])
    assert len(body["nodes"]) == 1
    n = body["nodes"][0]
    assert n["node_id"] == 1
    assert n["label"] == "worker-a"


def test_snapshot_includes_gpu_and_deployment_state():
    a = MetricsAggregator()
    a.ingest(node_id=1, sample=_sample(util=10), ts=10.0)
    a.ingest(node_id=1, sample=_sample(util=20), ts=20.0)
    body = render_metrics_snapshot(a, nodes=[_node(1, "worker-a")])
    n = body["nodes"][0]
    assert n["gpus"][0]["util_pct"] == 20
    assert n["deployments"][0]["in_flight"] == 3


def test_snapshot_includes_series_for_sparklines():
    a = MetricsAggregator()
    a.ingest(node_id=1, sample=_sample(util=10), ts=10.0)
    a.ingest(node_id=1, sample=_sample(util=20), ts=20.0)
    body = render_metrics_snapshot(a, nodes=[_node(1, "worker-a")])
    n = body["nodes"][0]
    assert n["series"]["gpu_util_pct"]["gpu0"] == [10, 20]
    assert n["series"]["request_rate"] == [5, 5]


def test_snapshot_empty_when_aggregator_empty():
    body = render_metrics_snapshot(MetricsAggregator(), nodes=[])
    assert body == {"nodes": []}


def test_snapshot_falls_back_to_numeric_when_label_missing():
    a = MetricsAggregator()
    a.ingest(node_id=99, sample=_sample(), ts=10.0)
    body = render_metrics_snapshot(a, nodes=[])
    assert body["nodes"][0]["label"] == "99"
