# Load-aware multi-node serving with request-level resilience

**Status:** draft
**Date:** 2026-05-19
**Depends on:** [2026-05-18-multi-node-serving-design.md](2026-05-18-multi-node-serving-design.md), [2026-05-18-secure-by-default-cluster-transport-design.md](2026-05-18-secure-by-default-cluster-transport-design.md)

## Summary

The multi-node story landed end-to-end: agents enroll over mTLS, the
leader dispatches deployments to remote nodes, the router filters out
unreachable ones. What it does not yet do is route *intelligently*, fail
*gracefully*, or *expose* enough signal to debug or operate. This spec
adds three things on top of the existing cluster transport:

1. A metrics data plane — per-node and per-deployment signal flowing
   from each agent back to the leader, surfaced via Prometheus and the
   existing UI.
2. A placement scorer — replaces the current candidate-selection-only
   placement with a small, explainable scoring function that prefers
   warm caches and lighter-loaded nodes.
3. Request-level resilience — bounded retry across distinct nodes for
   pre-first-token failures, SSE backpressure between proxy and client,
   and node-loss request audit logging.

## Goals

- Per-node + per-deployment metrics aggregated on the leader.
- Prometheus exposition at `/metrics` with stable, well-labeled series.
- UI surfaces (Nodes page, GPU cards, deployment cards) read live metrics
  without new screens.
- Placement scores candidates explicitly on `mem_headroom`, `in_flight`,
  `affinity`, `recent_latency`.
- A session-key affinity scheme keeps follow-up requests on the node
  whose KV cache already holds the conversation prefix.
- Pre-first-token retries try the next-ranked candidate on transient
  errors. Bounded budget, distinct nodes.
- SSE proxy backpressure: a slow client pauses the engine instead of
  buffering unbounded.
- Tests cover scorer ordering, retry classification, and a
  kill-the-node-mid-dispatch integration case.

## Non-goals

- Autoscaling, scale-to-zero, or any request-rate-driven deployment
  count change. Future spec, depends on this.
- Predictive placement, ML-trained scoring, or any "magic" beyond an
  explainable scorer.
- Workload migration of running deployments. The existing multi-node
  spec already punted on this; we keep that punt.
- Mid-stream failover. A node dying mid-generation propagates the
  error. Documented limitation.
- Security / cert-rotation work. Out of scope.
- A second persistence layer for metrics — Prometheus is the TSDB.

## Architecture

Three layers, each isolated, each communicating through one interface:

```
                ┌────────────────────────────────────────┐
                │ leader                                  │
   metrics ───► │  metrics_aggregator  ──► prom exporter │
   from agents  │           │             ──► UI         │
   (heartbeat)  │           ▼                            │
                │       placement.scorer ──► router      │
                │                              │         │
                │                              ▼         │
                │                       retry_dispatcher │
                └────────────────────────────────────────┘
                            ▲                  │
                            │ (samples)        │ (dispatch)
                            │                  ▼
                   ┌──────────────────────────────────┐
                   │ agent (per node)                  │
                   │  metrics_collector ──► WS frames  │
                   └──────────────────────────────────┘
```

Three new units, each with one job:

- `cluster/metrics_collector.py` (agent side) — samples GPU stats, local
  deployment in-flight counts, recent latency. Emits a snapshot on each
  heartbeat tick.
- `placement/scorer.py` (leader side) — a pure function:
  `(candidates, signals, request) → ranked list`. Pluggable; one default
  ships.
- `daemon/retry_dispatcher.py` (leader side) — wraps the existing
  dispatch path with bounded, pre-first-token retry across the ranked
  candidate list.

Three existing units gain new responsibilities:

- `cluster/protocol.py` — heartbeat frame schema extended with a
  `metrics` field.
- `daemon/admin.py` — new read-only `/admin/metrics/snapshot` endpoint
  the UI consumes.
- `observability/metrics.py` — registers the new gauges and histograms
  for Prometheus exposition.

One incidental refactor (see below): `lifecycle/manager.py` is carved to
extract `lifecycle/dispatch.py`.

## Observability data plane

### Sampling (agent side)

Every heartbeat interval (default 5 s), the agent samples:

**GPU**, per device:
- `index`, `mem_used_mb`, `mem_total_mb`, `util_pct`, `temp_c`.

**Per local deployment**:
- `deployment_id`, `model_id`, `in_flight`, `requests_last_window`,
  `latency_p50_ms`, `latency_p95_ms`, `errors_last_window`.

