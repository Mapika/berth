# Smart Routing & Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace candidate-selection-only routing with a load-aware scorer, add bounded pre-first-token retries across distinct nodes, and apply SSE backpressure between the engine and slow clients.

**Architecture:** A new `routing/scorer.py` (pure function: candidates + signals → ranked list) replaces today's first-match selection. A `daemon/dispatch.py` carved out of the inlined dispatch in `openai_proxy.py` becomes the retry unit. A `daemon/retry_dispatcher.py` walks the ranked candidate list, retries on classified transient errors only when no bytes have been sent. A small bounded `asyncio.Queue` in the streamer applies backpressure.

**Tech Stack:** Python 3.13, FastAPI, asyncio, httpx, pytest.

**Spec:** `docs/superpowers/specs/2026-05-19-load-aware-multi-node-serving-design.md`

**Depends on:** `2026-05-19-observability-data-plane.md` (this plan reads the aggregator built there).

---

## File Structure

**New files:**
- `src/serve_engine/routing/__init__.py` — empty.
- `src/serve_engine/routing/scorer.py` — pure scoring function and the default scorer.
- `src/serve_engine/routing/affinity.py` — bounded LRU `routing_affinity` map.
- `src/serve_engine/daemon/dispatch.py` — carved-out request dispatch (remote vs local).
- `src/serve_engine/daemon/dispatch_errors.py` — `RetryableError` + classifier.
- `src/serve_engine/daemon/retry_dispatcher.py` — pre-first-token retry over ranked candidates.

**New tests:**
- `tests/unit/test_routing_scorer.py`
- `tests/unit/test_routing_affinity.py`
- `tests/unit/test_dispatch_errors.py`
- `tests/unit/test_retry_dispatcher.py`
- `tests/integration/test_dispatch_retry_e2e.py`

**Modified:**
- `src/serve_engine/lifecycle/adapter_router.py` — adds `rank_deployments_for`, keeps `find_deployment_for` as a thin wrapper that returns the head.
- `src/serve_engine/daemon/openai_proxy.py` — uses the ranked list + `retry_dispatcher`; threads the dispatch unit; adds the backpressure queue.
- `src/serve_engine/cluster/health_watcher.py` — emits the node-loss audit log.
- `src/serve_engine/config.py` — `retry_budget`, `sse_queue_depth`, `scorer_entry_point` knobs.
- `docs/multi-node.md` — affinity, retry, backpressure, mid-stream-failure limitation.

**Boundaries:** `scorer` knows nothing about HTTP or WebSockets — it operates on dataclasses. `dispatch` knows nothing about scoring — it gets a single candidate and returns a streaming response (or raises). `retry_dispatcher` is the only place that knows both.

---

## Task 1: `RoutingSignals` and `RankedCandidate` dataclasses + the scorer interface

**Files:**
- Create: `src/serve_engine/routing/__init__.py` (empty)
- Create: `src/serve_engine/routing/scorer.py`
- Test: `tests/unit/test_routing_scorer.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_routing_scorer.py`:

```python
from __future__ import annotations

import pytest

from serve_engine.routing.scorer import (
    DeploymentCandidate,
    NodeSignals,
    RoutingRequest,
    default_scorer,
)


def _cand(*, deployment_id, node_id, model_required_mb=8000):
    return DeploymentCandidate(
        deployment_id=deployment_id,
        node_id=node_id,
        model_required_mb=model_required_mb,
    )


def _signals(
    *, node_id, mem_free_mb=20000, in_flight=0, p95=100,
):
    return NodeSignals(
        node_id=node_id,
        mem_free_mb=mem_free_mb,
        in_flight=in_flight,
        latency_p95_ms=p95,
    )


def test_scorer_returns_empty_when_no_candidates():
    out = default_scorer(
        candidates=[],
        signals_by_node={},
        request=RoutingRequest(affinity_key=None),
    )
    assert out == []


def test_scorer_drops_candidates_without_memory_headroom():
    candidates = [
        _cand(deployment_id=1, node_id=10, model_required_mb=8000),
        _cand(deployment_id=2, node_id=11, model_required_mb=8000),
    ]
    signals = {
        10: _signals(node_id=10, mem_free_mb=2000),     # too small
        11: _signals(node_id=11, mem_free_mb=20000),    # fits
    }
    out = default_scorer(
        candidates=candidates, signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    assert [c.deployment_id for c in out] == [2]


def test_scorer_ranks_lower_in_flight_first():
    candidates = [
        _cand(deployment_id=1, node_id=10),
        _cand(deployment_id=2, node_id=11),
    ]
    signals = {
        10: _signals(node_id=10, in_flight=8),
        11: _signals(node_id=11, in_flight=2),
    }
    out = default_scorer(
        candidates=candidates, signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    assert [c.deployment_id for c in out] == [2, 1]


def test_scorer_ranks_lower_p95_when_in_flight_is_tied():
    candidates = [
        _cand(deployment_id=1, node_id=10),
        _cand(deployment_id=2, node_id=11),
    ]
    signals = {
        10: _signals(node_id=10, in_flight=4, p95=500),
        11: _signals(node_id=11, in_flight=4, p95=120),
    }
    out = default_scorer(
        candidates=candidates, signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    assert [c.deployment_id for c in out] == [2, 1]


def test_scorer_prefers_affinity_hit_over_lower_load():
    candidates = [
        _cand(deployment_id=1, node_id=10),
        _cand(deployment_id=2, node_id=11),
    ]
    signals = {
        10: _signals(node_id=10, in_flight=8),  # affinity hit, but loaded
        11: _signals(node_id=11, in_flight=0),  # idle but no affinity
    }
    out = default_scorer(
        candidates=candidates, signals_by_node=signals,
        request=RoutingRequest(affinity_key="sess-abc", affinity_node_id=10),
    )
    assert [c.deployment_id for c in out] == [1, 2]


def test_scorer_treats_missing_signals_as_worst_case():
    """A node with no aggregator entry yet (freshly enrolled) shouldn't
    crash the scorer; it ranks last."""
    candidates = [
        _cand(deployment_id=1, node_id=10),
        _cand(deployment_id=2, node_id=11),
    ]
    signals = {10: _signals(node_id=10, in_flight=4)}
    # node 11 has no entry.
    out = default_scorer(
        candidates=candidates, signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    # Node 11 has no signal → assumed worst case → ranks last.
    assert out[0].deployment_id == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_routing_scorer.py -v`
Expected: FAIL — `ModuleNotFoundError: serve_engine.routing.scorer`.

- [ ] **Step 3: Implement the scorer**

Create `src/serve_engine/routing/__init__.py` (empty).

