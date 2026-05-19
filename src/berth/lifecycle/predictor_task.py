"""Predictor tick loop for adapter and base pre-warming.

Predictions are advisory: the task only uses ready deployments or recorded
successful plans, and a real request always wins.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from berth.backends.base import Backend
from berth.lifecycle.adapter_router import (
    ensure_adapter_loaded,
    find_deployment_for,
)
from berth.lifecycle.manager import LifecycleManager
from berth.lifecycle.plan import DeploymentPlan
from berth.lifecycle.predictor import Predictor, PredictorConfig
from berth.store import adapters as ad_store
from berth.store import deployment_adapters as da_store
from berth.store import deployment_plans as plan_store
from berth.store import deployments as dep_store
from berth.store import models as model_store

log = logging.getLogger(__name__)


class PredictorTask:
    """Long-running predictor loop with per-tick budgets."""

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        backends: dict[str, Backend],
        models_dir: Path,
        config: PredictorConfig | None = None,
        manager: LifecycleManager | None = None,
    ):
        self._conn = conn
        self._backends = backends
        self._models_dir = models_dir
        self._config = config or PredictorConfig()
        self._manager = manager
        self._predictor = Predictor(conn, config=self._config)
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self.preloads_attempted = 0
        self.preloads_succeeded = 0
        self.preloads_skipped_already_warm = 0
        self.preloads_skipped_no_deployment = 0
        self.base_prewarms_attempted = 0
        self.base_prewarms_succeeded = 0
        self.base_prewarms_skipped_no_plan = 0
        # Strong refs to fire-and-forget base pre-warm tasks. Without this,
        # the event loop may GC the task mid-load; we drop completed ones
        # opportunistically so the list doesn't grow unbounded.
        self._inflight_base_loads: set[asyncio.Task] = set()

    def candidates_snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "base_name": c.base_name,
                "adapter_name": c.adapter_name,
                "score": round(c.score, 4),
                "reason": c.reason,
            }
            for c in self._predictor.candidates()
        ]

    def stats_snapshot(self) -> dict[str, object]:
        return {
            "enabled": self._config.enabled,
            "tick_interval_s": self._config.tick_interval_s,
            "max_prewarm_per_tick": self._config.max_prewarm_per_tick,
            "max_base_prewarm_per_tick": self._config.max_base_prewarm_per_tick,
            "preloads_attempted": self.preloads_attempted,
            "preloads_succeeded": self.preloads_succeeded,
            "preloads_skipped_already_warm": self.preloads_skipped_already_warm,
            "preloads_skipped_no_deployment": self.preloads_skipped_no_deployment,
            "base_prewarms_attempted": self.base_prewarms_attempted,
            "base_prewarms_succeeded": self.base_prewarms_succeeded,
            "base_prewarms_skipped_no_plan": self.base_prewarms_skipped_no_plan,
        }

    async def tick_once(self) -> int:
        """Run one prediction pass; return the number of preloads
        actually triggered. Caller wraps in exception handling."""
        if not self._config.enabled:
            return 0
        candidates = self._predictor.candidates()
        triggered = 0
        budget = self._config.max_prewarm_per_tick
        base_budget = self._config.max_base_prewarm_per_tick
        base_triggered = 0
        for c in candidates:
            if triggered >= budget and base_triggered >= base_budget:
                break
            if c.adapter_name is None:
                if base_triggered >= base_budget:
                    continue
                if await self._try_prewarm_base(c.base_name, c.reason):
                    base_triggered += 1
                continue
            dep = find_deployment_for(self._conn, c.base_name, c.adapter_name)
            if dep is None or dep.container_address is None:
                self.preloads_skipped_no_deployment += 1
                if base_triggered < base_budget:
                    if await self._try_prewarm_base(
                        c.base_name,
                        f"base for adapter pre-warm ({c.adapter_name}): {c.reason}",
                    ):
                        base_triggered += 1
                continue
            if triggered >= budget:
                continue
            a = ad_store.get_by_name(self._conn, c.adapter_name)
            if a is None:
                continue
            if dep.id in da_store.find_deployments_with_adapter(self._conn, a.id):
                self.preloads_skipped_already_warm += 1
                continue
            backend = self._backends.get(dep.backend)
            if backend is None:
                continue
            self.preloads_attempted += 1
            try:
                await ensure_adapter_loaded(
                    self._conn, backend, dep, c.adapter_name,
                    models_dir=self._models_dir,
                )
                self.preloads_succeeded += 1
                triggered += 1
                log.info(
                    "predictor preloaded adapter %r into dep #%d (%s)",
                    c.adapter_name, dep.id, c.reason,
                )
            except Exception as e:
                log.warning(
                    "predictor failed to preload %r into dep #%d: %s",
                    c.adapter_name, dep.id, e,
                )
        return triggered

    async def _try_prewarm_base(self, base_name: str, reason: str) -> bool:
        """Launch a base deployment from its most recent successful plan."""
        if self._manager is None or self._config.max_base_prewarm_per_tick <= 0:
            return False
        manager = self._manager

        model = model_store.get_by_name(self._conn, base_name)
        if model is None:
            return False

        for d in dep_store.list_ready(self._conn):
            if d.model_id == model.id:
                return False
        loading = self._conn.execute(
            "SELECT 1 FROM deployments WHERE model_id=? AND status='loading'",
            (model.id,),
        ).fetchone()
        if loading is not None:
            return False

        record = plan_store.most_recent_ready_for_model(self._conn, model.id)
        if record is None:
            self.base_prewarms_skipped_no_plan += 1
            return False

        try:
            plan_dict = json.loads(record.plan_json)
            plan = DeploymentPlan(**plan_dict)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            log.warning(
                "predictor base pre-warm: bad plan_json id=%d: %s",
                record.id, e,
            )
            return False

        self.base_prewarms_attempted += 1
        async def _run() -> None:
            try:
                await manager.load(plan)
                self.base_prewarms_succeeded += 1
                log.info(
                    "predictor pre-warmed base %r from plan #%d (%s)",
                    base_name, record.id, reason,
                )
            except Exception as e:
                log.warning(
                    "predictor base pre-warm of %r failed: %s",
                    base_name, e,
                )

        task = asyncio.create_task(_run())
        self._inflight_base_loads.add(task)
        task.add_done_callback(self._inflight_base_loads.discard)
        return True

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick_once()
            except Exception:
                log.exception("predictor tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=float(self._config.tick_interval_s),
                )
            except TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None
