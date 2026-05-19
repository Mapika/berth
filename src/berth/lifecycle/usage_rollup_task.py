"""Daemon task that rolls old usage_events into usage_aggregates."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from berth.lifecycle.predictor import PredictorConfig
from berth.store import usage_aggregates as ua_store

log = logging.getLogger(__name__)


class UsageRollupTask:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        config: PredictorConfig | None = None,
        tick_s: float = 24 * 3600.0,
    ):
        self._conn = conn
        self._config = config or PredictorConfig()
        self._tick_s = tick_s
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def tick_once(self) -> dict:
        """Roll up everything older than retention_days. Returns the
        rollup counters for telemetry."""
        cutoff = (
            datetime.now(UTC).replace(tzinfo=None)
            - timedelta(days=self._config.retention_days)
        ).strftime("%Y-%m-%d %H:%M:%S")
        return ua_store.rollup_old_events(self._conn, before_iso=cutoff)

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = await self.tick_once()
                if result["events_deleted"] > 0:
                    log.info(
                        "usage rollup: %d buckets upserted, %d raw events dropped",
                        result["buckets_upserted"], result["events_deleted"],
                    )
            except Exception:
                log.exception("usage rollup tick failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._tick_s)
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