Create `src/serve_engine/routing/scorer.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

# Anything below this is treated as "doesn't fit" — leaves headroom for
# CUDA fragmentation and the activation memory you can't predict from
# weights alone. Tuneable later via config.
SAFETY_MARGIN_MB = 1024


@dataclass(frozen=True)
class DeploymentCandidate:
    deployment_id: int
    node_id: int
    model_required_mb: int


@dataclass(frozen=True)
class NodeSignals:
    """Aggregator-derived snapshot for one node, at scoring time."""
    node_id: int
    mem_free_mb: int
    in_flight: int
    latency_p95_ms: int


@dataclass(frozen=True)
class RoutingRequest:
    affinity_key: str | None
    affinity_node_id: int | None = None


def default_scorer(
    *,
    candidates: list[DeploymentCandidate],
    signals_by_node: dict[int, NodeSignals],
    request: RoutingRequest,
) -> list[DeploymentCandidate]:
    """Return candidates ranked best-first.

    Hard filter: drop candidates whose model wouldn't fit in (mem_free -
    safety_margin) on their node. Missing signals treat the node as
    worst-case: `mem_free=0`, `in_flight=infinity`, `p95=infinity`. That
    pushes new/unknown nodes to the end of the ranking without dropping
    them entirely.
    """
    if not candidates:
        return []

    scored: list[tuple[tuple, DeploymentCandidate]] = []
    for c in candidates:
        s = signals_by_node.get(c.node_id)
        if s is None:
            # No signal → worst-case but still kept (just-enrolled
            # nodes shouldn't be unreachable from the scorer's POV).
            mem_free = 0
            in_flight = 10**9
            p95 = 10**9
        else:
            mem_free = s.mem_free_mb
            in_flight = s.in_flight
            p95 = s.latency_p95_ms
        # Hard filter on memory headroom.
        if mem_free - SAFETY_MARGIN_MB < c.model_required_mb:
            continue
        # Affinity hit: 1 if the leader's routing map points here.
        affinity_hit = int(
            request.affinity_node_id is not None
            and c.node_id == request.affinity_node_id
        )
        # Lexicographic, larger-is-better.
        key = (affinity_hit, -in_flight, -p95)
        scored.append((key, c))

    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [c for _, c in scored]
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_routing_scorer.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/routing/__init__.py src/serve_engine/routing/scorer.py tests/unit/test_routing_scorer.py
git commit -m "feat(routing): default scorer (hard-filter + lexicographic rank)"
```

---

## Task 2: Routing-affinity map (bounded LRU)

**Files:**
- Create: `src/serve_engine/routing/affinity.py`
- Test: `tests/unit/test_routing_affinity.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_routing_affinity.py`:

```python
from __future__ import annotations

from serve_engine.routing.affinity import RoutingAffinity


def test_empty_lookup_returns_none():
    a = RoutingAffinity(capacity=4)
    assert a.lookup("sess-a") is None


def test_set_then_lookup():
    a = RoutingAffinity(capacity=4)
    a.set("sess-a", node_id=10)
    assert a.lookup("sess-a") == 10


def test_lru_eviction_when_full():
    a = RoutingAffinity(capacity=2)
    a.set("k1", node_id=10)
    a.set("k2", node_id=11)
    a.set("k3", node_id=12)   # evicts k1
    assert a.lookup("k1") is None
    assert a.lookup("k2") == 11
    assert a.lookup("k3") == 12


def test_lookup_promotes_recency():
    a = RoutingAffinity(capacity=2)
    a.set("k1", node_id=10)
    a.set("k2", node_id=11)
    _ = a.lookup("k1")        # promotes k1
    a.set("k3", node_id=12)   # evicts k2 (was LRU after the lookup)
    assert a.lookup("k1") == 10
    assert a.lookup("k2") is None


def test_evict_node_drops_all_pointing_entries():
    a = RoutingAffinity(capacity=4)
    a.set("k1", node_id=10)
    a.set("k2", node_id=10)
    a.set("k3", node_id=11)
    a.evict_node(10)
    assert a.lookup("k1") is None
    assert a.lookup("k2") is None
    assert a.lookup("k3") == 11
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_routing_affinity.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/serve_engine/routing/affinity.py`:

```python
from __future__ import annotations

import threading
from collections import OrderedDict


class RoutingAffinity:
    """Bounded LRU `affinity_key → node_id` map.

    Best-effort. Lost on process restart. Cleared per-node on node loss.
    Thread-safe with a single lock — this is a hot path on every
    request, but the lock is held for microseconds.
    """

    def __init__(self, *, capacity: int = 10_000) -> None:
        self._capacity = capacity
        self._data: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def lookup(self, key: str) -> int | None:
        with self._lock:
            v = self._data.get(key)
            if v is not None:
                # Promote on read.
                self._data.move_to_end(key)
            return v

    def set(self, key: str, *, node_id: int) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = node_id
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)

    def evict_node(self, node_id: int) -> None:
        with self._lock:
            to_drop = [k for k, v in self._data.items() if v == node_id]
            for k in to_drop:
                self._data.pop(k, None)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_routing_affinity.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/routing/affinity.py tests/unit/test_routing_affinity.py
git commit -m "feat(routing): bounded LRU routing-affinity map"
```

---

## Task 3: Extend `find_deployment_for` → ranked list

**Files:**
- Modify: `src/serve_engine/lifecycle/adapter_router.py`
- Test: `tests/unit/test_adapter_router.py` (extend)

Today `find_deployment_for` returns one `Deployment | None`. We add `rank_deployments_for(conn, base, adapter, *, scorer, signals, request) -> list[Deployment]` and keep `find_deployment_for` as a wrapper that returns the head. Existing callers see no behavior change until they migrate; the proxy migrates in Task 7.

- [ ] **Step 1: Read the current logic**

Read `src/serve_engine/lifecycle/adapter_router.py:98-142` (`find_deployment_for`). Note the existing tuple-score scheme (0 = already loaded, 1 = free slot, 2 = needs evict).

- [ ] **Step 2: Write the failing test**

Extend `tests/unit/test_adapter_router.py`. Add at the bottom (matching its fixture style — read the file first):

