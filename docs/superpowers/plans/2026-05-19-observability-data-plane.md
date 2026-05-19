# Observability Data Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream per-node and per-deployment metrics from each agent to the leader, expose them via Prometheus and the existing UI, and lay the foundation for the smart-routing plan that follows.

**Architecture:** Each agent samples GPU + in-flight + recent latency on every heartbeat tick and piggybacks the snapshot on the existing `Heartbeat` frame. The leader's `metrics_aggregator` keeps a 60 s ring per node, feeds the existing Prometheus exposition, and serves a read-only snapshot endpoint that the UI consumes for sparklines.

**Tech Stack:** Python 3.13, FastAPI, websockets, asyncio, pytest, React/TypeScript for the UI surface, pynvml for GPU sampling (already in tree).

**Spec:** `docs/superpowers/specs/2026-05-19-load-aware-multi-node-serving-design.md`

---

## File Structure

**New files (Python):**
- `src/serve_engine/cluster/metrics_collector.py` — agent-side sampler; pure data-gathering, no IPC.
- `src/serve_engine/daemon/metrics_aggregator.py` — leader-side ring buffer + sinks. In-memory, stateless across restarts.

**New files (frontend / docs):**
- `docs/dashboards/serve-engine.json` — sample Grafana dashboard, imported by operators.

**New tests:**
- `tests/unit/test_metrics_collector.py`
- `tests/unit/test_metrics_aggregator.py`
- `tests/integration/test_metrics_aggregation.py`

**Modified:**
- `src/serve_engine/cluster/protocol.py` — `Heartbeat` gains optional `metrics` field.
- `src/serve_engine/cluster/agent_client.py` — heartbeat task pulls a snapshot from the collector.
- `src/serve_engine/cluster/leader_hub.py` — feeds the aggregator on every heartbeat.
- `src/serve_engine/daemon/openai_proxy.py` — in-flight counter increment/decrement around the dispatch path.
- `src/serve_engine/daemon/admin.py` — adds `GET /admin/metrics/snapshot`.
- `src/serve_engine/observability/metrics.py` — registers new gauges/counters/histograms.
- `ui/src/views/Cluster.tsx` — sparklines on node cards.
- `docs/multi-node.md` — documents the metrics surface.

**Boundaries:** Collector knows nothing about the leader. Aggregator knows nothing about WebSockets — it consumes plain dataclasses. Both are unit-testable without network or processes.

---

## Task 1: Extend the `Heartbeat` frame with optional metrics fields

**Files:**
- Modify: `src/serve_engine/cluster/protocol.py`
- Test: `tests/unit/test_protocol.py` (create if absent)

Optional means: old agents that send `{"type": "heartbeat", "ts": ...}` keep working. New agents add a `metrics` field.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_protocol.py` (or extend if it exists):

```python
from __future__ import annotations

from serve_engine.cluster.protocol import Heartbeat, decode_frame, encode_frame


def test_heartbeat_round_trips_without_metrics():
    hb = Heartbeat(ts=1234.5)
    decoded = decode_frame(encode_frame(hb))
    assert isinstance(decoded, Heartbeat)
    assert decoded.ts == 1234.5
    assert decoded.metrics is None


def test_heartbeat_round_trips_with_metrics():
    hb = Heartbeat(
        ts=1234.5,
        metrics={
            "gpus": [{"index": 0, "mem_used_mb": 1024, "mem_total_mb": 81920,
                      "util_pct": 42, "temp_c": 55}],
            "deployments": [{"deployment_id": 7, "model_id": "llama3-8b",
                             "in_flight": 3, "requests_last_window": 12,
                             "latency_p50_ms": 120, "latency_p95_ms": 450,
                             "errors_last_window": 0}],
            "node": {"uptime_s": 99.0, "host_load_avg_1m": 0.8},
        },
    )
    decoded = decode_frame(encode_frame(hb))
    assert isinstance(decoded, Heartbeat)
    assert decoded.metrics is not None
    assert decoded.metrics["gpus"][0]["util_pct"] == 42
    assert decoded.metrics["deployments"][0]["in_flight"] == 3


def test_legacy_heartbeat_wire_format_still_decodes():
    """A heartbeat from an old agent (no metrics field) must still parse."""
    raw = '{"type": "heartbeat", "ts": 1234.5}'
    decoded = decode_frame(raw)
    assert isinstance(decoded, Heartbeat)
    assert decoded.metrics is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_protocol.py -v`
Expected: FAIL — `Heartbeat.__init__()` got unexpected keyword argument `metrics`.

- [ ] **Step 3: Add the field**

Modify `src/serve_engine/cluster/protocol.py`. Replace the `Heartbeat` dataclass with:

```python
@dataclass
class Heartbeat:
    ts: float
    metrics: dict[str, Any] | None = None
    type: str = "heartbeat"
```

The `metrics` payload is an untyped `dict` on the wire intentionally — the schema lives in `metrics_collector.py` and `metrics_aggregator.py`, not in the transport layer. The transport just carries it.

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_protocol.py -v`
Expected: 3 passed.

Run the full test suite to confirm nothing else broke:
Run: `pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cluster/protocol.py tests/unit/test_protocol.py
git commit -m "feat(cluster): heartbeat carries optional metrics payload"
```

---

## Task 2: Agent-side metrics collector module

**Files:**
- Create: `src/serve_engine/cluster/metrics_collector.py`
- Test: `tests/unit/test_metrics_collector.py`

The collector is a small dataclass-returning function plus a thin in-process counter object. No threads, no I/O of its own beyond reading from `gpu_stats.read_gpu_stats()` and from a counter the proxy increments. Latency stats are summarised from a fixed-size deque of recent samples.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_metrics_collector.py`:

```python
from __future__ import annotations

import pytest

from serve_engine.cluster import metrics_collector as mc


def test_in_flight_counter_starts_at_zero():
    c = mc.InFlightCounter()
    assert c.snapshot() == {}


def test_in_flight_counter_increments_and_decrements_by_deployment():
    c = mc.InFlightCounter()
    c.start(7)
    c.start(7)
    c.start(9)
    assert c.snapshot() == {7: 2, 9: 1}
    c.finish(7)
    assert c.snapshot() == {7: 1, 9: 1}


def test_in_flight_counter_never_goes_negative():
    c = mc.InFlightCounter()
    c.finish(7)
    assert c.snapshot() == {}


def test_latency_recorder_buckets_recent_samples():
    r = mc.LatencyRecorder()
    r.record(deployment_id=7, latency_ms=100, error=False)
    r.record(deployment_id=7, latency_ms=200, error=False)
    r.record(deployment_id=7, latency_ms=300, error=True)
    summary = r.summarize_and_reset(7)
    assert summary.requests_last_window == 3
    assert summary.errors_last_window == 1
    # p50 of [100,200,300] is 200; p95 with n=3 is 300 (nearest-rank).
    assert summary.latency_p50_ms == 200
    assert summary.latency_p95_ms == 300