**Node-level**:
- `agent_version`, `uptime_s`, `host_load_avg_1m`.

Window for "last_window" stats is the heartbeat interval. The collector
is a small wrapper around `observability/gpu_stats.py` (already exists)
plus an in-process counter the OpenAI proxy increments. No new IPC.

### Transport

The metrics payload piggybacks on the existing heartbeat WS frame —
`cluster/protocol.py` `HeartbeatFrame` gains an optional `metrics`
field. Payload size at 8 GPUs + 16 deployments is under 4 KB; well
under the per-frame budget. If this stops being true we move to a
separate channel; until then, one channel is simpler.

### Aggregation (leader side)

A new `daemon/metrics_aggregator.py`:

- Keeps a ring buffer per `(node, sample)` of the last 12 samples
  (60 s at 5 s heartbeat). In-memory only.
- Provides three reads:
  - `snapshot()` — latest sample for every known node, for UI + admin.
  - `prometheus_export()` — flattens current state into the registry
    for `/metrics`.
  - `query(node, deployment, key, window)` — for the scorer and CLI.
- Drops state for nodes that transition to `gone`.

The aggregator is stateless across restarts. On leader restart we lose
the last 60 s of trend data; agents resume sending immediately and the
buffer refills in one window.

### Prometheus surface

Registered in `observability/metrics.py`. New series:

```
serve_node_gpu_mem_used_bytes{node="...", gpu="0"}   gauge
serve_node_gpu_util_pct{node="...", gpu="0"}         gauge
serve_deployment_in_flight{node, deployment, model}  gauge
serve_deployment_requests_total{...}                 counter
serve_deployment_latency_ms_bucket{...}              histogram
serve_deployment_errors_total{...}                   counter
serve_dispatch_retries_total{node, reason}           counter
serve_router_affinity_hits_total{}                   counter
```

Stable labels. Existing series are not renamed.

### UI surface

No new screens. Three existing surfaces get richer:

- **Nodes page**: each card gets two sparklines (GPU util %, request
  rate) and a current in-flight number per deployment. Reads from
  `/admin/metrics/snapshot`.
- **GPU cards**: existing util/mem readouts now show "current" from the
  aggregator instead of last cached.
- **Deployment cards**: gain in-flight count and recent p95.

## Placement scorer

### Interface

```python
def score_candidates(
    candidates: list[DeploymentCandidate],
    signals: NodeSignals,
    request: RoutingRequest,
) -> list[DeploymentCandidate]:
    """Return candidates ranked best-first. Pure function."""
```

The router calls this once per request. Returns a *full ranking*, not a
single pick — `retry_dispatcher` consumes the tail when the head fails.

### Default scorer

Two-step. Hard filter, then lexicographic rank.

1. **Hard filter**: drop any candidate where the model would not fit
   in `mem_total_mb - mem_used_mb - safety_margin_mb`. Default safety
   margin is 1024 MB.
2. **Rank** by tuple, larger-is-better:
   - `affinity_hit` (1 if the routing key already maps to this node, 0
     otherwise),
   - `-in_flight` (less loaded first),
   - `-p95_latency_ms` (faster recent performance first).

Explicit, debuggable, easy to test. No floats summed with weights — the
moment we sum weighted floats we have a calibration problem and no
clear way to debug "why did it pick this node."

### Affinity key

Determined in the router, in this priority order:

1. `X-Session-Id` header if present.
2. `X-Conversation-Id` header if present.
3. The API key.
4. None (round-robin via stable hash of request id).

The leader keeps an in-memory `routing_affinity: dict[key, node_id]`
map. On successful dispatch, it's set to that node. On the node
transitioning to `unreachable` or `gone`, all entries pointing there are
evicted. The map is bounded (LRU, default 10 000 entries) and lost on
restart — affinity is best-effort, not a contract.

### Pluggability

The scorer is a callable injected into the router at startup. The
default lives at `placement/scorer.py`. An entry point in `config.py`
(`placement.scorer = "serve_engine.placement.scorer:default"`) lets an
operator point at a custom one without code changes. We ship one
scorer in v1; writing a second is out of scope.

## Failure handling

### Pre-first-token retry

`daemon/retry_dispatcher.py` wraps the dispatch path:

