"""Bounded rollup of usage_events for the predictor.

After `retention_days`, the rollup job collapses raw events into
(base, adapter, hour_of_week) buckets here and drops the raw rows.
Per design section12: keeps the predictor's storage bounded as the box runs
for months.

Schema: migration 008.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class UsageAggregate:
    id: int
    base_name: str
    adapter_name: str | None
    hour_of_week: int
    count: int
    last_rollup_at: str


def upsert(
    conn: sqlite3.Connection,
    *,
    base_name: str,
    adapter_name: str | None,
    hour_of_week: int,
    count_delta: int,
) -> None:
    """Add `count_delta` to the bucket (creating it if missing).

    Uses ON CONFLICT against the partial-unique index on
    (base_name, COALESCE(adapter_name, ''), hour_of_week).
    """
    if count_delta <= 0:
        return
    conn.execute(
        """
        INSERT INTO usage_aggregates (base_name, adapter_name, hour_of_week, count)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(base_name, COALESCE(adapter_name, ''), hour_of_week)
        DO UPDATE SET
            count = count + excluded.count,
            last_rollup_at = CURRENT_TIMESTAMP
        """,
        (base_name, adapter_name, hour_of_week, count_delta),
    )


def get_bucket(
    conn: sqlite3.Connection,
    *,
    base_name: str,
    adapter_name: str | None,
    hour_of_week: int,
) -> UsageAggregate | None:
    row = conn.execute(
        """
        SELECT * FROM usage_aggregates
        WHERE base_name = ?
          AND COALESCE(adapter_name, '') = COALESCE(?, '')
          AND hour_of_week = ?
        """,
        (base_name, adapter_name, hour_of_week),
    ).fetchone()
    if row is None:
        return None
    return UsageAggregate(
        id=row["id"],
        base_name=row["base_name"],
        adapter_name=row["adapter_name"],
        hour_of_week=row["hour_of_week"],
        count=int(row["count"]),
        last_rollup_at=row["last_rollup_at"],
    )


def list_all(conn: sqlite3.Connection) -> list[UsageAggregate]:
    rows = conn.execute(
        "SELECT * FROM usage_aggregates ORDER BY base_name, hour_of_week",
    ).fetchall()
    return [
        UsageAggregate(
            id=r["id"],
            base_name=r["base_name"],
            adapter_name=r["adapter_name"],
            hour_of_week=r["hour_of_week"],
            count=int(r["count"]),
            last_rollup_at=r["last_rollup_at"],
        )
        for r in rows
    ]


def rollup_old_events(
    conn: sqlite3.Connection, *, before_iso: str,
) -> dict:
    """Aggregate every event with ts < before_iso into usage_aggregates,
    then DELETE the source rows. Returns counters for telemetry.

    Done in a single transaction - partial rollups would re-count
    events on retry. Uses SQLite's hour_of_week formula
    (weekday * 24 + hour) so the predictor's SQL filter agrees.
    """
    # Group the soon-to-be-rolled-up rows.
    grouped = conn.execute(
        """
        SELECT
            base_name,
            adapter_name,
            (CAST(strftime('%w', ts) AS INTEGER) * 24
             + CAST(strftime('%H', ts) AS INTEGER)) AS hour_of_week,
            COUNT(*) AS n
        FROM usage_events
        WHERE ts < ?
        GROUP BY base_name, adapter_name, hour_of_week
        """,
        (before_iso,),
    ).fetchall()
    if not grouped:
        return {"buckets_upserted": 0, "events_deleted": 0}
    buckets = 0
    for row in grouped:
        upsert(
            conn,
            base_name=row["base_name"],
            adapter_name=row["adapter_name"],
            hour_of_week=int(row["hour_of_week"]),
            count_delta=int(row["n"]),
        )
        buckets += 1
    cur = conn.execute(
        "DELETE FROM usage_events WHERE ts < ?", (before_iso,),
    )
    return {
        "buckets_upserted": buckets,
        "events_deleted": cur.rowcount or 0,
    }