def test_latency_recorder_resets_after_summarize():
    r = mc.LatencyRecorder()
    r.record(deployment_id=7, latency_ms=100, error=False)
    r.summarize_and_reset(7)
    summary = r.summarize_and_reset(7)
    assert summary.requests_last_window == 0
    assert summary.latency_p50_ms == 0
    assert summary.latency_p95_ms == 0


def test_build_snapshot_assembles_all_three_sections(monkeypatch):
    from serve_engine.observability.gpu_stats import GPUSnapshot

    monkeypatch.setattr(
        mc, "read_gpu_stats",
        lambda: [GPUSnapshot(index=0, memory_used_mb=2048, memory_total_mb=81920,
                             gpu_util_pct=42, power_w=120)],
    )
    in_flight = mc.InFlightCounter()
    in_flight.start(7)
    latency = mc.LatencyRecorder()
    latency.record(deployment_id=7, latency_ms=150, error=False)

    snap = mc.build_snapshot(
        in_flight=in_flight, latency=latency,
        deployment_models={7: "llama3-8b"},
        uptime_s=42.0,
    )
    assert snap["gpus"][0]["util_pct"] == 42
    assert snap["deployments"][0]["deployment_id"] == 7
    assert snap["deployments"][0]["model_id"] == "llama3-8b"
    assert snap["deployments"][0]["in_flight"] == 1
    assert snap["deployments"][0]["requests_last_window"] == 1
    assert snap["node"]["uptime_s"] == 42.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_metrics_collector.py -v`
Expected: FAIL — `ModuleNotFoundError: serve_engine.cluster.metrics_collector`.

- [ ] **Step 3: Implement the collector**

Create `src/serve_engine/cluster/metrics_collector.py`:

```python
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from serve_engine.observability.gpu_stats import read_gpu_stats

# Per deployment we keep at most this many recent (latency_ms, error)
# samples between summarize_and_reset calls. The heartbeat interval is
# 5 s; at 200 QPS per deployment that's 1000 samples per window, far
# more than we need for a coarse p50/p95. 1024 is a safe ceiling that
# never lets one chatty deployment leak unbounded memory.
_LATENCY_WINDOW = 1024


class InFlightCounter:
    """Per-deployment in-flight request counter. Process-local."""

    def __init__(self) -> None:
        self._counts: defaultdict[int, int] = defaultdict(int)

    def start(self, deployment_id: int) -> None:
        self._counts[deployment_id] += 1

    def finish(self, deployment_id: int) -> None:
        n = self._counts.get(deployment_id, 0)
        if n <= 1:
            self._counts.pop(deployment_id, None)
        else:
            self._counts[deployment_id] = n - 1

    def snapshot(self) -> dict[int, int]:
        return dict(self._counts)


@dataclass(frozen=True)
class DeploymentLatencySummary:
    requests_last_window: int
    errors_last_window: int
    latency_p50_ms: int
    latency_p95_ms: int


class LatencyRecorder:
    """Per-deployment recent-latency ring. `summarize_and_reset` drains."""

    def __init__(self) -> None:
        self._samples: defaultdict[int, deque[tuple[int, bool]]] = defaultdict(
            lambda: deque(maxlen=_LATENCY_WINDOW)
        )

    def record(
        self, *, deployment_id: int, latency_ms: int, error: bool,
    ) -> None:
        self._samples[deployment_id].append((latency_ms, error))

    def summarize_and_reset(
        self, deployment_id: int,
    ) -> DeploymentLatencySummary:
        samples = list(self._samples.get(deployment_id, ()))
        self._samples.pop(deployment_id, None)
        if not samples:
            return DeploymentLatencySummary(0, 0, 0, 0)
        latencies = sorted(s[0] for s in samples)
        errors = sum(1 for s in samples if s[1])
        return DeploymentLatencySummary(
            requests_last_window=len(samples),
            errors_last_window=errors,
            latency_p50_ms=_percentile(latencies, 50),
            latency_p95_ms=_percentile(latencies, 95),
        )

    def known_deployments(self) -> list[int]:
        return list(self._samples.keys())


def _percentile(sorted_values: list[int], pct: int) -> int:
    """Nearest-rank percentile on a pre-sorted list. Returns 0 on empty."""
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = max(0, min(len(sorted_values) - 1,
                      int(round((pct / 100.0) * (len(sorted_values) - 1)))))
    return sorted_values[rank]


def build_snapshot(
    *,
    in_flight: InFlightCounter,
    latency: LatencyRecorder,
    deployment_models: dict[int, str],
    uptime_s: float,
) -> dict[str, Any]:
    """Assemble the dict that goes over the wire in Heartbeat.metrics.

    `deployment_models` maps deployment_id to model name so the leader
    can render a 'model_id' label without round-tripping to its own DB
    (which it could, but this keeps the leader aggregator a pure consumer).
    """
    gpus = [
        {
            "index": g.index,
            "mem_used_mb": g.memory_used_mb,
            "mem_total_mb": g.memory_total_mb,
            "util_pct": g.gpu_util_pct,
            "temp_c": 0,
        }
        for g in read_gpu_stats()
    ]
    in_flight_map = in_flight.snapshot()
    deployment_ids = set(in_flight_map) | set(latency.known_deployments())
    deployments = []
    for dep_id in sorted(deployment_ids):
        summary = latency.summarize_and_reset(dep_id)
        deployments.append({
            "deployment_id": dep_id,
            "model_id": deployment_models.get(dep_id, ""),
            "in_flight": in_flight_map.get(dep_id, 0),
            "requests_last_window": summary.requests_last_window,
            "latency_p50_ms": summary.latency_p50_ms,
            "latency_p95_ms": summary.latency_p95_ms,
            "errors_last_window": summary.errors_last_window,
        })
    return {
        "gpus": gpus,
        "deployments": deployments,
        "node": {"uptime_s": uptime_s, "host_load_avg_1m": 0.0},
    }
```

`temp_c` and `host_load_avg_1m` are stubbed to 0 — pynvml's temp API and `os.getloadavg()` are both available, but adding them is a follow-up. The schema field exists so we don't have to change the wire format later.

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_metrics_collector.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cluster/metrics_collector.py tests/unit/test_metrics_collector.py
git commit -m "feat(cluster): agent-side metrics collector (in-flight, latency, gpus)"
```

---

## Task 3: Hook the in-flight counter and latency recorder into the OpenAI proxy

