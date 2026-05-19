"""PredictorTask tick loop: candidate -> preload -> junction row.

The tick loop is the seam where the pure-function predictor meets the
engine HTTP layer. Tests cover the happy path (adapter gets loaded),
the no-deployment skip, the already-warm skip, and the
max_prewarm_per_tick cap.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from berth.backends.vllm import VLLMBackend
from berth.lifecycle.predictor import (
    KeyAffinityConfig,
    PredictorConfig,
    RuleConfig,
    SequencingConfig,
)
from berth.lifecycle.predictor_task import PredictorTask
from berth.store import adapters as ad_store
from berth.store import db
from berth.store import deployment_adapters as da_store
from berth.store import deployments as dep_store
from berth.store import models as model_store


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def _seed_deployment(conn, base_name: str, *, max_loras: int = 4) -> tuple:
    base = model_store.add(conn, name=base_name, hf_repo=f"o/{base_name}")
    dep = dep_store.create(
        conn, model_id=base.id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
        max_loras=max_loras,
    )
    dep_store.set_container(
        conn, dep.id, container_id="cid", container_name="x",
        container_port=49152, container_address="127.0.0.1",
    )
    dep_store.update_status(conn, dep.id, "ready")
    return base, dep


def _seed_adapter(conn, name: str, base_name: str, tmp_path):
    a = ad_store.add(conn, name=name, base_model_name=base_name, hf_repo=f"o/{name}")
    adir = tmp_path / f"models--o--{name}" / "snapshots" / "abc"
    adir.mkdir(parents=True)
    ad_store.set_local_path(conn, a.id, str(adir))
    return a


def _seed_recent_event(conn, base: str, adapter: str | None = None):
    """Insert one event "now" so the sequencing rule has a trigger."""
    conn.execute(
        """
        INSERT INTO usage_events (ts, api_key_id, model_name, base_name,
                                  adapter_name, deployment_id)
        VALUES (datetime('now'), NULL, ?, ?, ?, NULL)
        """,
        (adapter or base, base, adapter),
    )


def _intercept_engine_loads(monkeypatch):
    """Catch /v1/load_lora_adapter calls so tests don't need a real engine."""
    captured: list[dict] = []
    original_post = httpx.AsyncClient.post

    async def fake_post(self, url, *, json=None, **kw):
        if "49152" in str(url) and "load_lora_adapter" in str(url):
            captured.append({"url": str(url), "json": json})
            return httpx.Response(200, json={"message": "ok"})
        return await original_post(self, url, json=json, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return captured


def _only_seq() -> PredictorConfig:
    """Config with only the sequencing rule on - the easiest rule to
    trigger with a single seed event."""
    return PredictorConfig(
        time_of_day=RuleConfig(enabled=False),
        sequencing=SequencingConfig(window_s=30, min_p=0.30),
        key_affinity=KeyAffinityConfig(enabled=False),
    )


def test_task_exposes_public_snapshots(tmp_path):
    conn = _fresh(tmp_path)
    task = PredictorTask(
        conn=conn,
        backends={},
        models_dir=tmp_path,
        config=_only_seq(),
    )

    assert task.candidates_snapshot() == []
    stats = task.stats_snapshot()
    assert stats["enabled"] is True
    assert stats["preloads_attempted"] == 0
    assert stats["preloads_succeeded"] == 0
    assert "tick_interval_s" in stats


@pytest.mark.asyncio
async def test_tick_preloads_adapter_into_existing_deployment(tmp_path, monkeypatch):
    """When the predictor returns an adapter candidate AND a ready base
    deployment exists, the tick loop calls the engine load endpoint and
    creates the junction row."""
    conn = _fresh(tmp_path)
    _, dep = _seed_deployment(conn, "qwen3-7b")
    a = _seed_adapter(conn, "tone-formal", "qwen3-7b", tmp_path)

    # Build sequencing history: 5 historical (base) -> (adapter) pairs +
    # a recent base trigger.
    now = datetime.now(UTC).replace(tzinfo=None)
    for i in range(5):
        x_ts = now - timedelta(hours=1) - timedelta(minutes=i)
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'qwen3-7b', 'qwen3-7b', NULL)",
            (x_ts.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'tone-formal', 'qwen3-7b', 'tone-formal')",
            ((x_ts + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"),),
        )
    _seed_recent_event(conn, "qwen3-7b")

    cap = _intercept_engine_loads(monkeypatch)

    task = PredictorTask(
        conn=conn, backends={"vllm": VLLMBackend()},
        models_dir=tmp_path, config=_only_seq(),
    )
    triggered = await task.tick_once()

    assert triggered == 1
    assert task.preloads_succeeded == 1
    assert len(cap) == 1
    assert cap[0]["json"]["lora_name"] == "tone-formal"
    # Junction row created.
    assert dep.id in da_store.find_deployments_with_adapter(conn, a.id)


@pytest.mark.asyncio
async def test_tick_skips_bare_base_candidates(tmp_path, monkeypatch):
    """Bare-base candidates can't be pre-warmed (need plan reconstruction).
    The tick must skip them silently rather than crash."""
    conn = _fresh(tmp_path)
    _seed_deployment(conn, "qwen3-7b")
    # Sequencing history points at another BASE (no adapter).
    now = datetime.now(UTC).replace(tzinfo=None)
    for i in range(5):
        x_ts = now - timedelta(hours=1) - timedelta(minutes=i)
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'qwen3-7b', 'qwen3-7b', NULL)",
            (x_ts.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'other-base', 'other-base', NULL)",
            ((x_ts + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"),),
        )
    _seed_recent_event(conn, "qwen3-7b")

    cap = _intercept_engine_loads(monkeypatch)
    task = PredictorTask(
        conn=conn, backends={"vllm": VLLMBackend()},
        models_dir=tmp_path, config=_only_seq(),
    )
    triggered = await task.tick_once()

    assert triggered == 0
    assert cap == []


@pytest.mark.asyncio
async def test_tick_skips_already_loaded_adapter(tmp_path, monkeypatch):
    """If the adapter is already in the junction table, the predictor
    must not re-issue a load to the engine - that would be wasted work."""
    conn = _fresh(tmp_path)
    _, dep = _seed_deployment(conn, "qwen3-7b")
    a = _seed_adapter(conn, "tone-formal", "qwen3-7b", tmp_path)
    da_store.attach(conn, dep.id, a.id)  # pre-load

    # Build sequencing -> tone-formal.
    now = datetime.now(UTC).replace(tzinfo=None)
    for i in range(5):
        x_ts = now - timedelta(hours=1) - timedelta(minutes=i)
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'qwen3-7b', 'qwen3-7b', NULL)",
            (x_ts.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'tone-formal', 'qwen3-7b', 'tone-formal')",
            ((x_ts + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"),),
        )
    _seed_recent_event(conn, "qwen3-7b")

    cap = _intercept_engine_loads(monkeypatch)
    task = PredictorTask(
        conn=conn, backends={"vllm": VLLMBackend()},
        models_dir=tmp_path, config=_only_seq(),
    )
    triggered = await task.tick_once()

    assert triggered == 0
    assert cap == []
    assert task.preloads_skipped_already_warm == 1


@pytest.mark.asyncio
async def test_tick_skips_when_no_ready_base_deployment(tmp_path, monkeypatch):
    """Adapter candidate with no ready base deployment -> skip + count."""
    conn = _fresh(tmp_path)
    # Register the base AND the adapter - but no deployment.
    model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen3-7b")
    _seed_adapter(conn, "tone-formal", "qwen3-7b", tmp_path)
    now = datetime.now(UTC).replace(tzinfo=None)
    for i in range(5):
        x_ts = now - timedelta(hours=1) - timedelta(minutes=i)
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'qwen3-7b', 'qwen3-7b', NULL)",
            (x_ts.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'tone-formal', 'qwen3-7b', 'tone-formal')",
            ((x_ts + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"),),
        )
    _seed_recent_event(conn, "qwen3-7b")

    cap = _intercept_engine_loads(monkeypatch)
    task = PredictorTask(
        conn=conn, backends={"vllm": VLLMBackend()},
        models_dir=tmp_path, config=_only_seq(),
    )
    triggered = await task.tick_once()

    assert triggered == 0
    assert cap == []
    assert task.preloads_skipped_no_deployment == 1


@pytest.mark.asyncio
async def test_tick_respects_max_prewarm_per_tick(tmp_path, monkeypatch):
    """When more candidates exceed budget, only max_prewarm_per_tick fire."""
    conn = _fresh(tmp_path)
    _, dep = _seed_deployment(conn, "qwen3-7b", max_loras=4)
    a1 = _seed_adapter(conn, "lora-a", "qwen3-7b", tmp_path)
    a2 = _seed_adapter(conn, "lora-b", "qwen3-7b", tmp_path)
    a3 = _seed_adapter(conn, "lora-c", "qwen3-7b", tmp_path)

    # Sequencing rule alone can only emit candidates that follow X; with
    # three different adapter follow-ups it gives us three candidates.
    now = datetime.now(UTC).replace(tzinfo=None)
    for i in range(6):
        x_ts = now - timedelta(hours=1) - timedelta(minutes=i)
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'qwen3-7b', 'qwen3-7b', NULL)",
            (x_ts.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        # Rotate through three adapters - each gets P~0.33.
        adapter_name = ("lora-a", "lora-b", "lora-c")[i % 3]
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, ?, 'qwen3-7b', ?)",
            (
                (x_ts + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"),
                adapter_name,
                adapter_name,
            ),
        )
    _seed_recent_event(conn, "qwen3-7b")

    cap = _intercept_engine_loads(monkeypatch)
    cfg = _only_seq()
    cfg = PredictorConfig(
        max_prewarm_per_tick=2,
        time_of_day=cfg.time_of_day,
        sequencing=SequencingConfig(window_s=30, min_p=0.2),
        key_affinity=cfg.key_affinity,
    )
    task = PredictorTask(
        conn=conn, backends={"vllm": VLLMBackend()},
        models_dir=tmp_path, config=cfg,
    )
    triggered = await task.tick_once()
    assert triggered == 2
    assert len(cap) == 2
    # Both loads went into the only ready deployment.
    for entry in cap:
        assert "49152" in entry["url"]
    # Junction rows for exactly two of the three.
    loaded = {
        n for n in ("lora-a", "lora-b", "lora-c")
        if dep.id in da_store.find_deployments_with_adapter(
            conn, ad_store.get_by_name(conn, n).id,
        )
    }
    assert len(loaded) == 2
    # Suppress unused warnings.
    _ = (a1, a2, a3)


