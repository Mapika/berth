"""JSON->Prometheus exposition translator for TRT-LLM's /metrics endpoint.

TRT-LLM (PyTorch backend) emits a JSON array of per-iteration stats objects
when `enable_iter_perf_stats` is set in the engine config. That format is
incompatible with Prometheus scrapers, which expect text-based exposition
format. This module translates a single response body (potentially containing
multiple iteration objects) into Prometheus exposition by taking the most
recent iteration as the point-in-time gauge value.

The entry point used by the daemon aggregator is `translate_trtllm_metrics`
for a single deployment, and `translate_many` for grouping multiple
deployments under shared `# HELP` / `# TYPE` headers (Prometheus convention:
each metric's header must appear exactly once across all its samples).
"""
from __future__ import annotations

import json
from collections.abc import Iterable

# Metric definitions: (prom_name, help_text, json_path)
# json_path is a tuple of keys to descend into the iteration object.
_TOP_METRICS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "trtllm_gpu_memory_bytes",
        "GPU memory in use by the engine, in bytes.",
        ("gpuMemUsage",),
    ),
    (
        "trtllm_iter_latency_ms",
        "Latency of the most recent iteration, in milliseconds.",
        ("iterLatencyMS",),
    ),
    (
        "trtllm_max_num_active_requests",
        "Maximum number of concurrent active requests the engine can hold.",
        ("maxNumActiveRequests",),
    ),
)

_INFLIGHT_METRICS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "trtllm_inflight_num_context_requests",
        "In-flight context (prefill) requests.",
        ("inflightBatchingStats", "numContextRequests"),
    ),
    (
        "trtllm_inflight_num_gen_requests",
        "In-flight generation (decode) requests.",
        ("inflightBatchingStats", "numGenRequests"),
    ),
    (
        "trtllm_inflight_num_scheduled_requests",
        "Requests scheduled in the current iteration.",
        ("inflightBatchingStats", "numScheduledRequests"),
    ),
    (
        "trtllm_inflight_num_paused_requests",
        "Requests currently paused.",
        ("inflightBatchingStats", "numPausedRequests"),
    ),
    (
        "trtllm_inflight_num_queued_context_requests",
        "Context requests waiting in the queue.",
        ("inflightBatchingStats", "numQueuedContextRequests"),
    ),
    (
        "trtllm_inflight_num_queued_gen_requests",
        "Generation requests waiting in the queue.",
        ("inflightBatchingStats", "numQueuedGenRequests"),
    ),
    (
        "trtllm_inflight_num_ctx_tokens",
        "Tokens being processed in the prefill phase this iteration.",
        ("inflightBatchingStats", "numCtxTokens"),
    ),
    (
        "trtllm_inflight_num_ctx_kv_tokens",
        "KV-cache tokens populated for in-flight context requests.",
        ("inflightBatchingStats", "numCtxKvTokens"),
    ),
    (
        "trtllm_inflight_num_gen_kv_tokens",
        "KV-cache tokens populated for in-flight generation requests.",
        ("inflightBatchingStats", "numGenKvTokens"),
    ),
    (
        "trtllm_inflight_avg_decoded_tokens_per_iter",
        "Average decoded tokens per request per iteration.",
        ("inflightBatchingStats", "avgNumDecodedTokensPerIter"),
    ),
)