**Files:**
- Modify: `src/serve_engine/daemon/openai_proxy.py`
- Test: `tests/integration/test_openai_proxy_http.py` (extend the existing test)

The proxy needs to: (a) call `in_flight.start(dep_id)` before dispatching, (b) call `in_flight.finish(dep_id)` after the response completes (success or error), (c) record latency on completion. The collector instances live on the agent's `LocalAgent` (single-node) or the leader's local-agent surface — for both cases the proxy gets them from app state.

- [ ] **Step 1: Inspect the existing proxy flow**

Read `src/serve_engine/daemon/openai_proxy.py:1-100` to find the place where the dispatch starts and ends. Look for `active` (the resolved deployment) and the response-completion site. We'll wrap the dispatch in a try/finally.

- [ ] **Step 2: Write the failing test**

Extend `tests/integration/test_openai_proxy_http.py` with a new test (read the file first to match its fixture style). Add at the bottom:

```python
@pytest.mark.asyncio
async def test_proxy_records_in_flight_and_latency(
    monkeypatch, proxy_client, in_flight, latency,
):
    """The proxy must increment in_flight before dispatch and finalise
    it after (success or error), and record a latency sample.
    `in_flight` and `latency` are new pytest fixtures wired to the
    app's collector instances."""
    # in_flight must be non-zero at the moment dispatch starts.
    seen_in_flight: list[int] = []
    real_dispatch = proxy_client.app.state.dispatch
    async def spy_dispatch(*args, **kw):
        seen_in_flight.append(in_flight.snapshot().get(7, 0))
        async for chunk in real_dispatch(*args, **kw):
            yield chunk
    monkeypatch.setattr(proxy_client.app.state, "dispatch", spy_dispatch)

    r = await proxy_client.post(
        "/v1/chat/completions",
        json={"model": "test-base", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert seen_in_flight == [1]
    assert in_flight.snapshot() == {}  # released after completion
    assert latency.summarize_and_reset(7).requests_last_window == 1
```

The existing test fixture `proxy_client` already wires a fake backend. The new fixtures `in_flight` and `latency` need to be added in the same file's setup — they read `app.state.in_flight` and `app.state.latency` which Task 4 wires.

If the existing test file does not have `app.state.in_flight` set, this test will fail at fixture resolution. That's fine — that's exactly the failure we want before implementing.

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/integration/test_openai_proxy_http.py::test_proxy_records_in_flight_and_latency -v`
Expected: FAIL — `app.state` has no `in_flight` (or test errors at fixture wiring).

- [ ] **Step 4: Wire the collector into the proxy path**

In `src/serve_engine/daemon/openai_proxy.py`, find where dispatch happens (just after `active` is set, around the existing `tracer.update(trace, deployment_id=active.id, ...)`). Wrap the dispatch in a counter:

```python
import time as _time

# ... existing code up through `tracer.update(trace, deployment_id=active.id, backend=active.backend)` ...

in_flight = getattr(request.app.state, "in_flight", None)
latency = getattr(request.app.state, "latency", None)
if in_flight is not None:
    in_flight.start(active.id)
dispatch_started_at = _time.monotonic()
errored = False
try:
    # ... existing dispatch / streaming code ...
    return result
except Exception:
    errored = True
    raise
finally:
    if in_flight is not None:
        in_flight.finish(active.id)
    if latency is not None:
        latency_ms = int((_time.monotonic() - dispatch_started_at) * 1000)
        latency.record(
            deployment_id=active.id,
            latency_ms=latency_ms,
            error=errored,
        )
```

`getattr(..., None)` guard is intentional — older test setups without a collector keep working unchanged.

- [ ] **Step 5: Wire `app.state.in_flight` and `app.state.latency` in `app.py`**

Edit `src/serve_engine/daemon/app.py` startup (find the place that builds `app.state`). Add:

```python
from serve_engine.cluster.metrics_collector import InFlightCounter, LatencyRecorder

app.state.in_flight = InFlightCounter()
app.state.latency = LatencyRecorder()
```

- [ ] **Step 6: Add the test fixtures**

In `tests/integration/test_openai_proxy_http.py`, add fixtures that expose the collectors:

```python
@pytest.fixture
def in_flight(proxy_client):
    return proxy_client.app.state.in_flight

@pytest.fixture
def latency(proxy_client):
    return proxy_client.app.state.latency
```

- [ ] **Step 7: Run tests, verify pass**

Run: `pytest tests/integration/test_openai_proxy_http.py -v`
Expected: all green, including the new test.

Run the full proxy suite to confirm no regression:
Run: `pytest tests/integration/test_openai_proxy.py tests/integration/test_openai_proxy_http.py -v`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/serve_engine/daemon/openai_proxy.py src/serve_engine/daemon/app.py tests/integration/test_openai_proxy_http.py
git commit -m "feat(proxy): record in-flight + latency around dispatch"
```

---

## Task 4: Emit the metrics payload from the agent heartbeat task

**Files:**
- Modify: `src/serve_engine/cluster/agent_client.py`
- Modify: `src/serve_engine/cluster/local_bootstrap.py` (or wherever the agent client is constructed)
- Test: `tests/unit/test_agent_client.py` (extend)

The heartbeat task currently sends `Heartbeat(ts=time.time())`. It should send `Heartbeat(ts=..., metrics=build_snapshot(...))`. The collector instances live on the agent process; we pass them into `AgentClient`.

- [ ] **Step 1: Locate the heartbeat task**

Read `src/serve_engine/cluster/agent_client.py:480-510` (the `heartbeat()` inner function found earlier). Confirm the loop structure and the `encode_frame(Heartbeat(ts=_t.time()))` call.

- [ ] **Step 2: Write the failing test**

Extend `tests/unit/test_agent_client.py`. Add:

```python
@pytest.mark.asyncio
async def test_heartbeat_carries_metrics_when_collectors_provided():
    """When AgentClient is constructed with collectors, its heartbeat
    frames carry a non-None metrics payload assembled from them."""
    from serve_engine.cluster.agent_client import AgentClient
    from serve_engine.cluster.metrics_collector import InFlightCounter, LatencyRecorder
    from serve_engine.cluster.protocol import decode_frame

    in_flight = InFlightCounter()
    in_flight.start(7)
    latency = LatencyRecorder()
    latency.record(deployment_id=7, latency_ms=120, error=False)

    sent: list[str] = []
    class FakeWS:
        async def send(self, msg): sent.append(msg)
        async def recv(self): return None

    snap = AgentClient._build_heartbeat_frame(
        in_flight=in_flight,
        latency=latency,
        deployment_models={7: "llama3-8b"},
        uptime_s=10.0,
    )
    # Round-trip through the wire to validate the schema:
    decoded = decode_frame(snap.encode_payload())
    assert decoded.metrics is not None
    assert decoded.metrics["deployments"][0]["deployment_id"] == 7
    assert decoded.metrics["deployments"][0]["in_flight"] == 1
```

