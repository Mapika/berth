"""Daemon task that rolls aged request_metrics into request_metrics_hourly and
purges old hourly rows. Mirrors UsageRollupTask."""
from __future__ import annotations

import asyncio
import logging
import sqlite3

from berth.lifecycle.predictor import PredictorConfig
from berth.store import request_metrics_rollup as rmr
from berth.store.request_metrics import RAW_RETENTION_H

log = logging.getLogger(__name__)


class MetricsRollupTask:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        config: PredictorConfig | None = None,
        tick_s: float = 3600.0,
    ):
        self._conn = conn
        self._config = config or PredictorConfig()
        self._tick_s = tick_s
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def tick_once(self) -> dict:
        res = rmr.rollup_aged_raw(self._conn, older_than_h=RAW_RETENTION_H)
        res["hourly_purged"] = rmr.purge_hourly_older_than(
            self._conn, days=self._config.retention_days,
        )
        return res

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                r = await self.tick_once()
                if r["raw_deleted"] or r["hourly_purged"]:
                    log.info(
                        "metrics rollup: %d raw rolled, %d hourly purged",
                        r["raw_deleted"], r["hourly_purged"],
                    )
            except Exception:
                log.exception("metrics rollup tick failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._tick_s)
            except TimeoutError:
                pass

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            await self._task