@pytest.mark.asyncio
async def test_tick_disabled_predictor_is_noop(tmp_path, monkeypatch):
    """enabled=False short-circuits before the rules run."""
    conn = _fresh(tmp_path)
    _, _ = _seed_deployment(conn, "qwen3-7b")
    _seed_adapter(conn, "tone-formal", "qwen3-7b", tmp_path)
    _seed_recent_event(conn, "qwen3-7b")

    cap = _intercept_engine_loads(monkeypatch)
    cfg = _only_seq()
    cfg = PredictorConfig(
        enabled=False,
        time_of_day=cfg.time_of_day,
        sequencing=cfg.sequencing,
        key_affinity=cfg.key_affinity,
    )
    task = PredictorTask(
        conn=conn, backends={"vllm": VLLMBackend()},
        models_dir=tmp_path, config=cfg,
    )
    assert await task.tick_once() == 0
    assert cap == []


# ---- Base pre-warming (Workstream C follow-up) ----------------------------

from unittest.mock import AsyncMock  # noqa: E402

from berth.lifecycle.plan import DeploymentPlan  # noqa: E402
from berth.store import deployment_plans as plan_store  # noqa: E402


def _seed_plan(conn, base_name: str, *, max_loras: int = 4) -> int:
    """Insert a model row + a recorded successful plan for it; return plan id."""
    base = model_store.add(conn, name=base_name, hf_repo=f"o/{base_name}")
    plan = DeploymentPlan(
        model_name=base_name, hf_repo=f"o/{base_name}", revision="main",
        backend="vllm", image_tag="vllm:test", gpu_ids=[0],
        max_model_len=4096, tensor_parallel=1, dtype="auto",
        max_loras=max_loras, max_lora_rank=32,
    )
    pid = plan_store.record(conn, model_id=base.id, plan=plan)
    plan_store.mark_ready(conn, pid)
    return pid


