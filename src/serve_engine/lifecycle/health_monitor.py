from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable

import httpx

from serve_engine.backends.base import Backend
from serve_engine.store import deployments as dep_store

log = logging.getLogger(__name__)


def _default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=5.0)


class HealthMonitor:
    """Periodically re-probe ready deployments and flip to 'failed' after K
    consecutive probe failures.

    Once a deployment reaches 'ready' the manager does not re-check the
    engine. If the container silently dies (OOM, segfault, NCCL hang) the
    DB row stays 'ready' until the next real request fails. This task
    closes that gap so the deployment state reflects engine liveness.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        backends: dict[str, Backend],
        manager,
        interval_s: float = 30.0,
        max_consecutive_failures: int = 3,
        client_factory: Callable[[], httpx.AsyncClient] = _default_client_factory,
    ):
        self._conn = conn
        self._backends = backends
        self._manager = manager
        self._interval_s = interval_s
        self._max_failures = max_consecutive_failures
        self._client_factory = client_factory
        # deployment_id -> consecutive failure count
        self._failures: dict[int, int] = {}
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def tick_once(self) -> None:
        ready = dep_store.list_ready(self._conn)
        seen: set[int] = set()
        async with self._client_factory() as client:
            for d in ready:
                seen.add(d.id)
                backend = self._backends.get(d.backend)
                if backend is None:
                    continue
                if d.container_address is None or d.container_port is None:
                    continue
                ok = False
                url: str
                if d.container_address == "tunnel":
                    # Remote deployment — probe through the AgentLink.
                    # The agent does a localhost httpx call inside its
                    # own host and ships back the status.
                    url = f"<tunnel>{backend.health_path}"
                    registry = getattr(
                        self._manager, "_registry", None,
                    )
                    link = registry.get(d.node_id) if registry else None
                    if link is None or not link.is_ready:
                        # Agent disconnected — don't penalise the row; the
                        # node-status watcher already flagged it.
                        continue
                    if d.container_id is None:
                        continue
                    try:
                        status_code = await link.probe_container(
                            container_id=d.container_id,
                            path=backend.health_path,
                        )
                        ok = 200 <= status_code < 300
                    except Exception:
                        ok = False
                else:
                    url = (
                        f"http://{d.container_address}:{d.container_port}"
                        f"{backend.health_path}"
                    )
                    try:
                        r = await client.get(url)
                        ok = 200 <= r.status_code < 300
                    except (httpx.HTTPError, OSError):
                        ok = False

                if ok:
                    if d.id in self._failures:
                        self._failures.pop(d.id, None)
                    continue

                count = self._failures.get(d.id, 0) + 1
                self._failures[d.id] = count
                log.warning(
                    "health_monitor: deployment %s probe failed (%d/%d) url=%s",
                    d.id, count, self._max_failures, url,
                )
                if count >= self._max_failures:
                    msg = f"health probe failed {count} times"
                    log.error(
                        "health_monitor: marking deployment %s failed: %s",
                        d.id, msg,
                    )
                    try:
                        dep_store.update_status(
                            self._conn, d.id, "failed", last_error=msg,
                        )
                        await self._manager._emit(
                            "deployment.unhealthy", dep_id=d.id,
                        )
                    except Exception:
                        log.exception(
                            "health_monitor: failed to mark deployment %s failed",
                            d.id,
                        )
                    self._failures.pop(d.id, None)

        # Prune entries for deployments no longer in ready (stopped, failed,
        # replaced, etc.) so the dict doesn't grow without bound across churn.
        stale = [dep_id for dep_id in self._failures if dep_id not in seen]
        for dep_id in stale:
            self._failures.pop(dep_id, None)

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick_once()
            except Exception:
                log.exception("health_monitor tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_s,
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
