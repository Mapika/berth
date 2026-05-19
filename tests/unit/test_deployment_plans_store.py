"""Store-level coverage for deployment_plans history.

The predictor's base pre-warm path looks up `most_recent_ready_for_model`
and trusts the JSON it stores round-trips into a DeploymentPlan. Tests
lock down: only ready-marked rows are returned, ordering is by
reached_ready_at not insertion id, and a never-ready model returns None.
"""
from __future__ import annotations

import json

from serve_engine.lifecycle.plan import DeploymentPlan
from serve_engine.store import db
from serve_engine.store import deployment_plans as plan_store
from serve_engine.store import deployments as dep_store
from serve_engine.store import models as model_store


def _conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def _plan(model_name: str = "qwen3-7b", **overrides) -> DeploymentPlan:
    defaults = {
        "model_name": model_name,
        "hf_repo": "o/qwen3-7b",
        "revision": "main",
        "backend": "vllm",
        "image_tag": "vllm:test",
        "gpu_ids": [0],
        "max_model_len": 4096,
        "tensor_parallel": 1,
        "dtype": "auto",
        "max_loras": 4,
        "max_lora_rank": 32,
        "extra_args": {"--max-lora-rank": "64"},
    }
    defaults.update(overrides)
    return DeploymentPlan(**defaults)


def test_record_persists_plan_as_json(tmp_path):
    conn = _conn(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen3-7b")
    pid = plan_store.record(conn, model_id=base.id, plan=_plan())
    rows = plan_store.list_for_model(conn, base.id)
    assert len(rows) == 1
    parsed = json.loads(rows[0].plan_json)
    assert parsed["model_name"] == "qwen3-7b"
    assert parsed["extra_args"] == {"--max-lora-rank": "64"}
    assert parsed["max_loras"] == 4
    # `reached_ready_at` stays NULL until the load succeeds - failed plans
    # must not show up as a viable base to replay.
    assert rows[0].reached_ready_at is None
    assert pid > 0


def test_most_recent_ready_skips_unready_rows(tmp_path):
    """A failed load leaves the row in place but reached_ready_at NULL.
    most_recent_ready_for_model must skip those - replaying a plan that
    never worked is worse than not pre-warming at all."""
    conn = _conn(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen3-7b")
    pid1 = plan_store.record(conn, model_id=base.id, plan=_plan())
    plan_store.record(conn, model_id=base.id, plan=_plan())  # never reaches ready
    plan_store.mark_ready(conn, pid1)

    rec = plan_store.most_recent_ready_for_model(conn, base.id)
    assert rec is not None
    assert rec.id == pid1
    assert rec.reached_ready_at is not None


def test_most_recent_ready_returns_latest_when_multiple(tmp_path):
    """When the operator runs `serve run X` repeatedly, each successful
    load adds a row. The newest ready plan wins so config tweaks
    propagate to the next pre-warm."""
    conn = _conn(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen3-7b")
    pid1 = plan_store.record(conn, model_id=base.id, plan=_plan(max_loras=2))
    plan_store.mark_ready(conn, pid1)
    conn.execute(
        "UPDATE deployment_plans SET reached_ready_at=? WHERE id=?",
        ("2000-01-01 00:00:00", pid1),
    )
    pid2 = plan_store.record(conn, model_id=base.id, plan=_plan(max_loras=8))
    plan_store.mark_ready(conn, pid2)

    rec = plan_store.most_recent_ready_for_model(conn, base.id)
    assert rec is not None
    assert rec.id == pid2
    assert json.loads(rec.plan_json)["max_loras"] == 8


def test_most_recent_ready_returns_none_for_unknown_model(tmp_path):
    conn = _conn(tmp_path)
    assert plan_store.most_recent_ready_for_model(conn, model_id=999) is None


def test_plan_json_round_trips_into_DeploymentPlan(tmp_path):
    """The predictor reconstructs a DeploymentPlan from this row; the
    serialized field set must include every required ctor parameter and
    type-roundtrip without help."""
    conn = _conn(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen3-7b")
    plan = _plan()
    pid = plan_store.record(conn, model_id=base.id, plan=plan)
    plan_store.mark_ready(conn, pid)

    rec = plan_store.most_recent_ready_for_model(conn, base.id)
    assert rec is not None
    restored = DeploymentPlan(**json.loads(rec.plan_json))
    assert restored == plan


def test_cascades_with_deployment_deletion(tmp_path):
    """When the FK'd deployment row is deleted, deployment_id goes NULL
    but the plan row stays - historical plans must survive cleanup of
    their original deployment so we can still replay them."""
    conn = _conn(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen3-7b")
    dep = dep_store.create(
        conn, model_id=base.id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
    )
    pid = plan_store.record(
        conn, model_id=base.id, plan=_plan(), deployment_id=dep.id,
    )
    plan_store.mark_ready(conn, pid)

    conn.execute("DELETE FROM deployments WHERE id=?", (dep.id,))
    rec = plan_store.most_recent_ready_for_model(conn, base.id)
    assert rec is not None
    assert rec.deployment_id is None