Note: the test uses a small static helper `_build_heartbeat_frame` we'll add to keep heartbeat emission unit-testable without spinning up a real WS loop.

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_agent_client.py::test_heartbeat_carries_metrics_when_collectors_provided -v`
Expected: FAIL — `AgentClient._build_heartbeat_frame` does not exist.

- [ ] **Step 4: Implement the helper and wire it into the heartbeat task**

In `src/serve_engine/cluster/agent_client.py`:

1. Add imports at the top:

```python
from serve_engine.cluster.metrics_collector import (
    InFlightCounter,
    LatencyRecorder,
    build_snapshot,
)
```

2. Add the `__init__` parameters (the AgentClient already takes some — add to its existing list):

```python
def __init__(
    self,
    # ... existing args ...
    in_flight: InFlightCounter | None = None,
    latency: LatencyRecorder | None = None,
    deployment_models: dict[int, str] | None = None,
    started_at: float | None = None,
) -> None:
    # ... existing assignments ...
    self._in_flight = in_flight
    self._latency = latency
    self._deployment_models = deployment_models if deployment_models is not None else {}
    self._started_at = started_at if started_at is not None else _t.time()
```

3. Add the static helper (testable without the loop):

```python
@staticmethod
def _build_heartbeat_frame(
    *,
    in_flight: InFlightCounter | None,
    latency: LatencyRecorder | None,
    deployment_models: dict[int, str],
    uptime_s: float,
) -> Heartbeat:
    metrics = None
    if in_flight is not None and latency is not None:
        metrics = build_snapshot(
            in_flight=in_flight,
            latency=latency,
            deployment_models=deployment_models,
            uptime_s=uptime_s,
        )
    return Heartbeat(ts=_t.time(), metrics=metrics)
```

4. In the `heartbeat()` inner task (around line 482), replace the existing send with:

```python
async def heartbeat():
    while True:
        frame = AgentClient._build_heartbeat_frame(
            in_flight=self._in_flight,
            latency=self._latency,
            deployment_models=self._deployment_models,
            uptime_s=_t.time() - self._started_at,
        )
        await ws.send(encode_frame(frame))
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
```

(Keep the existing interval constant; if it's not named, leave the literal as it is.)

- [ ] **Step 5: Update the agent process to pass the collectors**

Find where `AgentClient` is constructed for the standalone agent process (`src/serve_engine/cluster/local_bootstrap.py` or `cli/agent_cmd.py`). The agent process needs its own `InFlightCounter` and `LatencyRecorder` that the agent's local proxy (the one inside the agent process) writes to.

For now (this plan delivers metrics from the *leader* node and stubs them empty on remote agents — wiring remote-agent's local proxy to the same counters is part of the routing plan), pass empty/new instances:

```python
# In the agent bootstrap:
in_flight = InFlightCounter()
latency = LatencyRecorder()
client = AgentClient(
    # ... existing args ...
    in_flight=in_flight,
    latency=latency,
    deployment_models={},  # populated on container start; see Task 4b
)
```

- [ ] **Step 5b: Keep deployment_models current**

When the agent starts a deployment (search `start_deployment` handling in `agent_client.py`), record the mapping:

```python
# After successful start:
self._deployment_models[plan["deployment_id"]] = plan.get("model_name", "")
```

When it stops one:

```python
# After successful stop:
self._deployment_models.pop(deployment_id, None)
```

If the deployment plan dict doesn't carry `model_name` today, leave the value empty — the leader can still label by `deployment_id`. Wiring the model name through is a small follow-up, not a blocker.

- [ ] **Step 6: Run tests, verify pass**

Run: `pytest tests/unit/test_agent_client.py -v`
Expected: all green.

Run the existing cluster integration test:
Run: `pytest tests/integration/test_remote_agent_roundtrip.py -v`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/serve_engine/cluster/agent_client.py src/serve_engine/cluster/local_bootstrap.py tests/unit/test_agent_client.py
git commit -m "feat(cluster): agent heartbeat carries metrics snapshot"
```

---

## Task 5: Leader-side metrics aggregator

**Files:**
- Create: `src/serve_engine/daemon/metrics_aggregator.py`
- Test: `tests/unit/test_metrics_aggregator.py`

Holds the last 12 samples per node (60 s at 5 s heartbeat). Pure in-memory; thread-safe with a single lock since FastAPI handlers may read concurrently with the heartbeat handler writing.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_metrics_aggregator.py`:

```python
from __future__ import annotations

import time

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
    assert series[0] == 8     # 20 - 12
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


def test_aggregator_thread_safety_under_concurrent_ingest(monkeypatch):
    """Two threads ingesting + reading concurrently must not crash or
    drop the underlying deque length invariant."""
    import threading
    a = MetricsAggregator(window=12)
    stop = False
    def writer():
        for i in range(500):
            a.ingest(node_id=1, sample=_sample(util=i), ts=float(i))
    def reader():
        for _ in range(500):
            _ = a.snapshot()
    t1, t2 = threading.Thread(target=writer), threading.Thread(target=reader)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert len(a.series(node_id=1, key="gpu_util_pct", gpu=0)) == 12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_metrics_aggregator.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the aggregator**

Create `src/serve_engine/daemon/metrics_aggregator.py`:

```python
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

# 12 samples × 5 s heartbeat = 60 s rolling window. The aggregator does
# not enforce the 5 s interval — the agent sets it. The window length
# here is just "how many of the most recent we keep."
DEFAULT_WINDOW = 12


@dataclass(frozen=True)
class _StampedSample:
    ts: float
    sample: dict[str, Any]


class MetricsAggregator:
    """Leader-side ring of per-node metric samples.

    In-memory, stateless across restarts. Single lock — read and write
    paths both hold it briefly. The hot path is heartbeat ingest at
    O(nodes/5s), so contention is irrelevant in practice.
    """

    def __init__(self, *, window: int = DEFAULT_WINDOW) -> None:
        self._window = window
        self._by_node: dict[int, deque[_StampedSample]] = {}
        self._lock = threading.Lock()

    def ingest(self, *, node_id: int, sample: dict[str, Any], ts: float) -> None:
        with self._lock:
            buf = self._by_node.get(node_id)
            if buf is None:
                buf = deque(maxlen=self._window)
                self._by_node[node_id] = buf
            buf.append(_StampedSample(ts=ts, sample=sample))

    def drop_node(self, node_id: int) -> None:
        with self._lock:
            self._by_node.pop(node_id, None)

    def snapshot(self) -> dict[int, dict[str, Any]]:
        """Latest sample per node, by node_id. Used by UI + admin."""
        with self._lock:
            return {
                nid: buf[-1].sample
                for nid, buf in self._by_node.items()
                if buf
            }

    def series(
        self, *, node_id: int, key: str, gpu: int | None = None,
    ) -> list[int]:
        """Extract a one-dimensional time series for sparklines.

        Known keys: 'gpu_util_pct', 'gpu_mem_used_mb' (require `gpu`),
        'request_rate' (sum of requests_last_window across deployments).
        Unknown keys return [].
        """
        with self._lock:
            buf = self._by_node.get(node_id)
            if not buf:
                return []
            samples = [s.sample for s in buf]

        out: list[int] = []
        for s in samples:
            v = _extract(s, key=key, gpu=gpu)
            out.append(v)
        return out

    def deployment_in_flight(
        self, *, node_id: int, deployment_id: int,
    ) -> int:
        with self._lock:
            buf = self._by_node.get(node_id)
            if not buf:
                return 0
            latest = buf[-1].sample
        for d in latest.get("deployments", []):
            if d.get("deployment_id") == deployment_id:
                return int(d.get("in_flight", 0))
        return 0

    def all_nodes(self) -> list[int]:
        with self._lock:
            return list(self._by_node.keys())


def _extract(sample: dict[str, Any], *, key: str, gpu: int | None) -> int:
    if key == "gpu_util_pct":
        for g in sample.get("gpus", []):
            if g.get("index") == gpu:
                return int(g.get("util_pct", 0))
        return 0
    if key == "gpu_mem_used_mb":
        for g in sample.get("gpus", []):
            if g.get("index") == gpu:
                return int(g.get("mem_used_mb", 0))
        return 0
    if key == "request_rate":
        return sum(
            int(d.get("requests_last_window", 0))
            for d in sample.get("deployments", [])
        )
    return 0
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_metrics_aggregator.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/daemon/metrics_aggregator.py tests/unit/test_metrics_aggregator.py
git commit -m "feat(daemon): metrics aggregator (per-node ring buffer)"
```

---

## Task 6: Wire the aggregator into the leader hub

**Files:**
- Modify: `src/serve_engine/cluster/leader_hub.py`
- Modify: `src/serve_engine/daemon/app.py` (construct aggregator at startup)
- Test: `tests/unit/test_leader_hub_metrics.py` (new)

`LeaderHub` is currently constructed with `(conn, registry, fingerprint_resolver)`. We add an optional `aggregator: MetricsAggregator | None`. The heartbeat handler calls `aggregator.ingest(...)` when the frame has a `metrics` payload.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_leader_hub_metrics.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from serve_engine.cluster.leader_hub import LeaderHub
from serve_engine.cluster.protocol import Heartbeat
from serve_engine.daemon.metrics_aggregator import MetricsAggregator


@pytest.mark.asyncio
async def test_leader_hub_feeds_aggregator_on_heartbeat():
    """When LeaderHub receives a Heartbeat with a metrics payload, it
    must call MetricsAggregator.ingest with the right node_id and
    sample."""
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


@pytest.mark.asyncio
async def test_leader_hub_heartbeat_without_metrics_is_a_noop():
    agg = MetricsAggregator()
    hub = LeaderHub(
        conn=MagicMock(), registry=MagicMock(),
        fingerprint_resolver=lambda ws: "fp", aggregator=agg,
    )
    hub._handle_heartbeat(node_id=5, frame=Heartbeat(ts=100.0, metrics=None))
    assert agg.snapshot() == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_leader_hub_metrics.py -v`
Expected: FAIL — `LeaderHub.__init__()` got unexpected keyword `aggregator`, and `_handle_heartbeat` doesn't exist.

- [ ] **Step 3: Wire the aggregator into the hub**

Modify `src/serve_engine/cluster/leader_hub.py`:

1. Add the import:

```python
from serve_engine.daemon.metrics_aggregator import MetricsAggregator
```

2. Change `__init__`:

```python
def __init__(
    self,
    *,
    conn: sqlite3.Connection,
    registry: AgentRegistry,
    fingerprint_resolver: FingerprintResolver = _default_fingerprint_resolver,
    aggregator: MetricsAggregator | None = None,
) -> None:
    self._conn = conn
    self._registry = registry
    self._resolve_fp = fingerprint_resolver
    self._aggregator = aggregator
    self.router = APIRouter()
    self.router.add_api_websocket_route("/cluster/agent", self._handle_agent)
```

3. Extract the heartbeat handler so it's unit-testable. Replace the existing block:

```python
if isinstance(frame, Heartbeat):
    nodes_store.set_status(
        self._conn, node.id,
        status="ready", last_seen=time.time(),
    )
    continue
```

with:

```python
if isinstance(frame, Heartbeat):
    self._handle_heartbeat(node_id=node.id, frame=frame)
    continue
```

and add the method on the class:

```python
def _handle_heartbeat(self, *, node_id: int, frame: Heartbeat) -> None:
    nodes_store.set_status(
        self._conn, node_id, status="ready", last_seen=time.time(),
    )
    if frame.metrics is not None and self._aggregator is not None:
        self._aggregator.ingest(
            node_id=node_id, sample=frame.metrics, ts=frame.ts,
        )
```

4. In the `finally:` block that handles node disconnect (around line 173), drop the node from the aggregator:

```python
finally:
    link.shutdown()
    self._registry.unregister(node.id)
    if self._aggregator is not None:
        self._aggregator.drop_node(node.id)
    nodes_store.set_status(
        self._conn, node.id,
        status="unreachable", last_seen=time.time(),
    )