```python
def test_rank_deployments_for_returns_full_list_in_scorer_order(tmp_conn_with_deployments):
    """The ranked list is the scored order from the routing scorer, not
    just the head. find_deployment_for stays a thin wrapper that returns
    the head."""
    from serve_engine.lifecycle.adapter_router import rank_deployments_for
    from serve_engine.routing.scorer import (
        DeploymentCandidate, NodeSignals, RoutingRequest,
    )

    # tmp_conn_with_deployments provides three ready deployments of the
    # same base on three different node ids (10, 11, 12). model_required_mb=8000.
    conn = tmp_conn_with_deployments
    signals = {
        10: NodeSignals(node_id=10, mem_free_mb=20000, in_flight=5, latency_p95_ms=200),
        11: NodeSignals(node_id=11, mem_free_mb=20000, in_flight=1, latency_p95_ms=100),
        12: NodeSignals(node_id=12, mem_free_mb=20000, in_flight=3, latency_p95_ms=150),
    }
    ranked = rank_deployments_for(
        conn, base_model_name="test-base", adapter_name=None,
        signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    assert [d.node_id for d in ranked] == [11, 12, 10]


def test_rank_deployments_for_filters_unfittable_candidates(tmp_conn_with_deployments):
    """If a candidate's node can't fit the model, drop it from the
    ranked list. (No node = no candidate, not a 503 candidate.)"""
    from serve_engine.lifecycle.adapter_router import rank_deployments_for
    from serve_engine.routing.scorer import NodeSignals, RoutingRequest

    conn = tmp_conn_with_deployments
    signals = {
        10: NodeSignals(node_id=10, mem_free_mb=20000, in_flight=0, latency_p95_ms=100),
        11: NodeSignals(node_id=11, mem_free_mb=500, in_flight=0, latency_p95_ms=100),
        12: NodeSignals(node_id=12, mem_free_mb=20000, in_flight=0, latency_p95_ms=100),
    }
    ranked = rank_deployments_for(
        conn, base_model_name="test-base", adapter_name=None,
        signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    assert 11 not in {d.node_id for d in ranked}


def test_find_deployment_for_is_head_of_rank_deployments_for(tmp_conn_with_deployments):
    from serve_engine.lifecycle.adapter_router import (
        find_deployment_for, rank_deployments_for,
    )
    from serve_engine.routing.scorer import NodeSignals, RoutingRequest

    conn = tmp_conn_with_deployments
    signals = {
        10: NodeSignals(node_id=10, mem_free_mb=20000, in_flight=5, latency_p95_ms=200),
        11: NodeSignals(node_id=11, mem_free_mb=20000, in_flight=1, latency_p95_ms=100),
        12: NodeSignals(node_id=12, mem_free_mb=20000, in_flight=3, latency_p95_ms=150),
    }
    ranked = rank_deployments_for(
        conn, base_model_name="test-base", adapter_name=None,
        signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    head = find_deployment_for(
        conn, "test-base", None,
        signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    assert head == ranked[0]
```

You will need a `tmp_conn_with_deployments` fixture that the existing test file lacks. Add it at the top:

```python
@pytest.fixture
def tmp_conn_with_deployments(tmp_path):
    """Three ready deployments of base model 'test-base' on node ids 10,
    11, 12. Uses the existing store schema."""
    from serve_engine.store import deployments as dep_store
    from serve_engine.store import models as models_store
    # ... build a sqlite connection, run migrations, insert three deployments.
    # Match what's done in tests/unit/test_admin_endpoints.py for the
    # minimum viable in-memory store setup.
```

