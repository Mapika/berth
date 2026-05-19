from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    id: int
    label: str
    fingerprint: str
    reachable_as: str | None
    status: str
    first_seen: float
    last_seen: float
    agent_version: str | None
    cpu_count: int
    total_ram_mb: int
    gpu_count: int
    total_vram_mb: int


_COLUMNS = (
    "id, label, fingerprint, reachable_as, status, "
    "first_seen, last_seen, agent_version, "
    "cpu_count, total_ram_mb, gpu_count, total_vram_mb"
)


def _row_to_node(row: sqlite3.Row) -> Node:
    return Node(
        id=row["id"],
        label=row["label"],
        fingerprint=row["fingerprint"],
        reachable_as=row["reachable_as"],
        status=row["status"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        agent_version=row["agent_version"],
        cpu_count=row["cpu_count"],
        total_ram_mb=row["total_ram_mb"],
        gpu_count=row["gpu_count"],
        total_vram_mb=row["total_vram_mb"],
    )


def insert(
    conn: sqlite3.Connection,
    *,
    label: str,
    fingerprint: str,
    reachable_as: str | None,
    first_seen: float,
    last_seen: float,
    agent_version: str | None,
    cpu_count: int,
    total_ram_mb: int,
    gpu_count: int,
    total_vram_mb: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO nodes (
            label, fingerprint, reachable_as, status,
            first_seen, last_seen, agent_version,
            cpu_count, total_ram_mb, gpu_count, total_vram_mb
        ) VALUES (?, ?, ?, 'unreachable', ?, ?, ?, ?, ?, ?, ?)
        """,
        (label, fingerprint, reachable_as,
         first_seen, last_seen, agent_version,
         cpu_count, total_ram_mb, gpu_count, total_vram_mb),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def get(conn: sqlite3.Connection, node_id: int) -> Node | None:
    cur = conn.execute(f"SELECT {_COLUMNS} FROM nodes WHERE id = ?", (node_id,))
    row = cur.fetchone()
    return _row_to_node(row) if row else None


def find_by_label(conn: sqlite3.Connection, label: str) -> Node | None:
    cur = conn.execute(f"SELECT {_COLUMNS} FROM nodes WHERE label = ?", (label,))
    row = cur.fetchone()
    return _row_to_node(row) if row else None


def find_by_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> Node | None:
    cur = conn.execute(
        f"SELECT {_COLUMNS} FROM nodes WHERE fingerprint = ?", (fingerprint,),
    )
    row = cur.fetchone()
    return _row_to_node(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[Node]:
    cur = conn.execute(f"SELECT {_COLUMNS} FROM nodes ORDER BY id")
    return [_row_to_node(r) for r in cur.fetchall()]


def set_status(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    status: str,
    last_seen: float,
) -> None:
    conn.execute(
        "UPDATE nodes SET status = ?, last_seen = ? WHERE id = ?",
        (status, last_seen, node_id),
    )


def update_inventory(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    agent_version: str | None,
    cpu_count: int,
    total_ram_mb: int,
    gpu_count: int,
    total_vram_mb: int,
) -> None:
    conn.execute(
        """UPDATE nodes
           SET agent_version = ?, cpu_count = ?, total_ram_mb = ?,
               gpu_count = ?, total_vram_mb = ?
           WHERE id = ?""",
        (agent_version, cpu_count, total_ram_mb,
         gpu_count, total_vram_mb, node_id),
    )


def delete(conn: sqlite3.Connection, node_id: int) -> None:
    conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
