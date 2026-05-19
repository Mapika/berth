from __future__ import annotations

import asyncio
import json

import pytest

from berth.observability.metrics import gather_engine_metrics
from berth.observability.trtllm_metrics import (
    translate_many,
    translate_trtllm_metrics,
)

# Sample iteration object as returned by trtllm-serve /metrics with
# enable_iter_perf_stats: true. Keep this in sync with the docstring example
# in the translator module.
_SAMPLE = [{
    "cpuMemUsage": 0,
    "gpuMemUsage": 58941767680,
    "iter": 1,
    "iterLatencyMS": 67526.06844902039,
    "maxBatchSizeRuntime": 0,
    "maxNumActiveRequests": 73,
    "inflightBatchingStats": {
        "avgNumDecodedTokensPerIter": 0.0,
        "microBatchId": 0,
        "numContextRequests": 1,
        "numCtxKvTokens": 0,
        "numCtxTokens": 13,
        "numGenKvTokens": 0,
        "numGenRequests": 0,
        "numPausedKvTokens": 0,
        "numPausedRequests": 0,
        "numQueuedContextRequests": 0,
        "numQueuedCtxTokens": 0,
        "numQueuedGenKvTokens": 0,
        "numQueuedGenRequests": 0,
        "numScheduledRequests": 1,
    },
    "kvCacheStats": {
        "allocNewBlocks": 1632,
        "allocTotalBlocks": 1632,
        "cacheHitRate": 0.0,
        "freeNumBlocks": 5350,
        "maxNumBlocks": 5358,
        "missedBlocks": 8,
        "reusedBlocks": 0,
        "tokensPerBlock": 32,
        "usedNumBlocks": 8,
    },
}]


def test_translate_sample_emits_expected_metrics():
    text = translate_trtllm_metrics(json.dumps(_SAMPLE), deployment_id=42)
    # Top-level metrics
    assert 'trtllm_gpu_memory_bytes{deployment_id="42"} 58941767680' in text
    assert 'trtllm_max_num_active_requests{deployment_id="42"} 73' in text
    # Float renders without losing precision
    assert 'trtllm_iter_latency_ms{deployment_id="42"} 67526.06844902039' in text
    # Inflight batching
    assert 'trtllm_inflight_num_context_requests{deployment_id="42"} 1' in text
    assert 'trtllm_inflight_num_scheduled_requests{deployment_id="42"} 1' in text
    assert 'trtllm_inflight_num_ctx_tokens{deployment_id="42"} 13' in text
    assert 'trtllm_inflight_avg_decoded_tokens_per_iter{deployment_id="42"} 0.0' in text
    # KV cache
    assert 'trtllm_kv_cache_max_blocks{deployment_id="42"} 5358' in text
    assert 'trtllm_kv_cache_used_blocks{deployment_id="42"} 8' in text
    assert 'trtllm_kv_cache_free_blocks{deployment_id="42"} 5350' in text
    assert 'trtllm_kv_cache_hit_rate{deployment_id="42"} 0.0' in text
    assert 'trtllm_kv_cache_tokens_per_block{deployment_id="42"} 32' in text


def test_translate_emits_help_and_type_per_metric():
    text = translate_trtllm_metrics(json.dumps(_SAMPLE), deployment_id=1)
    # Each metric has exactly one HELP/TYPE pair.
    for name in (
        "trtllm_gpu_memory_bytes",
        "trtllm_iter_latency_ms",
        "trtllm_kv_cache_hit_rate",
        "trtllm_inflight_num_scheduled_requests",
    ):
        assert text.count(f"# HELP {name} ") == 1
        assert text.count(f"# TYPE {name} gauge") == 1


def test_translate_empty_array_returns_empty_string():
    assert translate_trtllm_metrics("[]", deployment_id=1) == ""


def test_translate_malformed_json_returns_empty_string():
    # Truncated JSON, plain text, and `null` must all yield "" without raising.
    for body in ("not json at all", "[{", "null", "", "  "):
        assert translate_trtllm_metrics(body, deployment_id=1) == ""


def test_translate_missing_nested_objects_skips_those_metrics():
    # No inflightBatchingStats / kvCacheStats - top-level metrics still emit.
    sparse = [{"iter": 0, "gpuMemUsage": 1234, "maxNumActiveRequests": 8}]
    text = translate_trtllm_metrics(json.dumps(sparse), deployment_id=7)
    assert 'trtllm_gpu_memory_bytes{deployment_id="7"} 1234' in text
    assert 'trtllm_max_num_active_requests{deployment_id="7"} 8' in text
    # Nested-group metrics absent because their parent objects are missing.
    assert "trtllm_inflight_num_context_requests" not in text
    assert "trtllm_kv_cache_max_blocks" not in text


