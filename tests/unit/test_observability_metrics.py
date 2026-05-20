from __future__ import annotations

from berth.daemon.metrics_aggregator import MetricsAggregator
from berth.observability.metrics import format_cluster_metrics


def _sample():
    return {
        "gpus": [
            {"index": 0, "mem_used_mb": 1024, "mem_total_mb": 81920,
             "util_pct": 42, "temp_c": 0},
            {"index": 1, "mem_used_mb": 0, "mem_total_mb": 81920,
             "util_pct": 0, "temp_c": 0},
        ],
        "deployments": [
            {"deployment_id": 7, "model_id": "llama3-8b", "in_flight": 3,
             "requests_last_window": 12, "latency_p50_ms": 100,
             "latency_p95_ms": 450, "errors_last_window": 1},
        ],
        "node": {},
    }


def test_format_cluster_metrics_emits_gpu_gauges():
    a = MetricsAggregator()
    a.ingest(node_id=1, sample=_sample(), ts=0.0)
    out = format_cluster_metrics(a, node_labels={1: "worker-a"})
    assert 'serve_node_gpu_util_pct{node="worker-a",gpu="0"} 42' in out
    assert 'serve_node_gpu_mem_used_bytes{node="worker-a",gpu="0"} 1073741824' in out


def test_format_cluster_metrics_emits_deployment_gauges():
    a = MetricsAggregator()
    a.ingest(node_id=1, sample=_sample(), ts=0.0)
    out = format_cluster_metrics(a, node_labels={1: "worker-a"})
    assert (
        'serve_deployment_in_flight{node="worker-a",deployment="7",model="llama3-8b"} 3'
        in out
    )
    assert (
        'serve_deployment_errors_total{node="worker-a",deployment="7",model="llama3-8b"} 1'
        in out
    )


def test_format_cluster_metrics_empty_aggregator_is_empty_string():
    out = format_cluster_metrics(MetricsAggregator(), node_labels={})
    assert out == ""


def test_format_cluster_metrics_falls_back_when_label_missing():
    a = MetricsAggregator()
    a.ingest(node_id=99, sample=_sample(), ts=0.0)
    out = format_cluster_metrics(a, node_labels={})
    assert 'node="99"' in out


def test_format_cluster_metrics_escapes_prometheus_label_values():
    a = MetricsAggregator()
    a.ingest(
        node_id=1,
        sample={
            "gpus": [{"index": '0"\nserve_api_keys_active 999\nx="', "util_pct": 1}],
            "deployments": [
                {
                    "deployment_id": '7" ,bad="1',
                    "model_id": 'model"\\\nserve_models_total 999',
                    "in_flight": 1,
                },
            ],
        },
        ts=0.0,
    )

    out = format_cluster_metrics(
        a,
        node_labels={1: 'worker"\\\nserve_proxy_requests_total 999'},
    )

    assert "\nserve_api_keys_active 999\n" not in out
    assert "\nserve_models_total 999" not in out
    assert "\nserve_proxy_requests_total 999" not in out
    assert 'node="worker\\"\\\\\\nserve_proxy_requests_total 999"' in out
    assert 'gpu="0\\"\\nserve_api_keys_active 999\\nx=\\""' in out
    assert 'deployment="7\\" ,bad=\\"1"' in out
    assert 'model="model\\"\\\\\\nserve_models_total 999"' in out


def test_format_cluster_metrics_treats_malformed_agent_numbers_as_zero():
    a = MetricsAggregator()
    a.ingest(
        node_id=1,
        sample={
            "gpus": [
                {
                    "index": 0,
                    "util_pct": "busy",
                    "mem_used_mb": "-10",
                },
            ],
            "deployments": [
                {
                    "deployment_id": 7,
                    "model_id": "llama3-8b",
                    "in_flight": "many",
                    "requests_last_window": None,
                    "latency_p50_ms": "-1",
                    "latency_p95_ms": {},
                    "errors_last_window": True,
                },
            ],
        },
        ts=0.0,
    )

    out = format_cluster_metrics(a, node_labels={1: "worker-a"})

    assert 'serve_node_gpu_util_pct{node="worker-a",gpu="0"} 0' in out
    assert 'serve_node_gpu_mem_used_bytes{node="worker-a",gpu="0"} 0' in out
    assert 'serve_deployment_in_flight{node="worker-a",deployment="7",model="llama3-8b"} 0' in out
    assert (
        'serve_deployment_latency_p95_ms{node="worker-a",deployment="7",model="llama3-8b"} 0'
        in out
    )