```python
async def dispatch_with_retry(
    candidates: list[DeploymentCandidate],
    request: ProxyRequest,
    *,
    budget: int = 2,
) -> ProxyResponse:
    tried: set[NodeId] = set()
    for candidate in candidates:
        if candidate.node_id in tried:
            continue
        tried.add(candidate.node_id)
        try:
            return await dispatch(candidate, request)
        except RetryableError as e:
            if len(tried) > budget or _bytes_sent(request):
                raise
            metrics.dispatch_retries_total.labels(
                node=candidate.node_id, reason=e.reason).inc()
            continue
    raise NoCandidatesError(...)
```

Classification:
- **Retryable, pre-first-token**: connection refused, connection reset,
  HTTP 502/503/504 with no body started, timeout before headers,
  agent went `unreachable` between candidate selection and dispatch.
- **Non-retryable**: any 4xx, any 5xx after first byte to client, any
  error after first SSE event sent.

The "no bytes sent" check is the only thing that makes this safe. The
proxy must surface that bit to the dispatcher — wire it through the
existing proxy plumbing rather than re-implementing buffering.

Budget is 2 retries by default (3 total attempts). Configurable via
`config.py`.

### SSE backpressure

Today the OpenAI proxy reads from the engine and writes to the client
through unbounded queues. A slow client buffers in three places: kernel
send buffer, asyncio write queue, engine's generation ring.

The change is small:

- A bounded `asyncio.Queue(maxsize=N)` (default 64 chunks) between the
  engine reader task and the client writer task.
- When the queue is full, the reader task awaits on `put()`. This
  applies backpressure all the way back to the WS transport, which in
  turn applies backpressure to the engine.

This needs no new component — it's a 20-line change in
`daemon/openai_proxy.py`. The number is a knob; we don't auto-tune.

### Node-loss audit

When `cluster/health_watcher.py` transitions a node to `unreachable`,
it now logs (existing logger, no new infra) every in-flight request
that the leader knows about for that node:

```
node_loss_audit node=worker-2 in_flight=4
  request_id=abc deployment=llama3-8b duration_ms=1820
  request_id=def deployment=llama3-8b duration_ms=210
  ...
```

This is purely an operability aid — no behavior change, no new state.

### Out of scope: mid-stream failover

A node dying mid-generation cannot be resumed elsewhere without
KV-cache transfer. We do not do that. Errors after first byte propagate
to the client. Documented in `docs/multi-node.md`.

## State

Two new in-memory maps on the leader. Nothing on disk.

- `metrics_aggregator.samples: dict[node_id, deque[Sample]]` — ring of
  the last 12 samples per node.
- `router.routing_affinity: LRU[str, node_id]` — bounded affinity map.

Both reset on leader restart. Both clear entries on node `gone`. No
migrations.

## Refactor scope

`lifecycle/manager.py` is 698 lines. The dispatch path within it is
about to grow (retry, classification). Carve along the natural
boundary:

- `lifecycle/dispatch.py` — `dispatch(candidate, request)`, error
  classification helpers, the remote-vs-local branching that
  `manager.py` currently inlines.
- `lifecycle/manager.py` — stays as the orchestrator: lifecycle plan,
  state transitions, reconciliation, the deployment lifecycle FSM.

`retry_dispatcher.py` lives in `daemon/`, not `lifecycle/`, because
retry is a request-level concern, not a deployment-lifecycle concern.

No other refactor. Not a free-form cleanup pass.

## CLI / UI surface

### CLI additions

- `serve nodes show <label>` — gains a "Live" section with current GPU
  util, in-flight per deployment, last p95. One-shot read of the
  aggregator snapshot.
- `serve metrics tail [--node X] [--deployment Y]` — follows the
  snapshot, refreshing every heartbeat interval. Cheap; reads the same
  in-memory data.

No new top-level commands beyond `metrics`.

### UI additions

Covered above under "UI surface." No new pages.

### Prometheus

`/metrics` becomes the canonical scrape target. A sample dashboard JSON
(Grafana) ships in `docs/dashboards/serve-engine.json`. Not auto-deployed;
operators import it themselves.

## Testing

### Unit

- `placement/scorer.py` — full coverage of:
  - Hard filter: too-small node dropped, just-fits node kept.
  - Affinity hit beats lower load.
  - Equal affinity → lower in_flight wins.
  - Equal affinity + in_flight → lower p95 wins.
  - Missing signals (new node, no samples yet) — treated as worst-case
    for that dimension, never crashes.