@pytest.mark.asyncio
async def test_base_prewarm_fires_for_bare_base_candidate_with_history(
    tmp_path,
):
    """Sequencing produces a bare-base candidate; the tick reconstructs the
    plan from history and calls manager.load. No engine HTTP intercept
    needed - manager is mocked, so the load is a single coroutine call."""
    conn = _fresh(tmp_path)
    _seed_plan(conn, "qwen3-7b")
    # Sequencing history: another-base -> qwen3-7b (bare).
    now = datetime.now(UTC).replace(tzinfo=None)
    for i in range(5):
        x_ts = now - timedelta(hours=1) - timedelta(minutes=i)
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'another-base', 'another-base', NULL)",
            (x_ts.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'qwen3-7b', 'qwen3-7b', NULL)",
            ((x_ts + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"),),
        )
    _seed_recent_event(conn, "another-base")

    manager = AsyncMock()
    manager.load = AsyncMock()
    task = PredictorTask(
        conn=conn, backends={"vllm": VLLMBackend()},
        models_dir=tmp_path, config=_only_seq(), manager=manager,
    )
    await task.tick_once()
    # The base pre-warm is launched as a background task; await one event
    # loop turn so the fire-and-forget coroutine actually runs.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert task.base_prewarms_attempted == 1
    assert task.base_prewarms_succeeded == 1
    assert manager.load.call_count == 1
    submitted = manager.load.call_args[0][0]
    assert submitted.model_name == "qwen3-7b"
    assert submitted.max_loras == 4


@pytest.mark.asyncio
async def test_base_prewarm_skips_when_no_history(tmp_path):
    """Predictor surfaces a bare-base candidate but the model has never
    been loaded successfully. Skip + bump the no_plan counter so the
    operator can see why nothing happened."""
    conn = _fresh(tmp_path)
    # Register the model but record NO plan.
    model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen3-7b")
    now = datetime.now(UTC).replace(tzinfo=None)
    for i in range(5):
        x_ts = now - timedelta(hours=1) - timedelta(minutes=i)
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'another-base', 'another-base', NULL)",
            (x_ts.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'qwen3-7b', 'qwen3-7b', NULL)",
            ((x_ts + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"),),
        )
    _seed_recent_event(conn, "another-base")

    manager = AsyncMock()
    manager.load = AsyncMock()
    task = PredictorTask(
        conn=conn, backends={"vllm": VLLMBackend()},
        models_dir=tmp_path, config=_only_seq(), manager=manager,
    )
    await task.tick_once()
    await asyncio.sleep(0)

    assert task.base_prewarms_attempted == 0
    assert task.base_prewarms_skipped_no_plan == 1
    assert manager.load.call_count == 0


@pytest.mark.asyncio
async def test_base_prewarm_skips_when_deployment_already_ready(tmp_path):
    """The base is already serving. Don't spin up a duplicate - the
    predictor sees the bare-base candidate but the ready deployment
    short-circuits the path."""
    conn = _fresh(tmp_path)
    base, _ = _seed_deployment(conn, "qwen3-7b")
    # Record a plan against the *existing* model row so we don't double-
    # insert. The plan is still queryable; the predictor should ignore it
    # because the deployment is already ready.
    plan = DeploymentPlan(
        model_name="qwen3-7b", hf_repo="o/qwen3-7b", revision="main",
        backend="vllm", image_tag="vllm:test", gpu_ids=[0],
        max_model_len=4096, tensor_parallel=1, dtype="auto", max_loras=4,
    )
    pid = plan_store.record(conn, model_id=base.id, plan=plan)
    plan_store.mark_ready(conn, pid)

    now = datetime.now(UTC).replace(tzinfo=None)
    for i in range(5):
        x_ts = now - timedelta(hours=1) - timedelta(minutes=i)
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'another-base', 'another-base', NULL)",
            (x_ts.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'qwen3-7b', 'qwen3-7b', NULL)",
            ((x_ts + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"),),
        )
    _seed_recent_event(conn, "another-base")

    manager = AsyncMock()
    manager.load = AsyncMock()
    task = PredictorTask(
        conn=conn, backends={"vllm": VLLMBackend()},
        models_dir=tmp_path, config=_only_seq(), manager=manager,
    )
    await task.tick_once()
    await asyncio.sleep(0)

    assert manager.load.call_count == 0
    # Counter doesn't bump for "already-ready" - that's a happy outcome,
    # not a missing-plan miss. The skipped_no_plan counter must stay 0
    # so operators distinguish "predictor is doing nothing" from "the
    # base is up and predictor correctly noticed".
    assert task.base_prewarms_skipped_no_plan == 0


@pytest.mark.asyncio
async def test_base_prewarm_respects_zero_budget(tmp_path):
    """max_base_prewarm_per_tick=0 disables base pre-warming entirely  -
    operator opts out via predictor.yaml."""
    conn = _fresh(tmp_path)
    _seed_plan(conn, "qwen3-7b")
    now = datetime.now(UTC).replace(tzinfo=None)
    for i in range(5):
        x_ts = now - timedelta(hours=1) - timedelta(minutes=i)
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'another-base', 'another-base', NULL)",
            (x_ts.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, adapter_name) "
            "VALUES (?, 'qwen3-7b', 'qwen3-7b', NULL)",
            ((x_ts + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"),),
        )
    _seed_recent_event(conn, "another-base")

    cfg = PredictorConfig(
        max_base_prewarm_per_tick=0,
        time_of_day=RuleConfig(enabled=False),
        sequencing=SequencingConfig(window_s=30, min_p=0.30),
        key_affinity=KeyAffinityConfig(enabled=False),
    )
    manager = AsyncMock()
    manager.load = AsyncMock()
    task = PredictorTask(
        conn=conn, backends={"vllm": VLLMBackend()},
        models_dir=tmp_path, config=cfg, manager=manager,
    )
    await task.tick_once()
    await asyncio.sleep(0)

    assert manager.load.call_count == 0
    assert task.base_prewarms_attempted == 0
