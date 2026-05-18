from __future__ import annotations

import sqlite3
import time

from serve_engine.cluster import host_info as hi
from serve_engine.store import node_gpus as node_gpus_store
from serve_engine.store import nodes as nodes_store


def ensure_local_node(conn: sqlite3.Connection, *, agent_version: str) -> int:
    """Ensure a 'local' node row exists and reflects current host inventory.

    Idempotent: safe to call on every daemon startup. The local node has
    label='local' and fingerprint='local' as its durable identity; the
    integer id is assigned by AUTOINCREMENT on first insert.
    """
    info = hi.collect_host_info()
    now = time.time()
    existing = nodes_store.find_by_label(conn, "local")
    if existing is None:
        node_id = nodes_store.insert(
            conn,
            label="local", fingerprint="local",
            reachable_as=None,
            first_seen=now, last_seen=now,
            agent_version=agent_version,
            cpu_count=info.cpu_count,
            total_ram_mb=info.total_ram_mb,
            gpu_count=info.gpu_count,
            total_vram_mb=info.total_vram_mb,
        )
    else:
        node_id = existing.id
        nodes_store.update_inventory(
            conn, node_id,
            agent_version=agent_version,
            cpu_count=info.cpu_count,
            total_ram_mb=info.total_ram_mb,
            gpu_count=info.gpu_count,
            total_vram_mb=info.total_vram_mb,
        )
    nodes_store.set_status(conn, node_id, status="ready", last_seen=now)
    node_gpus_store.delete_for_node(conn, node_id)
    for g in info.gpus:
        node_gpus_store.upsert(
            conn, node_id=node_id, gpu_index=g.index,
            name=g.name, total_vram_mb=g.total_vram_mb,
            driver_version=g.driver_version,
        )
    return node_id