```

- [ ] **Step 4: Wire the aggregator at app startup**

In `src/serve_engine/daemon/app.py`, find where `LeaderHub` is constructed (search for `LeaderHub(`). Construct an aggregator first and pass it in, then expose it on `app.state` for the admin endpoint:

```python
from serve_engine.daemon.metrics_aggregator import MetricsAggregator

app.state.metrics_aggregator = MetricsAggregator()
hub = LeaderHub(
    conn=conn, registry=registry,
    aggregator=app.state.metrics_aggregator,
)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `pytest tests/unit/test_leader_hub_metrics.py -v`
Expected: 2 passed.

Run: `pytest tests/integration/test_remote_agent_roundtrip.py tests/integration/test_daemon_tls_listeners.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/serve_engine/cluster/leader_hub.py src/serve_engine/daemon/app.py tests/unit/test_leader_hub_metrics.py
git commit -m "feat(cluster): leader hub feeds metrics aggregator on heartbeat"
```

---

## Task 7: Prometheus exposition for the new series

**Files:**
- Modify: `src/serve_engine/observability/metrics.py`
- Test: `tests/unit/test_observability_metrics.py` (new)

`format_daemon_metrics` already builds a Prometheus exposition string. We add a new function `format_cluster_metrics(aggregator)` that walks the snapshot and emits gauges / counters / histograms.

- [ ] **Step 1: Locate the metrics endpoint**

Search where `format_daemon_metrics` is called:

```bash
grep -rn "format_daemon_metrics\|/metrics" src/serve_engine/daemon/ | head
```

Expected match in `daemon/metrics_router.py` and/or `app.py`. Identify the FastAPI route serving `/metrics`.

- [ ] **Step 2: Write the failing test**

Create `tests/unit/test_observability_metrics.py`:

```python
from __future__ import annotations

from serve_engine.daemon.metrics_aggregator import MetricsAggregator
from serve_engine.observability.metrics import format_cluster_metrics


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
    assert "serve_node_gpu_util_pct{node=\"worker-a\",gpu=\"0\"} 42" in out
    assert "serve_node_gpu_mem_used_bytes{node=\"worker-a\",gpu=\"0\"} 1073741824" in out


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
    """Aggregator can have a node_id that isn't in the label map yet
    (race during enrollment). Fall back to the numeric id."""
    a = MetricsAggregator()
    a.ingest(node_id=99, sample=_sample(), ts=0.0)
    out = format_cluster_metrics(a, node_labels={})
    assert 'node="99"' in out
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_observability_metrics.py -v`
Expected: FAIL — `format_cluster_metrics` does not exist.

- [ ] **Step 4: Implement the formatter**

Add to `src/serve_engine/observability/metrics.py`:

```python
from serve_engine.daemon.metrics_aggregator import MetricsAggregator


_HELP_BLOCK = """\
# HELP serve_node_gpu_util_pct Per-GPU utilization percent.
# TYPE serve_node_gpu_util_pct gauge
# HELP serve_node_gpu_mem_used_bytes Per-GPU memory used in bytes.
# TYPE serve_node_gpu_mem_used_bytes gauge
# HELP serve_deployment_in_flight Per-deployment in-flight request count.
# TYPE serve_deployment_in_flight gauge
# HELP serve_deployment_requests_total Per-deployment requests in the last window.
# TYPE serve_deployment_requests_total counter
# HELP serve_deployment_latency_p50_ms Per-deployment p50 latency (ms).
# TYPE serve_deployment_latency_p50_ms gauge
# HELP serve_deployment_latency_p95_ms Per-deployment p95 latency (ms).
# TYPE serve_deployment_latency_p95_ms gauge
# HELP serve_deployment_errors_total Per-deployment error count in the last window.
# TYPE serve_deployment_errors_total counter
"""


def format_cluster_metrics(
    aggregator: MetricsAggregator,
    *,
    node_labels: dict[int, str],
) -> str:
    """Render the aggregator's current snapshot as Prometheus exposition.

    `node_labels` maps node_id → human label (the `label` column on the
    `nodes` table). Numeric fallback if a label is missing.
    """
    snap = aggregator.snapshot()
    if not snap:
        return ""
    lines: list[str] = [_HELP_BLOCK.rstrip()]
    for node_id, sample in sorted(snap.items()):
        node = node_labels.get(node_id, str(node_id))
        for g in sample.get("gpus", []):
            gpu = str(g.get("index", -1))
            lines.append(
                f'serve_node_gpu_util_pct{{node="{node}",gpu="{gpu}"}} '
                f'{int(g.get("util_pct", 0))}'
            )
            lines.append(
                f'serve_node_gpu_mem_used_bytes{{node="{node}",gpu="{gpu}"}} '
                f'{int(g.get("mem_used_mb", 0)) * 1024 * 1024}'
            )
        for d in sample.get("deployments", []):
            dep = str(d.get("deployment_id", -1))
            model = str(d.get("model_id", ""))
            tail = f'{{node="{node}",deployment="{dep}",model="{model}"}}'
            lines.append(f'serve_deployment_in_flight{tail} '
                         f'{int(d.get("in_flight", 0))}')
            lines.append(f'serve_deployment_requests_total{tail} '
                         f'{int(d.get("requests_last_window", 0))}')
            lines.append(f'serve_deployment_latency_p50_ms{tail} '
                         f'{int(d.get("latency_p50_ms", 0))}')
            lines.append(f'serve_deployment_latency_p95_ms{tail} '
                         f'{int(d.get("latency_p95_ms", 0))}')
            lines.append(f'serve_deployment_errors_total{tail} '
                         f'{int(d.get("errors_last_window", 0))}')
    return "\n".join(lines) + "\n"
```

- [ ] **Step 5: Hook it into the `/metrics` route**

Find the `/metrics` route handler (likely in `daemon/metrics_router.py`). Append the cluster metrics to the response body after the existing daemon/engine metrics. Roughly:

```python
from serve_engine.observability.metrics import format_cluster_metrics
from serve_engine.store import nodes as nodes_store

# inside the handler, after existing body is built:
agg = request.app.state.metrics_aggregator
node_labels = {n.id: n.label for n in nodes_store.list_all(conn)}
body += format_cluster_metrics(agg, node_labels=node_labels)
```

If the route doesn't currently take `request: Request`, add it.

- [ ] **Step 6: Run tests, verify pass**

Run: `pytest tests/unit/test_observability_metrics.py -v`
Expected: 4 passed.

Run: `pytest -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/serve_engine/observability/metrics.py src/serve_engine/daemon/metrics_router.py tests/unit/test_observability_metrics.py
git commit -m "feat(observability): prometheus exposition for cluster metrics"
```

---

## Task 8: Admin snapshot endpoint for the UI

**Files:**
- Modify: `src/serve_engine/daemon/admin.py`
- Test: `tests/unit/test_admin_metrics_snapshot.py` (new)

A read-only `GET /admin/metrics/snapshot` returns:

```json
{
  "nodes": [
    {
      "node_id": 1, "label": "worker-a",
      "gpus": [...],
      "deployments": [...],
      "series": {
        "gpu_util_pct": {"gpu0": [10,12,...], "gpu1": [...]},
        "request_rate": [3,5,4,...]
      }
    }
  ]
}
```

`series` is what the UI uses for sparklines.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_admin_metrics_snapshot.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from serve_engine.daemon import admin as admin_mod
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


@pytest.fixture
def client_with_agg():
    app = FastAPI()
    agg = MetricsAggregator()
    agg.ingest(node_id=1, sample=_sample(util=10), ts=10.0)
    agg.ingest(node_id=1, sample=_sample(util=20), ts=20.0)
    app.state.metrics_aggregator = agg
    # The admin router needs a nodes-store stub for labels. We stub it:
    app.state.list_nodes = lambda: [MagicMock(id=1, label="worker-a")]
    app.include_router(admin_mod.build_metrics_router())
    return TestClient(app)


def test_snapshot_returns_known_nodes(client_with_agg):
    r = client_with_agg.get("/admin/metrics/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert len(body["nodes"]) == 1
    n = body["nodes"][0]
    assert n["node_id"] == 1
    assert n["label"] == "worker-a"


def test_snapshot_includes_gpu_and_deployment_state(client_with_agg):
    body = client_with_agg.get("/admin/metrics/snapshot").json()
    n = body["nodes"][0]
    assert n["gpus"][0]["util_pct"] == 20  # latest sample
    assert n["deployments"][0]["in_flight"] == 3


def test_snapshot_includes_series_for_sparklines(client_with_agg):
    body = client_with_agg.get("/admin/metrics/snapshot").json()
    n = body["nodes"][0]
    assert n["series"]["gpu_util_pct"]["gpu0"] == [10, 20]
    assert n["series"]["request_rate"] == [5, 5]


def test_snapshot_empty_when_aggregator_empty():
    app = FastAPI()
    app.state.metrics_aggregator = MetricsAggregator()
    app.state.list_nodes = lambda: []
    app.include_router(admin_mod.build_metrics_router())
    client = TestClient(app)
    r = client.get("/admin/metrics/snapshot")
    assert r.status_code == 200
    assert r.json() == {"nodes": []}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_admin_metrics_snapshot.py -v`
Expected: FAIL — `build_metrics_router` does not exist.

- [ ] **Step 3: Implement the endpoint**

Add to `src/serve_engine/daemon/admin.py` (anywhere alongside the existing routers):

```python
from fastapi import APIRouter, Request


def build_metrics_router() -> APIRouter:
    """Read-only metrics snapshot for the UI. No mutation, no auth
    beyond what the parent admin router already enforces."""
    r = APIRouter()

    @r.get("/admin/metrics/snapshot")
    def snapshot(request: Request) -> dict:
        agg = request.app.state.metrics_aggregator
        labels = {n.id: n.label for n in request.app.state.list_nodes()}
        out = []
        for node_id, latest in sorted(agg.snapshot().items()):
            label = labels.get(node_id, str(node_id))
            series_gpu_util: dict[str, list[int]] = {}
            for g in latest.get("gpus", []):
                idx = g.get("index", -1)
                series_gpu_util[f"gpu{idx}"] = agg.series(
                    node_id=node_id, key="gpu_util_pct", gpu=idx,
                )
            out.append({
                "node_id": node_id,
                "label": label,
                "gpus": latest.get("gpus", []),
                "deployments": latest.get("deployments", []),
                "series": {
                    "gpu_util_pct": series_gpu_util,
                    "request_rate": agg.series(
                        node_id=node_id, key="request_rate",
                    ),
                },
            })
        return {"nodes": out}

    return r
```

In `app.py` where the admin router is mounted, also include this one:

```python
app.include_router(admin_mod.build_metrics_router())
```

And populate `app.state.list_nodes`:

```python
app.state.list_nodes = lambda: nodes_store.list_all(get_conn())
```

(Match the existing pattern for how `app.state` exposes DB-backed helpers.)

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_admin_metrics_snapshot.py -v`
Expected: 4 passed.

Run: `pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/daemon/admin.py src/serve_engine/daemon/app.py tests/unit/test_admin_metrics_snapshot.py
git commit -m "feat(daemon): GET /admin/metrics/snapshot for UI sparklines"
```

---

## Task 9: UI sparklines on the Cluster page

**Files:**
- Modify: `ui/src/views/Cluster.tsx`
- Modify: `ui/src/api.ts` (add the snapshot fetch)

This is a small additive UI change. No new screen.

- [ ] **Step 1: Add the API call**

In `ui/src/api.ts`, add:

```ts
export interface MetricsSnapshotNode {
  node_id: number
  label: string
  gpus: Array<{ index: number; mem_used_mb: number; mem_total_mb: number; util_pct: number }>
  deployments: Array<{ deployment_id: number; model_id: string; in_flight: number; latency_p95_ms: number }>
  series: {
    gpu_util_pct: Record<string, number[]>
    request_rate: number[]
  }
}

export interface MetricsSnapshot { nodes: MetricsSnapshotNode[] }

export async function fetchMetricsSnapshot(): Promise<MetricsSnapshot> {
  const r = await fetch("/admin/metrics/snapshot")
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json()
}
```

- [ ] **Step 2: Add a minimal inline sparkline component**

In `ui/src/views/Cluster.tsx`, add at the top of the file (above the component):

```tsx
function Sparkline({ values, width = 80, height = 20 }: {
  values: number[]; width?: number; height?: number;
}) {
  if (values.length === 0) return <span style={{ color: "#888" }}>—</span>
  const max = Math.max(1, ...values)
  const step = width / Math.max(1, values.length - 1)
  const points = values.map((v, i) =>
    `${i * step},${height - (v / max) * height}`
  ).join(" ")
  return (
    <svg width={width} height={height} aria-label="sparkline">
      <polyline fill="none" stroke="currentColor" strokeWidth="1" points={points} />
    </svg>
  )
}
```

No dependency — pure SVG. Keeps the UI bundle untouched.

- [ ] **Step 3: Wire the polling**

In the existing `Cluster` component, add a `useEffect` that polls every 5 s and stores the snapshot in component state. Read the existing patterns in this file — match how `Cluster.tsx` already fetches nodes (likely via `useQuery` or a `useEffect` with `setInterval`). Use the same approach.

- [ ] **Step 4: Render sparklines on node cards**

In the node card render, next to or under the existing GPU display, insert per-GPU util sparklines and a request-rate sparkline. Sketch:

```tsx
{snapshot?.nodes.find(n => n.node_id === node.id)?.series.gpu_util_pct &&
  Object.entries(snapshot.nodes.find(n => n.node_id === node.id)!.series.gpu_util_pct).map(
    ([gpuLabel, values]) => (
      <div key={gpuLabel}>
        <span>{gpuLabel} util</span>
        <Sparkline values={values} />
      </div>
    )
  )
}
<div>
  <span>req/s</span>
  <Sparkline values={snapshot?.nodes.find(n => n.node_id === node.id)?.series.request_rate ?? []} />
</div>
```

Read the actual structure of `Cluster.tsx` before editing — match its existing className conventions and don't introduce new style patterns.

- [ ] **Step 5: Verify in dev**

Bring up the UI dev server (per `ui/package.json`, likely `npm run dev`), point at a running leader with at least one connected agent, and confirm:
- Each node card renders one or more sparklines.
- Sparklines update every 5 s.
- A node with no metrics yet (just connected) shows the em-dash placeholder.

If you cannot test the UI live (no GPU host available), explicitly say so in the commit message rather than claiming the UI works.

- [ ] **Step 6: Commit**

```bash
git add ui/src/views/Cluster.tsx ui/src/api.ts
git commit -m "feat(ui): live sparklines on node cards"
```

---

## Task 10: Integration test — leader + two fake agents

**Files:**
- Create: `tests/integration/test_metrics_aggregation.py`

End-to-end test. Stands up a leader, connects two fake agents (no real WS, just calls the hub handlers directly), pushes synthetic metrics, asserts the snapshot endpoint and `/metrics` both reflect them.

- [ ] **Step 1: Write the test**

Create `tests/integration/test_metrics_aggregation.py`:

```python
from __future__ import annotations

import pytest

from serve_engine.cluster.protocol import Heartbeat
from serve_engine.daemon.metrics_aggregator import MetricsAggregator
from serve_engine.observability.metrics import format_cluster_metrics


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


def test_two_agents_to_one_aggregator_through_hub_handler():
    """Drive heartbeats through LeaderHub._handle_heartbeat directly
    rather than over a real WS — keeps the test fast and deterministic."""
    from unittest.mock import MagicMock
    from serve_engine.cluster.leader_hub import LeaderHub

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
    assert snap[1]["gpus"][0]["util_pct"] == 30  # latest wins
    assert snap[2]["gpus"][0]["util_pct"] == 50

    out = format_cluster_metrics(agg, node_labels={1: "a", 2: "b"})
    assert 'serve_node_gpu_util_pct{node="a",gpu="0"} 30' in out
    assert 'serve_node_gpu_util_pct{node="b",gpu="0"} 50' in out


def test_node_disconnect_evicts_aggregator_entry():
    from unittest.mock import MagicMock
    from serve_engine.cluster.leader_hub import LeaderHub

    agg = MetricsAggregator()
    hub = LeaderHub(
        conn=MagicMock(), registry=MagicMock(),
        fingerprint_resolver=lambda ws: "fp", aggregator=agg,
    )
    hub._handle_heartbeat(node_id=1, frame=Heartbeat(ts=10.0, metrics=_sample(20)))
    assert agg.snapshot() != {}
    # Simulate disconnect path:
    agg.drop_node(1)
    assert agg.snapshot() == {}
```

- [ ] **Step 2: Run the integration test**

Run: `pytest tests/integration/test_metrics_aggregation.py -v`
Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_metrics_aggregation.py
git commit -m "test(cluster): end-to-end metrics aggregation via hub handler"
```

---

## Task 11: Sample Grafana dashboard + docs update

**Files:**
- Create: `docs/dashboards/serve-engine.json`
- Modify: `docs/multi-node.md`

The dashboard is a starting point for operators, not a product. Document what's exposed and the import path.

- [ ] **Step 1: Create the dashboard JSON**

Create `docs/dashboards/serve-engine.json`. Keep it minimal — 4 panels:

```json
{
  "title": "serve-engine cluster",
  "schemaVersion": 38,
  "panels": [
    {
      "id": 1, "title": "GPU utilization", "type": "timeseries",
      "targets": [{"expr": "serve_node_gpu_util_pct", "legendFormat": "{{node}} gpu{{gpu}}"}]
    },
    {
      "id": 2, "title": "In-flight requests", "type": "timeseries",
      "targets": [{"expr": "sum by (node, deployment) (serve_deployment_in_flight)",
                   "legendFormat": "{{node}} dep{{deployment}}"}]
    },
    {
      "id": 3, "title": "p95 latency (ms)", "type": "timeseries",
      "targets": [{"expr": "serve_deployment_latency_p95_ms",
                   "legendFormat": "{{node}} {{model}}"}]
    },
    {
      "id": 4, "title": "Error rate", "type": "timeseries",
      "targets": [{"expr": "rate(serve_deployment_errors_total[1m])",
                   "legendFormat": "{{node}} {{model}}"}]
    }
  ]
}
```

- [ ] **Step 2: Add an "Observability" section to `docs/multi-node.md`**

Append a new section. The content should cover:

1. What metrics are exposed (the seven series with their labels).
2. Where to scrape them (`GET /metrics` on the leader).
3. How to import the dashboard (`docs/dashboards/serve-engine.json` → Grafana import).
4. The UI snapshot endpoint and that the Cluster page shows live sparklines.

Roughly 30-60 lines, matching the existing tone of `docs/multi-node.md`. Read the file first to match its structure.

- [ ] **Step 3: Commit**

```bash
git add docs/dashboards/serve-engine.json docs/multi-node.md
git commit -m "docs: observability surface (prometheus series + sample dashboard)"
```

---

## Self-Review Output

**1. Spec coverage**

- Heartbeat schema extension — Task 1.
- Sampling (GPU, in-flight, latency, node) — Tasks 2, 3, 4.
- Aggregator (60 s ring, snapshot/series/drop_node) — Task 5.
- Aggregator wired into hub heartbeat handler — Task 6.
- Prometheus exposition (seven series, stable labels) — Task 7.
- Admin snapshot endpoint — Task 8.
- UI sparklines (no new screen) — Task 9.
- Integration test — Task 10.
- Dashboard JSON + docs — Task 11.
- CLI `serve metrics tail` — **NOT in this plan.** It depends on the snapshot endpoint and is a small CLI follow-up; deferred to keep this plan focused on the data plane. Worth raising at handoff.

**2. Placeholder scan** — clean. Every step has either code or an exact command.

**3. Type consistency**

- `InFlightCounter.start(deployment_id)` / `finish(deployment_id)` — consistent in collector, proxy, aggregator tests.
- `LatencyRecorder.record(deployment_id=, latency_ms=, error=)` — consistent.
- `MetricsAggregator.ingest(node_id=, sample=, ts=)` — consistent in hub, tests, integration test.
- `MetricsAggregator.snapshot()` returns `dict[int, dict[str, Any]]` — consistent across consumers.
- `format_cluster_metrics(aggregator, *, node_labels)` — consistent.

**Out-of-plan items raised to handoff:**
- `serve metrics tail` CLI command (small, easy follow-up).
- `temp_c` and `host_load_avg_1m` stubbed to 0 in the collector; trivial to fill in but out of scope here.
- Remote-agent process's *own* OpenAI proxy needs the in-flight + latency wiring too. Today (per the codebase), only the leader runs the proxy; remote agents forward via `proxy_request`. So this plan covers the leader; remote-side instrumentation is unblocked once they grow a local proxy.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-19-observability-data-plane.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

A companion plan (`2026-05-19-smart-routing-and-resilience.md`) covers the placement scorer, retry dispatcher, SSE backpressure, and the `lifecycle/dispatch.py` carve-out, building on the data plane above.
