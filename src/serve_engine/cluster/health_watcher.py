from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.store import nodes as nodes_store

log = logging.getLogger(__name__)


def sweep(
    conn: sqlite3.Connection,
    registry: AgentRegistry,
    *,
    now: float | None = None,
    stale_after_s: float = 15.0,
) -> None:
    """One pass over the nodes table: any node currently `ready` whose
    last_seen is older than `stale_after_s` is moved to `unreachable` and
    unregistered from the live AgentLink registry."""
    t = now if now is not None else time.time()
    for n in nodes_store.list_all(conn):
        if n.label == "local":
            continue  # local is reachable as long as the daemon is running
        if n.status == "ready" and (t - n.last_seen) > stale_after_s:
            nodes_store.set_status(
                conn, n.id, status="unreachable", last_seen=n.last_seen,
            )
            registry.unregister(n.id)


async def run_health_watcher(
    conn: sqlite3.Connection,
    registry: AgentRegistry,
    *,
    interval_s: float = 5.0,
    stale_after_s: float = 15.0,
) -> None:
    """Run sweep() in a loop until cancelled."""
    while True:
        try:
            sweep(conn, registry, stale_after_s=stale_after_s)
        except Exception:
            log.exception("health watcher sweep failed")
        await asyncio.sleep(interval_s)