If you cannot reuse an existing fixture builder, lift the minimal in-memory DB construction from `tests/unit/test_admin_endpoints.py` (the existing test suite's reference for this) and add it as a fixture here.

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_adapter_router.py -v`
Expected: FAIL — `rank_deployments_for` doesn't exist; `find_deployment_for` has the wrong signature.

- [ ] **Step 4: Implement `rank_deployments_for`**

In `src/serve_engine/lifecycle/adapter_router.py`, add:

```python
from serve_engine.routing.scorer import (
    DeploymentCandidate,
    NodeSignals,
    RoutingRequest,
    default_scorer,
)


def rank_deployments_for(
    conn: sqlite3.Connection,
    base_model_name: str,
    adapter_name: str | None,
    *,
    signals_by_node: dict[int, NodeSignals],
    request: RoutingRequest,
) -> list[dep_store.Deployment]:
    """Ranked list of ready deployments for the (base, adapter) target.

    The adapter-affinity logic (already-loaded > free-slot > needs-evict)
    is preserved as a *prefilter*: only deployments at the best adapter
    affinity tier are scored further. Within a tier, the node-level
    scorer picks the order.
    """
    if adapter_name is None:
        candidates = [
            d for d in dep_store.list_ready(conn)
            if d.model_name == base_model_name
        ]
    else:
        a = ad_store.get_by_name(conn, adapter_name)
        if a is None:
            return []
        # Reuse the existing adapter-tier scoring.
        tiered: list[tuple[int, dep_store.Deployment]] = []
        for d in dep_store.list_ready(conn):
            if d.model_id != a.base_model.id:
                continue
            if d.max_loras <= 0:
                continue
            loaded_into = da_store.find_deployments_with_adapter(conn, a.id)
            if d.id in loaded_into:
                tiered.append((0, d))
            else:
                count = da_store.count_for_deployment(conn, d.id)
                if count < d.max_loras:
                    tiered.append((1, d))
                else:
                    tiered.append((2, d))
        if not tiered:
            return []
        # Take only the best tier — don't score-mix tiers, that would
        # hide cold-load latency behind a fast idle node.
        best_tier = min(t[0] for t in tiered)
        candidates = [d for t, d in tiered if t == best_tier]

    if not candidates:
        return []

    deploy_candidates = [
        DeploymentCandidate(
            deployment_id=d.id,
            node_id=d.node_id,
            model_required_mb=d.vram_reserved_mb,
        )
        for d in candidates
    ]
    ranked = default_scorer(
        candidates=deploy_candidates,
        signals_by_node=signals_by_node,
        request=request,
    )
    # Map back to Deployment, preserving order.
    by_id = {d.id: d for d in candidates}
    return [by_id[c.deployment_id] for c in ranked]


def find_deployment_for(
    conn: sqlite3.Connection,
    base_model_name: str,
    adapter_name: str | None,
    *,
    signals_by_node: dict[int, NodeSignals] | None = None,
    request: RoutingRequest | None = None,
) -> dep_store.Deployment | None:
    """Head of `rank_deployments_for`.

    When `signals_by_node` is None (legacy callers, tests without a
    cluster), falls back to the old behavior: returns the first ready
    deployment regardless of node load.
    """
    if signals_by_node is None:
        return _legacy_find_deployment_for(conn, base_model_name, adapter_name)
    ranked = rank_deployments_for(
        conn, base_model_name, adapter_name,
        signals_by_node=signals_by_node,
        request=request or RoutingRequest(affinity_key=None),
    )
    return ranked[0] if ranked else None


def _legacy_find_deployment_for(
    conn: sqlite3.Connection,
    base_model_name: str,
    adapter_name: str | None,
) -> dep_store.Deployment | None:
    """The pre-scorer body of find_deployment_for, kept verbatim for
    tests and other callers that don't pass signals."""
    # ... move the existing body of find_deployment_for here ...
```

Move the existing `find_deployment_for` body into `_legacy_find_deployment_for` without changes. The new `find_deployment_for` becomes a thin dispatcher.

- [ ] **Step 5: Verify nothing else broke**

Run: `pytest tests/unit/test_adapter_router.py -v`
Expected: 3 new tests passed + existing tests still pass.

Run: `pytest tests/integration/test_openai_proxy.py tests/integration/test_openai_proxy_http.py -v`
Expected: green — proxy still calls `find_deployment_for` without `signals_by_node`, so it takes the legacy path.

- [ ] **Step 6: Commit**

```bash
git add src/serve_engine/lifecycle/adapter_router.py tests/unit/test_adapter_router.py
git commit -m "feat(routing): rank_deployments_for — scorer-driven candidate list"
```

---

## Task 4: `RetryableError` and dispatch-error classification

**Files:**
- Create: `src/serve_engine/daemon/dispatch_errors.py`
- Test: `tests/unit/test_dispatch_errors.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_dispatch_errors.py`:

```python
from __future__ import annotations

import httpx
import pytest

from serve_engine.daemon.dispatch_errors import (
    RetryableError,
    classify_pre_first_byte,
)


def test_connect_error_is_retryable():
    err = classify_pre_first_byte(httpx.ConnectError("nope"))
    assert isinstance(err, RetryableError)
    assert err.reason == "connect"


def test_timeout_is_retryable():
    err = classify_pre_first_byte(httpx.ReadTimeout("slow"))
    assert isinstance(err, RetryableError)
    assert err.reason == "timeout"


@pytest.mark.parametrize("status", [502, 503, 504])
def test_5xx_pre_first_byte_is_retryable(status):
    err = classify_pre_first_byte_status(status)
    assert isinstance(err, RetryableError)
    assert err.reason == "upstream_5xx"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_4xx_is_not_retryable(status):
    assert classify_pre_first_byte_status(status) is None


def test_500_with_no_clear_signal_is_not_retryable():
    """500 from the engine itself probably means a model crash; retrying
    won't help and we'd add load. Only 502/503/504 are retryable."""
    assert classify_pre_first_byte_status(500) is None


def test_node_unreachable_error_is_retryable():
    from serve_engine.daemon.dispatch_errors import NodeUnreachableError
    err = classify_pre_first_byte(NodeUnreachableError(node_id=5))
    assert isinstance(err, RetryableError)
    assert err.reason == "node_unreachable"


def test_unknown_exception_is_not_retryable():
    err = classify_pre_first_byte(ValueError("weird"))
    assert err is None
```

Note: import `classify_pre_first_byte_status` once you've defined it. The test references both `classify_pre_first_byte` (for exception classification) and `classify_pre_first_byte_status` (for status-code classification, before any byte to the client).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_dispatch_errors.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/serve_engine/daemon/dispatch_errors.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class RetryableError:
    """Marker for errors a dispatch-retry-budget can swallow."""
    reason: str
    underlying: BaseException | None = None


class NodeUnreachableError(Exception):
    """Raised by the dispatch layer when the chosen node's AgentLink
    isn't ready (heartbeat stale, agent disconnected) at dispatch time.
    Carries the node id for logging."""
    def __init__(self, node_id: int) -> None:
        super().__init__(f"node {node_id} unreachable")
        self.node_id = node_id


_RETRYABLE_HTTPX = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def classify_pre_first_byte(exc: BaseException) -> RetryableError | None:
    """Classify an exception raised before any byte was sent to the
    client. Returns RetryableError when the next-ranked candidate has
    a reasonable chance of succeeding."""
    if isinstance(exc, NodeUnreachableError):
        return RetryableError(reason="node_unreachable", underlying=exc)
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return RetryableError(reason="connect", underlying=exc)
    if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
        return RetryableError(reason="timeout", underlying=exc)
    if isinstance(exc, httpx.RemoteProtocolError):
        return RetryableError(reason="remote_protocol", underlying=exc)
    return None


_RETRYABLE_STATUS = {502, 503, 504}


def classify_pre_first_byte_status(status: int) -> RetryableError | None:
    """Same idea as `classify_pre_first_byte`, but for upstream status
    codes seen before any body has been streamed to the client."""
    if status in _RETRYABLE_STATUS:
        return RetryableError(reason="upstream_5xx")
    return None
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_dispatch_errors.py -v`
Expected: 9 passed (counting parametrized).

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/daemon/dispatch_errors.py tests/unit/test_dispatch_errors.py
git commit -m "feat(daemon): pre-first-byte error classifier (retryable vs not)"
```

---

## Task 5: Carve `daemon/dispatch.py` from `openai_proxy.py`

**Files:**
- Create: `src/serve_engine/daemon/dispatch.py`
- Modify: `src/serve_engine/daemon/openai_proxy.py`

Extract the remote-vs-local dispatch logic (lines ~258-409 in `openai_proxy.py`) into one function: given a chosen deployment + prepared headers/body, open the upstream stream and return `(status, headers, body_iterator)`. The streamer composition (usage tracker, tracer finalize) stays in `openai_proxy.py` because it's tied to the request context.

- [ ] **Step 1: Read the current dispatch site**

Read `src/serve_engine/daemon/openai_proxy.py:256-409` to understand the two branches (remote via AgentLink, local via httpx).

- [ ] **Step 2: Write the failing test**

Create `tests/unit/test_dispatch.py`:

```python
from __future__ import annotations

import pytest

from serve_engine.daemon.dispatch import open_upstream_stream


@pytest.mark.asyncio
async def test_open_upstream_stream_local_uses_direct_httpx(httpx_mock):
    """Local dispatch (active.node_id == local_node_id) must NOT
    consult the registry; it goes direct to the container address."""
    # Detailed fake-deployment setup omitted here for brevity; pattern
    # matches tests/integration/test_openai_proxy_http.py — read that
    # file first to construct the equivalent fixture.
    ...


@pytest.mark.asyncio
async def test_open_upstream_stream_remote_uses_agent_link():
    """Remote dispatch (active.node_id != local_node_id) must use the
    AgentLink from the registry. Raises NodeUnreachableError if not."""
    from serve_engine.daemon.dispatch_errors import NodeUnreachableError
    from serve_engine.daemon.dispatch import open_upstream_stream

    class FakeRegistry:
        def get(self, _): return None
    class FakeDep:
        id = 1; node_id = 99; container_id = "c1"
        container_address = None; container_port = None

    with pytest.raises(NodeUnreachableError):
        await open_upstream_stream(
            deployment=FakeDep(), local_node_id=0, registry=FakeRegistry(),
            method="POST", path="/v1/chat/completions",
            headers={}, body=b"{}",
        )
```

Adapt the local test once you've inspected the fixtures in `tests/integration/test_openai_proxy_http.py`.

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_dispatch.py -v`
Expected: FAIL — module missing.

- [ ] **Step 4: Implement**

Create `src/serve_engine/daemon/dispatch.py`:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.daemon.dispatch_errors import NodeUnreachableError
from serve_engine.lifecycle.engine_clients import make_engine_client


@dataclass
class UpstreamStream:
    """Return value of `open_upstream_stream`. Caller is responsible for
    closing via `aclose()` once the body iterator is drained."""
    status: int
    headers: dict[str, str]
    body: AsyncIterator[bytes]
    aclose: callable  # async () -> None


async def open_upstream_stream(
    *,
    deployment,            # dep_store.Deployment
    local_node_id: int,
    registry: AgentRegistry,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
) -> UpstreamStream:
    """Open the upstream stream for a single deployment.

    Local deployment → direct httpx stream to the container.
    Remote deployment → AgentLink proxy_request through the tunnel.

    Raises NodeUnreachableError before any byte is consumed if the
    target node has no live, ready AgentLink.
    """
    is_remote = (
        deployment.node_id != 0 and deployment.node_id != local_node_id
    )
    if is_remote:
        link = registry.get(deployment.node_id)
        if link is None or not link.is_ready:
            raise NodeUnreachableError(node_id=deployment.node_id)
        if deployment.container_id is None:
            raise NodeUnreachableError(node_id=deployment.node_id)
        return await _open_remote(link, deployment, method, path, headers, body)
    return await _open_local(deployment, method, path, headers, body)


async def _open_remote(
    link, deployment, method, path, headers, body,
) -> UpstreamStream:
    agen = link.proxy_request(
        container_id=deployment.container_id,
        method=method, path=path, headers=headers, body=body,
    )
    first = None
    status = 502
    upstream_headers: dict[str, str] = {}
    async for ch in agen:
        if ch.status is not None:
            status = ch.status
        if ch.headers is not None:
            upstream_headers = dict(ch.headers)
        first = ch
        break

    async def body_iter():
        if first is not None and first.body:
            yield first.body
        if first is None or not first.eof:
            async for ch in agen:
                if ch.body:
                    yield ch.body
                if ch.eof:
                    break

    async def aclose():
        # AsyncIterator from `proxy_request` doesn't expose explicit
        # close; the link drops the stream on disconnect.
        pass

    return UpstreamStream(
        status=status, headers=upstream_headers,
        body=body_iter(), aclose=aclose,
    )


async def _open_local(
    deployment, method, path, headers, body,
) -> UpstreamStream:
    base = (
        f"http://{deployment.container_address}:{deployment.container_port}"
    )
    client = make_engine_client(base)
    stream_cm = client.stream(method, path, content=body, headers=headers)
    resp = await stream_cm.__aenter__()

    async def body_iter():
        async for chunk in resp.aiter_raw():
            yield chunk

    async def aclose():
        await stream_cm.__aexit__(None, None, None)
        await client.aclose()

    return UpstreamStream(
        status=resp.status_code,
        headers=dict(resp.headers),
        body=body_iter(),
        aclose=aclose,
    )
```

Notes:
- `make_engine_client` already exists somewhere in the codebase — search for it and import correctly.
- This keeps the function returning before any body byte is yielded, so the retry logic in the next task has a clear "no bytes sent" window.

- [ ] **Step 5: Update `openai_proxy.py` to call the new function**

Replace the existing inline dispatch (the 150-line block around lines 256-409 in `openai_proxy.py`) with a call to `open_upstream_stream(...)`. The streamer wrapping, usage tracker, tracer finalize, and StreamingResponse construction stay in the proxy — the dispatch unit just opens the stream.

This is the largest mechanical edit in the plan. Do it carefully:
1. Build the call: `up = await open_upstream_stream(deployment=active, local_node_id=local_node_id, registry=request.app.state.agent_registry, method="POST", path=engine_path, headers=headers, body=body)`
2. Replace the remote/local branching with one path that consumes `up.body`.
3. The streamer function (`remote_streamer` / `streamer`) collapses into one that iterates `up.body` and calls `up.aclose()` in `finally:`.
4. Status/headers/media-type read from `up.status` / `up.headers`.

- [ ] **Step 6: Run the existing proxy suite**

Run: `pytest tests/integration/test_openai_proxy.py tests/integration/test_openai_proxy_http.py tests/integration/test_remote_agent_roundtrip.py -v`
Expected: all green. Behavior is identical at this checkpoint — no retry yet, no scoring yet, just refactored dispatch.

Run: `pytest tests/unit/test_dispatch.py -v`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add src/serve_engine/daemon/dispatch.py src/serve_engine/daemon/openai_proxy.py tests/unit/test_dispatch.py
git commit -m "refactor(proxy): carve daemon/dispatch.py — open_upstream_stream"
```

---

## Task 6: Retry dispatcher

**Files:**
- Create: `src/serve_engine/daemon/retry_dispatcher.py`
- Test: `tests/unit/test_retry_dispatcher.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_retry_dispatcher.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

import pytest

from serve_engine.daemon.dispatch_errors import NodeUnreachableError
from serve_engine.daemon.retry_dispatcher import dispatch_with_retry


@dataclass
class FakeDep:
    id: int
    node_id: int


@pytest.mark.asyncio
async def test_first_candidate_succeeds_no_retry():
    calls: list[int] = []
    async def open_stream(deployment, **kw):
        calls.append(deployment.id)
        return ("ok", deployment)
    result = await dispatch_with_retry(
        ranked=[FakeDep(1, 10), FakeDep(2, 11)],
        open_stream=open_stream,
        budget=2,
    )
    assert result == ("ok", FakeDep(1, 10))
    assert calls == [1]


@pytest.mark.asyncio
async def test_retries_on_node_unreachable_then_succeeds():
    calls: list[int] = []
    async def open_stream(deployment, **kw):
        calls.append(deployment.id)
        if deployment.id == 1:
            raise NodeUnreachableError(node_id=10)
        return ("ok", deployment)
    result = await dispatch_with_retry(
        ranked=[FakeDep(1, 10), FakeDep(2, 11)],
        open_stream=open_stream,
        budget=2,
    )
    assert result == ("ok", FakeDep(2, 11))
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_non_retryable_error_propagates_immediately():
    async def open_stream(deployment, **kw):
        raise ValueError("boom")
    with pytest.raises(ValueError):
        await dispatch_with_retry(
            ranked=[FakeDep(1, 10), FakeDep(2, 11)],
            open_stream=open_stream,
            budget=2,
        )


@pytest.mark.asyncio
async def test_budget_exhausted_propagates_last_error():
    async def open_stream(deployment, **kw):
        raise NodeUnreachableError(node_id=deployment.node_id)
    with pytest.raises(NodeUnreachableError):
        await dispatch_with_retry(
            ranked=[FakeDep(1, 10), FakeDep(2, 11), FakeDep(3, 12)],
            open_stream=open_stream,
            budget=2,  # 2 retries → 3 attempts total → exhausts all 3
        )


@pytest.mark.asyncio
async def test_each_node_tried_at_most_once_even_if_multiple_deployments_share_node():
    calls: list[int] = []
    async def open_stream(deployment, **kw):
        calls.append(deployment.id)
        raise NodeUnreachableError(node_id=deployment.node_id)
    with pytest.raises(NodeUnreachableError):
        await dispatch_with_retry(
            ranked=[FakeDep(1, 10), FakeDep(2, 10), FakeDep(3, 11)],
            open_stream=open_stream,
            budget=5,
        )
    # Two attempts: deployment 1 (node 10), then deployment 3 (node 11).
    # Deployment 2 (node 10 again) is skipped.
    assert calls == [1, 3]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_retry_dispatcher.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Create `src/serve_engine/daemon/retry_dispatcher.py`:

```python
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from serve_engine.daemon.dispatch_errors import (
    RetryableError,
    classify_pre_first_byte,
)

log = logging.getLogger(__name__)

T = TypeVar("T")


async def dispatch_with_retry(
    *,
    ranked: list,
    open_stream: Callable[..., Awaitable[T]],
    budget: int = 2,
    **open_stream_kwargs: Any,
) -> T:
    """Walk `ranked` calling `open_stream(deployment, **kwargs)`,
    retrying on retryable pre-first-byte errors until either:
    - one succeeds (returned),
    - the budget is exhausted (`budget` retries → `budget + 1` attempts max),
    - all distinct nodes have been tried,
    - a non-retryable error is hit (re-raised immediately).

    Each node is tried at most once per call — multiple candidate
    deployments on the same node don't burn extra attempts.
    """
    if not ranked:
        raise RuntimeError("dispatch_with_retry: no candidates")

    attempts = 0
    tried_nodes: set[int] = set()
    last_err: BaseException | None = None
    max_attempts = budget + 1

    for deployment in ranked:
        if deployment.node_id in tried_nodes:
            continue
        if attempts >= max_attempts:
            break
        tried_nodes.add(deployment.node_id)
        attempts += 1
        try:
            return await open_stream(deployment, **open_stream_kwargs)
        except BaseException as exc:
            classified = classify_pre_first_byte(exc)
            if classified is None:
                # Non-retryable: re-raise immediately.
                raise
            log.warning(
                "dispatch_retry node=%s reason=%s attempt=%d",
                deployment.node_id, classified.reason, attempts,
            )
            last_err = exc
            continue

    # Budget exhausted or no untried nodes left.
    assert last_err is not None
    raise last_err
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_retry_dispatcher.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/daemon/retry_dispatcher.py tests/unit/test_retry_dispatcher.py
git commit -m "feat(daemon): retry_dispatcher — pre-first-byte retry across nodes"
```

---

## Task 7: Wire scorer + retry dispatcher into the OpenAI proxy

**Files:**
- Modify: `src/serve_engine/daemon/openai_proxy.py`
- Test: `tests/integration/test_openai_proxy_routing.py` (new)

This is the integration point. The proxy must:
1. Build `signals_by_node` from the aggregator.
2. Resolve the affinity key (header → API key → None).
3. Look up `affinity.lookup(key) → affinity_node_id`.
4. Call `rank_deployments_for(..., signals, request) → ranked`.
5. Call `dispatch_with_retry(ranked=ranked, open_stream=open_upstream_stream, **kwargs)`.
6. On success, update `affinity.set(key, node_id=chosen.node_id)`.

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/test_openai_proxy_routing.py`:

```python
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_proxy_picks_lowest_in_flight_when_two_deployments_serve_same_model(
    proxy_client_with_two_deployments_on_two_nodes,
):
    """Two deployments of 'test-base' on nodes 10 and 11. Node 11 has
    higher in_flight per the aggregator. Request must land on node 10."""
    # Fixture sets up: two deployments, an aggregator with synthetic
    # signals favoring node 10. Real proxy stack but fake backend.
    ...


@pytest.mark.asyncio
async def test_proxy_retries_when_first_node_unreachable(
    proxy_client_with_one_unreachable_node,
):
    """Two ranked deployments. The first node has no live AgentLink.
    Client must see success from the second."""
    ...


@pytest.mark.asyncio
async def test_proxy_records_affinity_after_successful_dispatch(
    proxy_client_with_two_deployments_on_two_nodes,
):
    """First request lands on node X; second request with the same
    session header must prefer node X even if its load went up."""
    ...
```

The fixtures are substantial. Match the style of `tests/integration/test_openai_proxy.py` for fake-backend setup. If you cannot land all three at once, split this into a minimum viable test (the in_flight ranking case) and add the others in follow-ups.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_openai_proxy_routing.py -v`
Expected: FAIL (fixtures or behavior).

- [ ] **Step 3: Wire `app.state.routing_affinity` at startup**

In `src/serve_engine/daemon/app.py`:

```python
from serve_engine.routing.affinity import RoutingAffinity

app.state.routing_affinity = RoutingAffinity(capacity=10_000)
```

- [ ] **Step 4: Build `signals_by_node` from the aggregator**

Add a small helper in `src/serve_engine/daemon/openai_proxy.py` (or a new tiny module):

```python
from serve_engine.routing.scorer import NodeSignals


def _build_signals_by_node(aggregator, *, nodes_store, conn) -> dict[int, NodeSignals]:
    out: dict[int, NodeSignals] = {}
    snap = aggregator.snapshot()
    for node_id, sample in snap.items():
        gpus = sample.get("gpus", [])
        mem_free = sum(
            max(0, g.get("mem_total_mb", 0) - g.get("mem_used_mb", 0))
            for g in gpus
        )
        in_flight = sum(
            d.get("in_flight", 0) for d in sample.get("deployments", [])
        )
        # p95 is per-deployment in the sample. We use the max across
        # this node's deployments as the node-level p95 — a conservative
        # choice that pessimises a node where any deployment is slow.
        p95 = max(
            (d.get("latency_p95_ms", 0) for d in sample.get("deployments", [])),
            default=0,
        )
        out[node_id] = NodeSignals(
            node_id=node_id,
            mem_free_mb=mem_free,
            in_flight=in_flight,
            latency_p95_ms=p95,
        )
    return out
```

- [ ] **Step 5: Replace the deployment-selection block in the proxy**

Find the block in `src/serve_engine/daemon/openai_proxy.py` starting at the comment `# Resolve \`model\` to (base, optional adapter).` (around line 138) and ending at the existing `find_deployment_for` call (line 153-157). Replace the candidate-resolution loop with:

```python
aggregator = request.app.state.metrics_aggregator
affinity = request.app.state.routing_affinity

# Determine affinity key.
affinity_key = (
    request.headers.get("x-session-id")
    or request.headers.get("x-conversation-id")
    or (f"key:{key.id}" if key is not None else None)
)
affinity_node_id = affinity.lookup(affinity_key) if affinity_key else None
signals = _build_signals_by_node(
    aggregator, nodes_store=_nodes_store, conn=conn,
)
routing_request = RoutingRequest(
    affinity_key=affinity_key, affinity_node_id=affinity_node_id,
)

ranked: list = []
unknown_error: UnknownModel | None = None
target = None
routed_model_name = requested_model_name
for candidate_model_name in candidate_model_names:
    try:
        candidate_target = resolve_target(conn, candidate_model_name)
    except UnknownModel as e:
        unknown_error = e
        continue
    candidates = rank_deployments_for(
        conn, candidate_target.base_model_name, candidate_target.adapter_name,
        signals_by_node=signals, request=routing_request,
    )
    if candidates:
        target = candidate_target
        ranked = candidates
        routed_model_name = candidate_model_name
        break

if not ranked:
    # ... existing 'no deployment' branches kept as-is ...
```

(Preserve the existing UnknownModel / 404 / 503 error paths verbatim.)

- [ ] **Step 6: Replace the dispatch site with `dispatch_with_retry`**

Find the call into `open_upstream_stream` from Task 5. Wrap it:

```python
from serve_engine.daemon.retry_dispatcher import dispatch_with_retry

up: UpstreamStream = await dispatch_with_retry(
    ranked=ranked,
    open_stream=open_upstream_stream,
    budget=2,
    local_node_id=local_node_id,
    registry=request.app.state.agent_registry,
    method="POST",
    path=engine_path,
    headers=headers,
    body=body,
)
# On success, record affinity:
if affinity_key:
    affinity.set(affinity_key, node_id=up.deployment.node_id)
# ... existing streamer code consuming up.body ...
```

For `up.deployment.node_id` to be available, extend `UpstreamStream` from Task 5 to carry the chosen `deployment` (or, simpler, change `open_upstream_stream`'s return type to a tuple `(UpstreamStream, deployment)`). Either works — pick the smaller diff.

- [ ] **Step 7: Run tests, verify pass**

Run: `pytest tests/integration/test_openai_proxy_routing.py -v`
Expected: green for the tests you implemented.

Run: `pytest tests/integration/test_openai_proxy.py tests/integration/test_openai_proxy_http.py -v`
Expected: green — pre-existing tests must continue to pass.

- [ ] **Step 8: Commit**

```bash
git add src/serve_engine/daemon/openai_proxy.py src/serve_engine/daemon/app.py tests/integration/test_openai_proxy_routing.py
git commit -m "feat(proxy): scorer + retry_dispatcher + affinity-aware routing"
```

---

## Task 8: SSE backpressure

**Files:**
- Modify: `src/serve_engine/daemon/openai_proxy.py`
- Test: `tests/integration/test_proxy_backpressure.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_proxy_backpressure.py`:

```python
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_slow_client_applies_backpressure_to_engine():
    """When the client reads slowly, the proxy must stop reading from
    the engine. We assert by tracking how many chunks the engine has
    produced after the client pauses for ~200ms. With backpressure the
    engine produces at most queue_depth + 1; without it, it races
    ahead unbounded."""

    # Set up a fake engine that produces 1000 small chunks as fast as
    # possible. Connect a real proxy with queue_depth=8. Read from the
    # proxy client, then pause for 200ms while observing the engine's
    # chunk counter. Assert engine_produced_after_pause <= queue_depth + 2.

    # Implementation pattern: counter on the engine's side; a fake
    # client that reads N chunks then sleeps. See
    # tests/integration/test_openai_proxy_http.py for the proxy
    # fixture.
    ...
```

- [ ] **Step 2: Implement the bounded queue**

In `src/serve_engine/daemon/openai_proxy.py`, find the streamer function (after the Task 5 refactor, there should be exactly one — consuming `up.body`). Replace it with two coroutines connected by a bounded `asyncio.Queue`:

```python
sse_queue_depth = getattr(
    request.app.state.config, "sse_queue_depth", 64,
)

async def streamer():
    q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=sse_queue_depth)
    first_byte_seen = False

    async def reader():
        try:
            async for chunk in up.body:
                await q.put(chunk)   # applies backpressure when full
        finally:
            await q.put(None)        # sentinel for end-of-stream

    reader_task = asyncio.create_task(reader())
    try:
        while True:
            chunk = await q.get()
            if chunk is None:
                break
            if not first_byte_seen:
                first_byte_seen = True
                tracer.update(trace, first_byte_at=time.monotonic())
            usage_tracker.feed(chunk)
            yield chunk
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass
        await up.aclose()
        # ... existing usage/tracer finalize ...
```

Knobs:
- `sse_queue_depth` defaults to 64; configurable via `config.py`.
- Queue items are `bytes | None` (None as end sentinel).

- [ ] **Step 3: Run the proxy regression suite**

Run: `pytest tests/integration/test_openai_proxy.py tests/integration/test_openai_proxy_http.py -v`
Expected: all green — backpressure changes throughput behavior, not correctness.

- [ ] **Step 4: Run the new backpressure test**

Run: `pytest tests/integration/test_proxy_backpressure.py -v`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/daemon/openai_proxy.py src/serve_engine/config.py tests/integration/test_proxy_backpressure.py
git commit -m "feat(proxy): bounded SSE queue applies backpressure to engine"
```

---

## Task 9: Node-loss audit logging + affinity eviction

**Files:**
- Modify: `src/serve_engine/cluster/health_watcher.py`
- Modify: `src/serve_engine/daemon/app.py` (pass routing_affinity to health watcher)
- Test: `tests/unit/test_health_watcher_audit.py` (new)

When a node transitions to `unreachable`, log in-flight context and clear affinity entries pointing at it.

- [ ] **Step 1: Read the existing transition**

Read `src/serve_engine/cluster/health_watcher.py`. It's 48 lines — small. Find the transition site.

- [ ] **Step 2: Write the failing test**

Create `tests/unit/test_health_watcher_audit.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from serve_engine.routing.affinity import RoutingAffinity


def test_node_unreachable_evicts_affinity_entries(caplog):
    aff = RoutingAffinity(capacity=4)
    aff.set("k1", node_id=10)
    aff.set("k2", node_id=11)
    # Drive the transition (call whatever method health_watcher exposes
    # — likely a private _mark_unreachable or similar). The exact API
    # depends on the file's structure.
    from serve_engine.cluster.health_watcher import _on_node_unreachable
    _on_node_unreachable(
        node_id=10, label="worker-a", in_flight_count=3,
        affinity=aff, logger_fn=lambda msg, **kw: caplog.records.append(msg),
    )
    assert aff.lookup("k1") is None
    assert aff.lookup("k2") == 11


def test_node_unreachable_logs_audit_line(caplog):
    aff = RoutingAffinity(capacity=4)
    msgs: list[str] = []
    from serve_engine.cluster.health_watcher import _on_node_unreachable
    _on_node_unreachable(
        node_id=10, label="worker-a", in_flight_count=3,
        affinity=aff, logger_fn=lambda msg, **kw: msgs.append(msg),
    )
    joined = " ".join(msgs)
    assert "worker-a" in joined or "node_id=10" in joined
    assert "in_flight" in joined or "in-flight" in joined
```

- [ ] **Step 3: Implement the audit hook**

In `src/serve_engine/cluster/health_watcher.py`, add:

```python
import logging

log = logging.getLogger(__name__)


def _on_node_unreachable(
    *,
    node_id: int,
    label: str,
    in_flight_count: int,
    affinity,           # RoutingAffinity
    logger_fn=None,
) -> None:
    """Called when a node transitions ready → unreachable. Pure side-
    effect: log + clear affinity. No new state."""
    msg = (
        f"node_loss_audit node_id={node_id} label={label!r} "
        f"in_flight={in_flight_count}"
    )
    (logger_fn or log.warning)(msg)
    affinity.evict_node(node_id)
```

Then wire `_on_node_unreachable` into the existing health-watcher loop at the place it currently transitions a node. Pass the affinity instance from `app.state.routing_affinity`.

For `in_flight_count`, query the aggregator: `aggregator.deployment_in_flight(node_id=node_id, deployment_id=...)` for each known deployment, summed. If that's hard to thread from where the transition happens, just pass `0` for now — the line is still useful for traceability and the deeper attribution is a follow-up.

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_health_watcher_audit.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cluster/health_watcher.py src/serve_engine/daemon/app.py tests/unit/test_health_watcher_audit.py
git commit -m "feat(cluster): node-loss audit log + affinity eviction"
```

---

## Task 10: Integration test — kill node mid-dispatch

**Files:**
- Create: `tests/integration/test_dispatch_retry_e2e.py`

End-to-end: leader + two fake agents. Mid-dispatch, the chosen agent's `is_ready` flips to False. The retry must land the request on the other agent. Client sees success.

- [ ] **Step 1: Write the test**

Create `tests/integration/test_dispatch_retry_e2e.py`:

```python
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_kill_first_node_between_selection_and_dispatch(
    leader_with_two_agents,
):
    """Both nodes serve 'test-base'. The aggregator favors node A.
    Between candidate ranking and dispatch, node A goes unreachable.
    Client must see success from node B.

    Setup pattern: extend tests/integration/test_remote_agent_roundtrip.py's
    leader+agent harness. Flip the first agent's `is_ready` to False
    via the registry between the proxy's call to ranking and the call
    to dispatch — easiest via monkeypatching `open_upstream_stream` to
    flip the link on its first call to node A.
    """
    ...


@pytest.mark.asyncio
async def test_5xx_pre_first_byte_triggers_retry(leader_with_two_agents):
    """First agent returns HTTP 503 before any byte; client gets success
    from the second."""
    ...
```

Two cases is enough — the deep unit coverage is in `test_retry_dispatcher.py`. This test validates end-to-end wiring.

- [ ] **Step 2: Run the integration test**

Run: `pytest tests/integration/test_dispatch_retry_e2e.py -v`
Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_dispatch_retry_e2e.py
git commit -m "test(cluster): e2e retry across nodes on dispatch failure"
```

---

## Task 11: Docs update

**Files:**
- Modify: `docs/multi-node.md`

Add a "Routing & Resilience" section covering:
- The affinity key precedence (`X-Session-Id` > `X-Conversation-Id` > API key > none).
- That the leader keeps a bounded in-memory affinity map (lost on restart).
- The retry budget (default 2 retries pre-first-byte, distinct nodes only).
- The mid-stream-failure limitation (no automatic failover after first byte).
- That `sse_queue_depth` controls how aggressively the engine is throttled by a slow client.

Roughly 60-100 lines. Read the existing file first to match its tone.

- [ ] **Step 1: Write the section**

Edit `docs/multi-node.md`. Append the new section after the existing "Observability" section (added in Plan A).

- [ ] **Step 2: Commit**

```bash
git add docs/multi-node.md
git commit -m "docs: routing affinity, retry budget, mid-stream limitation"
```

---

## Self-Review Output

**1. Spec coverage**

- Placement scorer (pure, lexicographic, debuggable) — Task 1.
- Routing affinity map (LRU, bounded, node eviction) — Task 2.
- `rank_deployments_for` returning a ranked list — Task 3.
- `RetryableError` + classification — Task 4.
- Carve `daemon/dispatch.py` — Task 5.
- `retry_dispatcher` — Task 6.
- Scorer + retry wired into the proxy + affinity recorded on success — Task 7.
- SSE backpressure — Task 8.
- Node-loss audit + affinity eviction — Task 9.
- E2E retry test — Task 10.
- Docs — Task 11.
- Carving `lifecycle/dispatch.py` from `manager.py` was in the spec; **this plan instead carves `daemon/dispatch.py` from `openai_proxy.py`** because that's where the *request* dispatch lives in the current codebase. The spec's manager.py reference was based on an inaccurate read; the right boundary is the proxy. The spec section "Refactor scope" should be updated to match — flag at handoff.

**2. Placeholder scan** — some tasks (Task 7's integration tests, Task 10's e2e tests, Task 11's docs) instruct the engineer to read existing files first and match the pattern, rather than spell out every line. This is intentional: the fixtures in `tests/integration/test_openai_proxy.py` and the prose tone in `docs/multi-node.md` are stable references in this repo that hand-rolling here would just duplicate. No "TBD" or vague-error-handling placeholders remain.

**3. Type consistency**

- `DeploymentCandidate.deployment_id` / `.node_id` / `.model_required_mb` — consistent.
- `NodeSignals(node_id, mem_free_mb, in_flight, latency_p95_ms)` — consistent.
- `RoutingRequest(affinity_key, affinity_node_id=None)` — consistent.
- `default_scorer(*, candidates, signals_by_node, request)` — consistent across scorer, `rank_deployments_for`, proxy.
- `dispatch_with_retry(*, ranked, open_stream, budget, **open_stream_kwargs)` — consistent.
- `RetryableError(reason, underlying=None)` + `classify_pre_first_byte(exc)` — consistent.
- `NodeUnreachableError(node_id)` — consistent across dispatch, retry, health watcher.
- `RoutingAffinity.lookup(key)` / `.set(key, node_id=)` / `.evict_node(node_id)` — consistent.

**Spec-aligned scope reduction:**
- "Affinity persistence" — explicitly punted to a follow-up plan in the spec ("the moment we have leader HA, this becomes a real question").
- `serve metrics tail` CLI — deferred to follow-up (covered in Plan A's handoff notes).
- The spec's "carve `lifecycle/dispatch.py` from `manager.py`" is not what this plan does; it carves request dispatch from the proxy instead. The manager.py-size issue remains for a future plan.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-19-smart-routing-and-resilience.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

This plan depends on `2026-05-19-observability-data-plane.md`. Tasks 7-10 in particular require `app.state.metrics_aggregator` and `app.state.routing_affinity` to be populated, which Plan A wires.
