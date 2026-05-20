#!/usr/bin/env python3
"""Performance harness for berth.

Methodology matches vLLM's `benchmarks/benchmark_serving.py`:

  - Poisson arrivals at a fixed request-rate (req/sec). NOT burst-of-N.
  - Per-request percentiles: TTFT, ITL (inter-token-latency), E2E.
  - Warm-up window discarded before measurement.
  - Separate cold-load probe before each measurement run.
  - Multiple --qps levels (sweep) in one invocation.

Usage:
  python scripts/bench.py \\
      --model qwen-0_5b --engine vllm \\
      --qps 1,4,16,64 --duration-s 30 --max-tokens 512 \\
      --out /tmp/bench.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import subprocess  # nosec
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:11500"
BERTH_BIN = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "berth"
# When auth is enabled (any active API key in the daemon), set BERTH_API_KEY
# to a Bearer secret. Empty string disables the header.
API_KEY = os.environ.get("BERTH_API_KEY", "")
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}


def _run_berth(*args: str) -> str:
    out = subprocess.run([str(BERTH_BIN), *args], capture_output=True, text=True)  # nosec
    if out.returncode != 0:
        raise RuntimeError(f"berth {' '.join(args)}: {out.stderr}")
    return out.stdout


# ----------------- a single request -----------------

@dataclass
class RequestResult:
    ttft_ms: float
    e2e_ms: float
    output_tokens: int
    itls_ms: list[float] = field(default_factory=list)
    failed: bool = False

    @property
    def tps(self) -> float:
        if self.output_tokens <= 1 or self.e2e_ms <= self.ttft_ms:
            return 0.0
        return (self.output_tokens - 1) / ((self.e2e_ms - self.ttft_ms) / 1000.0)


PROMPT = (
    "Write a clear, structured explanation of how large language models perform "
    "autoregressive decoding. Cover tokenisation, the attention mechanism, KV "
    "caching, and continuous batching. Aim for technical precision."
)


async def one_request(
    client: httpx.AsyncClient, model: str, max_tokens: int,
) -> RequestResult:
    t_start = time.monotonic()
    t_first: float | None = None
    t_prev: float | None = None
    itls: list[float] = []
    tokens_seen = 0

    try:
        async with client.stream(
            "POST", f"{BASE}/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={
                "model": model,
                "messages": [{"role": "user", "content": PROMPT}],
                "stream": True,
                "max_tokens": max_tokens,
                "stream_options": {"include_usage": True},
            },
        ) as r:
            if r.status_code != 200:
                return RequestResult(0, 0, 0, failed=True)
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                delta = choices[0].get("delta", {}) if choices else {}
                if delta.get("content"):
                    now = time.monotonic()
                    if t_first is None:
                        t_first = now
                    elif t_prev is not None:
                        itls.append((now - t_prev) * 1000)
                    t_prev = now
                    tokens_seen += 1
                usage = obj.get("usage")
                if usage:
                    tokens_seen = int(usage.get("completion_tokens", tokens_seen))
    except (httpx.HTTPError, TimeoutError):
        return RequestResult(0, 0, 0, failed=True)

    t_end = time.monotonic()
    if t_first is None:
        return RequestResult(0, 0, 0, failed=True)
    return RequestResult(
        ttft_ms=(t_first - t_start) * 1000,
        e2e_ms=(t_end - t_start) * 1000,
        output_tokens=tokens_seen,
        itls_ms=itls,
    )


# ----------------- one measurement run at a given QPS -----------------

async def measure_qps(
    model: str, qps: float, duration_s: float, max_tokens: int, warmup_s: float,
) -> dict:
    """Fire requests at a Poisson process with mean rate qps for duration_s seconds.

    Returns per-request percentiles + aggregate tokens/sec.
    """
    limits = httpx.Limits(max_connections=2048, max_keepalive_connections=512)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0),
        limits=limits,
    ) as client:
        # Warmup
        warm_end = time.monotonic() + warmup_s
        warm_tasks: list[asyncio.Task] = []
        while time.monotonic() < warm_end:
            warm_tasks.append(
                asyncio.create_task(one_request(client, model, max_tokens))
            )
            # Poisson inter-arrival: exponential with mean 1/qps
            await asyncio.sleep(random.expovariate(qps))
        # Drain warmup but discard results
        for t in warm_tasks:
            try:
                await t
            except Exception:
                pass  # nosec

        # Measurement
        meas_tasks: list[asyncio.Task] = []
        meas_start = time.monotonic()
        meas_end = meas_start + duration_s
        while time.monotonic() < meas_end:
            meas_tasks.append(
                asyncio.create_task(one_request(client, model, max_tokens))
            )
            await asyncio.sleep(random.expovariate(qps))
        results: list[RequestResult] = []
        for t in meas_tasks:
            try:
                results.append(await t)
            except Exception:
                results.append(RequestResult(0, 0, 0, failed=True))
        wall_s = time.monotonic() - meas_start

    successes = [r for r in results if not r.failed]
    failures = sum(1 for r in results if r.failed)
    out_tokens = sum(r.output_tokens for r in successes)
    ttfts = sorted(r.ttft_ms for r in successes)
    e2es = sorted(r.e2e_ms for r in successes)
    all_itls = sorted(itl for r in successes for itl in r.itls_ms)

    return {
        "qps_target": qps,
        "qps_observed": len(results) / wall_s if wall_s else 0,
        "n_requests": len(results),
        "n_failures": failures,
        "duration_s": wall_s,
        "tokens_out_total": out_tokens,
        "tokens_per_sec_agg": out_tokens / wall_s if wall_s else 0,
        "ttft_ms_p50": pct(ttfts, 50),
        "ttft_ms_p95": pct(ttfts, 95),
        "ttft_ms_p99": pct(ttfts, 99),
        "itl_ms_p50": pct(all_itls, 50),
        "itl_ms_p95": pct(all_itls, 95),
        "itl_ms_p99": pct(all_itls, 99),
        "e2e_ms_p50": pct(e2es, 50),
        "e2e_ms_p95": pct(e2es, 95),
        "e2e_ms_p99": pct(e2es, 99),
        "tps_per_stream_mean": (
            statistics.mean(r.tps for r in successes if r.tps > 0)
            if any(r.tps > 0 for r in successes) else 0
        ),
    }


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    k = max(0, min(len(xs) - 1, round((p / 100) * (len(xs) - 1))))
    return xs[k]


# ----------------- cold-load probe -----------------

def cold_load(model: str, engine: str, ctx: int = 4096) -> float:
    _run_berth("stop")
    time.sleep(2)
    t0 = time.time()
    _run_berth("run", model, "--gpu", "0", "--ctx", str(ctx), "--engine", engine)
    return time.time() - t0


# ----------------- main -----------------

async def amain(args: argparse.Namespace) -> None:
    qps_levels = [float(x) for x in args.qps.split(",")]
    results: list[dict] = []
    for model in args.models.split(","):
        for engine in args.engines.split(","):
            print(f"\n=== {model} on {engine} ===", flush=True)
            if args.no_cold_load:
                cold_s = 0.0
                print("  cold_load skipped (--no-cold-load)", flush=True)
            else:
                cold_s = cold_load(model, engine, ctx=args.ctx)
                print(f"  cold_load_s        = {cold_s:.1f}", flush=True)

            for qps in qps_levels:
                print(f"  measuring qps={qps} ...", flush=True)
                r = await measure_qps(
                    model, qps,
                    duration_s=args.duration_s,
                    max_tokens=args.max_tokens,
                    warmup_s=args.warmup_s,
                )
                r["model"] = model
                r["engine"] = engine
                r["cold_load_s"] = cold_s
                results.append(r)
                print(
                    f"    n={r['n_requests']:<3}  "
                    f"agg_tps={r['tokens_per_sec_agg']:.0f}  "
                    f"ttft_p50={r['ttft_ms_p50']:.0f}/p99={r['ttft_ms_p99']:.0f}  "
                    f"itl_p50={r['itl_ms_p50']:.1f}/p99={r['itl_ms_p99']:.1f}  "
                    f"e2e_p50={r['e2e_ms_p50']:.0f}",
                    flush=True,
                )
                Path(args.out).write_text(json.dumps(results, indent=2))

    print(f"\n=== written to {args.out} ===")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="qwen-0_5b,qwen-1_5b")
    ap.add_argument("--engines", default="vllm,sglang")
    ap.add_argument("--qps", default="1,4,16,32",
                    help="Request rates (req/sec, Poisson arrivals)")
    ap.add_argument("--duration-s", type=float, default=20.0)
    ap.add_argument("--warmup-s", type=float, default=5.0)
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--ctx", type=int, default=4096)
    ap.add_argument("--out", default="/tmp/bench-v2.json")  # nosec
    ap.add_argument("--no-cold-load", action="store_true",
                    help="Skip the stop+run cold-load probe; bench the currently "
                         "loaded deployment. Use when you've manually tuned `berth run` "
                         "flags that cold_load would clobber.")
    args = ap.parse_args()
    random.seed(0)
    asyncio.run(amain(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
