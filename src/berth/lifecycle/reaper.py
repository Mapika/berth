from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC

log = logging.getLogger(__name__)


class Reaper:
    """Periodically evict ready non-pinned deployments past their idle timeout."""

    def __init__(
        self,
        *,
        manager,
        list_ready: Callable,
        default_idle_timeout_s: int = 300,
        tick_s: float = 30.0,
        now_fn: Callable[[], float] = time.time,
    ):
        self._manager = manager
        self._list_ready = list_ready
        self._default_idle_timeout_s = default_idle_timeout_s
        self._tick_s = tick_s
        self._now = now_fn
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def tick_once(self) -> None:
        now = self._now()
        for d in self._list_ready():
            if d.pinned:
                continue
            if d.status != "ready":
                continue
            last = d.last_request_at
            if last is None:
                continue
            try:
                last_ts = (
                    last if isinstance(last, (int, float))
                    else _parse_sqlite_ts(last)
                )
            except Exception:
                continue
            idle = now - last_ts
            timeout = d.idle_timeout_s or self._default_idle_timeout_s
            if idle >= timeout:
                log.info("reaper evicting deployment %s (idle %.0fs)", d.id, idle)
                try:
                    await self._manager.stop(d.id)
                except Exception:
                    log.exception("reaper failed to evict %s", d.id)

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick_once()
            except Exception:
                log.exception("reaper tick failed")
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


def _parse_sqlite_ts(s: str) -> float:
    """Parse 'YYYY-MM-DD HH:MM:SS' (UTC) returned by SQLite CURRENT_TIMESTAMP."""
    from datetime import datetime
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()
