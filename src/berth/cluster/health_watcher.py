from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from berth.cluster.agent_registry import AgentRegistry
from berth.routing.affinity import RoutingAffinity
from berth.store import nodes as nodes_store

log = logging.getLogger(__name__)


def on_node_unreachable(
    *,
    node_id: int,
    label: str,
    affinity: RoutingAffinity | None,
) -> None:
    """Side-effects on node ready → unreachable: emit an audit log line
    and clear any routing-affinity entries pointing at the lost node.
    Pure; safe to call from any context."""
    log.warning("node_loss_audit node_id=%d label=%r", node_id, label)
    if affinity is not None:
        affinity.evict_node(node_id)


def sweep(
    conn: sqlite3.Connection,
    registry: AgentRegistry,
    *,
    now: float | None = None,
    stale_after_s: float = 15.0,
    affinity: RoutingAffinity | None = None,
) -> None:
    """One pass over the nodes table: any node currently `ready` whose
    last_seen is older than `stale_after_s` is moved to `unreachable`,
    unregistered from the live AgentLink registry, and has its routing
    affinity entries cleared."""
    t = now if now is not None else time.time()
    for n in nodes_store.list_all(conn):
        if n.label == "local":
            continue  # local is reachable as long as the daemon is running
        if n.status == "ready" and (t - n.last_seen) > stale_after_s:
            nodes_store.set_status(
                conn, n.id, status="unreachable", last_seen=n.last_seen,
            )
            link = registry.get(n.id)
            if link is not None:
                # Identity-safe: if a fresh agent has reconnected between
                # snapshot and now, registry.unregister(link) returns False
                # and leaves the new link alone.
                registry.unregister(link)
            on_node_unreachable(node_id=n.id, label=n.label, affinity=affinity)


async def run_health_watcher(
    conn: sqlite3.Connection,
    registry: AgentRegistry,
    *,
    interval_s: float = 5.0,
    stale_after_s: float = 15.0,
    affinity: RoutingAffinity | None = None,
) -> None:
    """Run sweep() in a loop until cancelled."""
    while True:
        try:
            sweep(
                conn, registry,
                stale_after_s=stale_after_s, affinity=affinity,
            )
        except Exception:
            log.exception("health watcher sweep failed")
        await asyncio.sleep(interval_s)