- `daemon/retry_dispatcher.py` — mocked dispatch:
  - Retryable error pre-first-token → retries on next candidate.
  - Non-retryable error → propagates immediately.
  - Same node never retried twice.
  - Budget exhausted → propagates last error.
  - Bytes already sent → no retry, propagates.

### Integration

- `tests/integration/test_metrics_aggregation.py` — start leader +
  two fake agents emitting synthetic metrics. Assert:
  - `/admin/metrics/snapshot` returns both nodes.
  - `/metrics` Prometheus output contains expected series with
    correct labels.
  - Snapshot updates within one heartbeat interval.
- `tests/integration/test_dispatch_retry_e2e.py` — leader + two
  agents. Kill agent A between candidate selection and dispatch.
  Assert client sees success and the request landed on agent B.
- Existing `test_leader_remote_agent_e2e.py` gets one new assertion:
  metrics frames arrive on each heartbeat.

### Performance sanity

Not a benchmark suite. One scripted check in `tests/perf/` that
confirms scoring 100 candidates against synthetic signals takes under
1 ms (sanity check against accidental O(n²)).

## Migration / backwards compatibility

- Heartbeat frames without a `metrics` field are accepted (existing
  field is optional). Old agents continue to work; their nodes just
  show "metrics unavailable" in the UI until they upgrade.
- Existing Prometheus series are unchanged. New series are additive.
- The placement entry point defaults to the new default scorer. Single-
  node deployments behave identically (one candidate, trivially ranked).
- No database migration.

## File layout (anticipated)

```
src/serve_engine/
  cluster/
    metrics_collector.py    NEW   agent-side sampler
    protocol.py             EDIT  HeartbeatFrame.metrics field
  daemon/
    metrics_aggregator.py   NEW   leader-side ring + sinks
    retry_dispatcher.py     NEW   pre-first-token retry wrapper
    openai_proxy.py         EDIT  bounded queue for backpressure
    admin.py                EDIT  /admin/metrics/snapshot
  lifecycle/
    dispatch.py             NEW   carved from manager.py
    manager.py              EDIT  trimmed
  observability/
    metrics.py              EDIT  new gauges/counters/histograms
  placement/
    scorer.py               NEW   default scorer + pluggable interface
  config.py                 EDIT  scorer entry point, retry budget,
                                  queue size knobs
ui/src/views/
  Cluster.tsx               EDIT  sparklines on node cards
  ...                       (deployment cards in existing views)
docs/
  multi-node.md             EDIT  document affinity, retry, backpressure,
                                  mid-stream-failure limitation
  dashboards/
    serve-engine.json       NEW   sample Grafana dashboard
tests/
  unit/
    test_placement_scorer.py        NEW
    test_retry_dispatcher.py        NEW
    test_metrics_aggregator.py      NEW
  integration/
    test_metrics_aggregation.py     NEW
    test_dispatch_retry_e2e.py      NEW
  perf/
    test_scorer_perf.py             NEW
```

## Open questions to resolve at planning time

These are implementation-shape questions, not architecture questions.

1. **Heartbeat payload size** — if 8 GPUs + many deployments pushes the
   frame past ~16 KB we want a separate channel. Plan should measure
   once before deciding.
2. **Affinity key precedence in the OpenAI proxy** — `X-Session-Id` is
   the proposed first key; if the OpenAI-compatible spec or downstream
   clients standardize on something different, prefer the standard.
3. **Backpressure queue depth (default 64)** — needs a sanity check
   against typical SSE chunk size and a slow-client scenario. May tune
   once.
4. **Retry budget across `dispatch_retries_total` cardinality** — the
   `reason` label values must be a closed set (~5 strings) to keep
   cardinality bounded.

## Build sequence

A rough order — the implementation plan refines this:

1. Heartbeat schema + agent collector + leader aggregator + Prometheus
   exposition. Validate the data plane end-to-end before anything
   depends on it.
2. Admin snapshot endpoint + UI sparklines. Confirm the surface works
   for operators.
3. Carve `lifecycle/dispatch.py` out of `manager.py`. Tests stay
   green.
4. Placement scorer (pure function, no router wiring yet). Unit tests.
5. Wire scorer into the router. Single-node still works.
6. `retry_dispatcher` + classification + integration test.
7. SSE backpressure.
8. Docs + dashboard JSON + audit logging polish.
