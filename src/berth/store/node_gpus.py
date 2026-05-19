from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class NodeGpu:
    node_id: int
    gpu_index: int
    name: str
    total_vram_mb: int
    driver_version: str | None


def upsert(
    conn: sqlite3.Connection,
    *,
    node_id: int,
    gpu_index: int,
    name: str,
    total_vram_mb: int,
    driver_version: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO node_gpus (node_id, gpu_index, name, total_vram_mb, driver_version)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(node_id, gpu_index) DO UPDATE SET
            name = excluded.name,
            total_vram_mb = excluded.total_vram_mb,
            driver_version = excluded.driver_version
        """,
        (node_id, gpu_index, name, total_vram_mb, driver_version),
    )


def list_for_node(conn: sqlite3.Connection, node_id: int) -> list[NodeGpu]:
    cur = conn.execute(
        "SELECT node_id, gpu_index, name, total_vram_mb, driver_version "
        "FROM node_gpus WHERE node_id = ? ORDER BY gpu_index",
        (node_id,),
    )
    return [NodeGpu(*r) for r in cur.fetchall()]


def delete_for_node(conn: sqlite3.Connection, node_id: int) -> None:
    conn.execute("DELETE FROM node_gpus WHERE node_id = ?", (node_id,))