def test_translate_picks_latest_iteration_in_array():
    # When the body holds multiple iteration objects, the translator must
    # publish the most recent - Prometheus gauges are point-in-time.
    older = json.loads(json.dumps(_SAMPLE[0]))
    older["iter"] = 0
    older["gpuMemUsage"] = 100
    newer = json.loads(json.dumps(_SAMPLE[0]))
    newer["iter"] = 99
    newer["gpuMemUsage"] = 999
    text = translate_trtllm_metrics(json.dumps([older, newer]), deployment_id=3)
    assert 'trtllm_gpu_memory_bytes{deployment_id="3"} 999' in text
    assert 'trtllm_gpu_memory_bytes{deployment_id="3"} 100' not in text


def test_translate_many_groups_headers_across_deployments():
    body = json.dumps(_SAMPLE)
    text = translate_many([(1, body), (2, body)])
    # Header for each metric appears exactly once across both deployments.
    assert text.count("# HELP trtllm_gpu_memory_bytes ") == 1
    assert text.count("# TYPE trtllm_gpu_memory_bytes gauge") == 1
    # But samples for both deployment ids are present.
    assert 'trtllm_gpu_memory_bytes{deployment_id="1"} 58941767680' in text
    assert 'trtllm_gpu_memory_bytes{deployment_id="2"} 58941767680' in text


def test_translate_many_skips_empty_or_malformed_bodies():
    body = json.dumps(_SAMPLE)
    text = translate_many([
        (1, body),
        (2, "[]"),
        (3, "garbage"),
        (4, body),
    ])
    # Only deployments 1 and 4 should appear.
    assert 'deployment_id="1"' in text
    assert 'deployment_id="4"' in text
    assert 'deployment_id="2"' not in text
    assert 'deployment_id="3"' not in text


def test_translate_all_empty_returns_empty_string():
    assert translate_many([(1, "[]"), (2, "")]) == ""


def test_translate_bool_field_is_skipped():
    """Bool would render as `True`/`False` which Prometheus rejects. Defensive
    skip - none of the documented fields are booleans, but TRT-LLM's schema
    may grow."""
    obj = [{"iter": 1, "gpuMemUsage": True}]
    text = translate_trtllm_metrics(json.dumps(obj), deployment_id=1)
    assert "trtllm_gpu_memory_bytes" not in text


# --- aggregator integration ---

def test_gather_engine_metrics_translates_json_passes_prometheus(monkeypatch):
    """Bodies that look like Prometheus exposition stay verbatim; bodies that
    look like JSON arrays get routed through the TRT-LLM translator."""
    prom_body = (
        "# HELP vllm_requests Total requests.\n"
        "# TYPE vllm_requests counter\n"
        "vllm_requests 5\n"
    )
    json_body = json.dumps(_SAMPLE)

    async def fake_fetch(url: str, path: str = "/metrics") -> str:
        return prom_body if "vllm" in url else json_body

    monkeypatch.setattr(
        "berth.observability.metrics.fetch_engine_metrics", fake_fetch,
    )
    text = asyncio.run(gather_engine_metrics([
        (1, "http://vllm-host:8000"),
        (2, "http://trtllm-host:8000"),
    ]))
    # vLLM body present unchanged.
    assert "vllm_requests 5" in text
    assert "# TYPE vllm_requests counter" in text
    assert "# --- deployment 1 ---" in text
    # TRT-LLM body translated; deployment_id label set; JSON not leaked through.
    assert 'trtllm_gpu_memory_bytes{deployment_id="2"} 58941767680' in text
    assert "gpuMemUsage" not in text  # raw JSON keys absent
    assert "[" not in text.split("# --- trtllm")[1] if "# --- trtllm" in text else True


def test_gather_engine_metrics_groups_multiple_trtllm_under_one_header(monkeypatch):
    body = json.dumps(_SAMPLE)

    async def fake_fetch(url: str, path: str = "/metrics") -> str:
        return body

    monkeypatch.setattr(
        "berth.observability.metrics.fetch_engine_metrics", fake_fetch,
    )
    text = asyncio.run(gather_engine_metrics([
        (10, "http://a"),
        (11, "http://b"),
    ]))
    # Single HELP/TYPE pair across both TRT-LLM deployments.
    assert text.count("# HELP trtllm_gpu_memory_bytes ") == 1
    assert text.count("# TYPE trtllm_gpu_memory_bytes gauge") == 1
    assert 'deployment_id="10"' in text
    assert 'deployment_id="11"' in text


def test_gather_engine_metrics_empty_input_returns_empty():
    assert asyncio.run(gather_engine_metrics([])) == ""


@pytest.mark.parametrize("body", ["[", "{ ", "  [\n"])
def test_looks_like_json_handles_whitespace_prefix(body, monkeypatch):
    """Aggregator must classify bodies that begin with whitespace correctly,
    or vLLM responses (which start with `#`) and TRT-LLM (`[`) get swapped."""
    from berth.observability.metrics import _looks_like_json
    assert _looks_like_json(body)
    assert not _looks_like_json("# HELP foo bar\n")
    assert not _looks_like_json("foo_total 1\n")
