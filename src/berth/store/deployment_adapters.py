from __future__ import annotations

import sqlite3

from berth.store.adapters import Adapter, _row_to_adapter


def attach(conn: sqlite3.Connection, dep_id: int, adapter_id: int) -> None:
    """Mark `adapter_id` as loaded into `dep_id`. Idempotent - calling on
    an already-attached pair touches `loaded_at` and `last_used_at`."""
    conn.execute(
        """
        INSERT INTO deployment_adapters (deployment_id, adapter_id)
        VALUES (?, ?)
        ON CONFLICT(deployment_id, adapter_id) DO UPDATE
        SET loaded_at=CURRENT_TIMESTAMP, last_used_at=CURRENT_TIMESTAMP
        """,
        (dep_id, adapter_id),
    )


def detach(conn: sqlite3.Connection, dep_id: int, adapter_id: int) -> None:
    conn.execute(
        "DELETE FROM deployment_adapters WHERE deployment_id=? AND adapter_id=?",
        (dep_id, adapter_id),
    )


def detach_all(conn: sqlite3.Connection, dep_id: int) -> None:
    """Drop every adapter attachment for a deployment. Called when the
    deployment is being torn down - CASCADE handles this automatically
    on row delete, but the manager calls it explicitly during the
    `stopping` phase so events fire and any in-flight unload requests
    don't race the row deletion."""
    conn.execute(
        "DELETE FROM deployment_adapters WHERE deployment_id=?", (dep_id,),
    )


def touch(conn: sqlite3.Connection, dep_id: int, adapter_id: int) -> None:
    """Record per-request use; drives the LRU within a deployment."""
    conn.execute(
        """
        UPDATE deployment_adapters
        SET last_used_at=CURRENT_TIMESTAMP
        WHERE deployment_id=? AND adapter_id=?
        """,
        (dep_id, adapter_id),
    )


def list_for_deployment(conn: sqlite3.Connection, dep_id: int) -> list[Adapter]:
    rows = conn.execute(
        """
        SELECT a.* FROM adapters a
        JOIN deployment_adapters da ON da.adapter_id = a.id
        WHERE da.deployment_id=?
        ORDER BY da.last_used_at DESC
        """,
        (dep_id,),
    ).fetchall()
    return [_row_to_adapter(conn, r) for r in rows]


def lru_for_deployment(conn: sqlite3.Connection, dep_id: int) -> Adapter | None:
    """Return the least-recently-used adapter loaded into this deployment.
    Used to pick an eviction victim when adapter slots are full."""
    rows = conn.execute(
        """
        SELECT a.* FROM adapters a
        JOIN deployment_adapters da ON da.adapter_id = a.id
        WHERE da.deployment_id=?
        ORDER BY da.last_used_at ASC
        LIMIT 1
        """,
        (dep_id,),
    ).fetchall()
    if not rows:
        return None
    return _row_to_adapter(conn, rows[0])


def find_deployments_with_adapter(
    conn: sqlite3.Connection, adapter_id: int,
) -> list[int]:
    rows = conn.execute(
        "SELECT deployment_id FROM deployment_adapters WHERE adapter_id=?",
        (adapter_id,),
    ).fetchall()
    return [r["deployment_id"] for r in rows]


def count_for_deployment(conn: sqlite3.Connection, dep_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM deployment_adapters WHERE deployment_id=?",
        (dep_id,),
    ).fetchone()
    return int(row["n"]) if row and row["n"] is not None else 0
