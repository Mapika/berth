"""History of DeploymentPlan instances submitted to the manager.

One row per `manager.load(plan)` call, captured as JSON so we don't lose
fields the `deployments` row doesn't store (extra_args,
gpu_memory_utilization, enable_*). The predictor mines this to
reconstruct a plan when it wants to pre-warm a base from scratch - the
deployments table alone isn't sufficient because:

- extra_args is operator-supplied (`-x '--max-lora-rank=N'`) and the
  current schema doesn't store it,
- rows for stopped deployments stick around but rotate when the same
  model is re-launched, so a multi-month-old plan may have been deleted,
- gpu_memory_utilization is computed at placement time and not part of
  what the operator specified - replaying needs the *operator's* intent,
  not the manager's resolved values.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class DeploymentPlanRecord:
    id: int
    model_id: int
    plan_json: str
    deployment_id: int | None
    reached_ready_at: str | None
    created_at: str


def _row(row: sqlite3.Row) -> DeploymentPlanRecord:
    return DeploymentPlanRecord(
        id=row["id"],
        model_id=row["model_id"],
        plan_json=row["plan_json"],
        deployment_id=row["deployment_id"],
        reached_ready_at=row["reached_ready_at"],
        created_at=row["created_at"],
    )


def record(
    conn: sqlite3.Connection,
    *,
    model_id: int,
    plan,  # DeploymentPlan - typed loosely to avoid a circular import
    deployment_id: int | None = None,
) -> int:
    """Persist a plan submitted via `manager.load(plan)`. Returns the new
    row id; the caller uses it with `mark_ready` once health-check passes.

    `plan` is serialized via `dataclasses.asdict` so the predictor can
    rehydrate it with `DeploymentPlan(**json.loads(plan_json))` without
    a custom codec - DeploymentPlan is a frozen dataclass of JSON-safe
    primitives + a `dict[str, str]` extra_args.
    """
    plan_json = json.dumps(asdict(plan), sort_keys=True)
    cur = conn.execute(
        """
        INSERT INTO deployment_plans (model_id, plan_json, deployment_id)
        VALUES (?, ?, ?)
        """,
        (model_id, plan_json, deployment_id),
    )
    return int(cur.lastrowid or 0)


def mark_ready(conn: sqlite3.Connection, plan_id: int) -> None:
    """Flip `reached_ready_at = CURRENT_TIMESTAMP`. Called by the manager
    only after the engine's healthz answers - failed loads don't pollute
    the history with un-replayable plans."""
    conn.execute(
        "UPDATE deployment_plans SET reached_ready_at=CURRENT_TIMESTAMP "
        "WHERE id=?",
        (plan_id,),
    )


def most_recent_ready_for_model(
    conn: sqlite3.Connection, model_id: int,
) -> DeploymentPlanRecord | None:
    """The last plan that actually became healthy for this model. The
    predictor uses this to choose flags for a base pre-warm. Returns None
    if the model has never had a successful load - pre-warming is unsafe
    in that case (we don't know what config works)."""
    row = conn.execute(
        """
        SELECT * FROM deployment_plans
        WHERE model_id=? AND reached_ready_at IS NOT NULL
        ORDER BY reached_ready_at DESC
        LIMIT 1
        """,
        (model_id,),
    ).fetchone()
    return _row(row) if row else None


def list_for_model(
    conn: sqlite3.Connection, model_id: int,
) -> list[DeploymentPlanRecord]:
    rows = conn.execute(
        "SELECT * FROM deployment_plans WHERE model_id=? ORDER BY id DESC",
        (model_id,),
    ).fetchall()
    return [_row(r) for r in rows]