_KV_METRICS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "trtllm_kv_cache_max_blocks",
        "Total KV-cache blocks allocated to the engine.",
        ("kvCacheStats", "maxNumBlocks"),
    ),
    (
        "trtllm_kv_cache_used_blocks",
        "KV-cache blocks currently in use.",
        ("kvCacheStats", "usedNumBlocks"),
    ),
    (
        "trtllm_kv_cache_free_blocks",
        "Free KV-cache blocks available for new requests.",
        ("kvCacheStats", "freeNumBlocks"),
    ),
    (
        "trtllm_kv_cache_alloc_new_blocks",
        "KV-cache blocks newly allocated this iteration.",
        ("kvCacheStats", "allocNewBlocks"),
    ),
    (
        "trtllm_kv_cache_alloc_total_blocks",
        "Cumulative KV-cache blocks allocated.",
        ("kvCacheStats", "allocTotalBlocks"),
    ),
    (
        "trtllm_kv_cache_reused_blocks",
        "KV-cache blocks reused via prefix caching.",
        ("kvCacheStats", "reusedBlocks"),
    ),
    (
        "trtllm_kv_cache_missed_blocks",
        "KV-cache lookups that missed and required allocation.",
        ("kvCacheStats", "missedBlocks"),
    ),
    (
        "trtllm_kv_cache_hit_rate",
        "KV-cache prefix-cache hit rate (0.0-1.0).",
        ("kvCacheStats", "cacheHitRate"),
    ),
    (
        "trtllm_kv_cache_tokens_per_block",
        "Tokens per KV-cache block (engine config).",
        ("kvCacheStats", "tokensPerBlock"),
    ),
)

_ALL_METRICS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    _TOP_METRICS + _INFLIGHT_METRICS + _KV_METRICS
)


def _parse_latest_iter(json_text: str) -> dict | None:
    """Parse the response body and return the most recent iteration object.

    Returns None on any parse error or empty array (caller treats as no-op).
    The endpoint returns a JSON array of per-iteration objects; "latest" means
    the highest `iter` value, falling back to last array position if `iter`
    is missing.
    """
    try:
        data = json.loads(json_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list) or not data:
        return None
    # Pick the object with the highest `iter` field; fall back to the last
    # element if none have it. TRT-LLM appends in order, but be defensive.
    best: dict | None = None
    best_iter = -1
    for obj in data:
        if not isinstance(obj, dict):
            continue
        it = obj.get("iter")
        if isinstance(it, int) and it > best_iter:
            best = obj
            best_iter = it
        elif best is None:
            best = obj
    return best


def _descend(obj: dict, path: tuple[str, ...]) -> object | None:
    """Walk `obj` along `path`, returning None if any key is missing."""
    cur: object = obj
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _format_value(v: object) -> str | None:
    """Render a JSON value for Prometheus exposition. None if not numeric."""
    if isinstance(v, bool):  # bool is an int subclass; reject explicitly
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        # Prometheus accepts standard float formatting; avoid scientific where
        # the input wasn't scientific. repr() round-trips and is unambiguous.
        return repr(v)
    return None


def translate_many(items: Iterable[tuple[int, str]]) -> str:
    """Translate (deployment_id, json_text) pairs into a single exposition.

    Headers are emitted once per metric across all deployments (Prometheus
    requires this; some scrapers reject duplicate # HELP / # TYPE for the
    same metric name). Deployments whose JSON is empty/malformed contribute
    no samples but do not abort the others.
    """
    # Map metric -> list of "metric{labels} value" sample lines.
    samples: dict[str, list[str]] = {name: [] for name, _, _ in _ALL_METRICS}

    for dep_id, json_text in items:
        latest = _parse_latest_iter(json_text)
        if latest is None:
            continue
        for prom_name, _help, path in _ALL_METRICS:
            v = _descend(latest, path)
            if v is None:
                continue
            rendered = _format_value(v)
            if rendered is None:
                continue
            samples[prom_name].append(
                f'{prom_name}{{deployment_id="{dep_id}"}} {rendered}',
            )

    out: list[str] = []
    for prom_name, help_text, _path in _ALL_METRICS:
        rows = samples[prom_name]
        if not rows:
            continue
        out.append(f"# HELP {prom_name} {help_text}")
        out.append(f"# TYPE {prom_name} gauge")
        out.extend(rows)
    if not out:
        return ""
    return "\n".join(out) + "\n"


def translate_trtllm_metrics(json_text: str, deployment_id: int) -> str:
    """Translate a single TRT-LLM /metrics body into Prometheus exposition.

    Convenience wrapper over `translate_many` for the single-deployment case.
    Returns the empty string for empty arrays, malformed JSON, or arrays
    whose objects expose none of the recognised fields - never raises.
    """
    return translate_many([(deployment_id, json_text)])
