from __future__ import annotations

from serve_engine.daemon.metrics_aggregator import MetricsAggregator


def _sample(util=10, in_flight=0, p95=100):
    return {
        "gpus": [{"index": 0, "mem_used_mb": 1024, "mem_total_mb": 81920,
                  "util_pct": util, "temp_c": 0}],
        "deployments": [{"deployment_id": 7, "model_id": "llama3-8b",
                         "in_flight": in_flight, "requests_last_window": 5,
                         "latency_p50_ms": 80, "latency_p95_ms": p95,
                         "errors_last_window": 0}],
        "node": {"uptime_s": 1.0, "host_load_avg_1m": 0.0},
    }


def test_aggregator_starts_empty():
    a = MetricsAggregator()
    assert a.snapshot() == {}


def test_aggregator_records_per_node_samples():
    a = MetricsAggregator()
    a.ingest(node_id=1, sample=_sample(util=10), ts=100.0)
    a.ingest(node_id=2, sample=_sample(util=20), ts=100.0)
    snap = a.snapshot()
    assert set(snap.keys()) == {1, 2}
    assert snap[1]["gpus"][0]["util_pct"] == 10


def test_aggregator_keeps_only_last_12_samples_per_node():
    a = MetricsAggregator(window=12)
    for i in range(20):
        a.ingest(node_id=1, sample=_sample(util=i), ts=float(i))
    series = a.series(node_id=1, key="gpu_util_pct", gpu=0)
    assert len(series) == 12
    assert series[0] == 8
    assert series[-1] == 19


def test_aggregator_drop_node_evicts():
    a = MetricsAggregator()
    a.ingest(node_id=1, sample=_sample(util=10), ts=100.0)
    a.drop_node(1)
    assert a.snapshot() == {}


def test_aggregator_series_for_missing_node_returns_empty():
    a = MetricsAggregator()
    assert a.series(node_id=99, key="gpu_util_pct", gpu=0) == []


def test_aggregator_query_in_flight_for_deployment():
    a = MetricsAggregator()
    a.ingest(node_id=1, sample=_sample(in_flight=3), ts=100.0)
    assert a.deployment_in_flight(node_id=1, deployment_id=7) == 3
    assert a.deployment_in_flight(node_id=1, deployment_id=99) == 0


def test_aggregator_thread_safety_under_concurrent_ingest():
    import threading
    a = MetricsAggregator(window=12)

    def writer():
        for i in range(500):
            a.ingest(node_id=1, sample=_sample(util=i), ts=float(i))

    def reader():
        for _ in range(500):
            _ = a.snapshot()

    t1, t2 = threading.Thread(target=writer), threading.Thread(target=reader)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert len(a.series(node_id=1, key="gpu_util_pct", gpu=0)) == 12
