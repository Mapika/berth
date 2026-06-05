"""Roll aged request_metrics raw rows into request_metrics_hourly with exact
per-bucket percentiles, then drop the raw rows. Mirrors usage_aggregates.

Each closed hour is rolled in a single pass (the task runs after the raw row
ages past RAW_RETENTION_H), so the stored percentiles are exact for that hour
and never recombined across buckets.
"""
from __future__ import annotations

import sqlite3

from berth.store.request_metrics import _percentile


def rollup_aged_raw(conn: sqlite3.Connection, *, older_than_h: int) -> dict:
    rows = conn.execute(
        """
        SELECT
            strftime('%Y-%m-%d %H:00:00', ts) AS bucket_start,
            model_name, is_error, dispatched, latency_ms, ttft_ms
        FROM request_metrics
        WHERE ts < datetime('now', ?)
        """,
        (f"-{older_than_h} hours",),
    ).fetchall()
    if not rows:
        return {"buckets_upserted": 0, "raw_deleted": 0}

    # Group by (bucket, model) and (bucket, all-models=None).
    groups: dict[tuple, list] = {}
    for r in rows:
        b = r["bucket_start"]
        groups.setdefault((b, r["model_name"]), []).append(r)
        groups.setdefault((b, None), []).append(r)

    conn.execute("BEGIN IMMEDIATE")
    try:
        upserts = 0
        for (bucket, model), grp in groups.items():
            lat = sorted(x["latency_ms"] for x in grp if x["latency_ms"] is not None)
            ttft = sorted(x["ttft_ms"] for x in grp if x["ttft_ms"] is not None)
            conn.execute(
                """
                INSERT INTO request_metrics_hourly (
                    bucket_start, model_name, count, error_count, dispatched_count,
                    latency_p50_ms, latency_p95_ms, ttft_p50_ms, ttft_p95_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bucket_start, COALESCE(model_name, '')) DO UPDATE SET
                    count = count + excluded.count,
                    error_count = error_count + excluded.error_count,
                    dispatched_count = dispatched_count + excluded.dispatched_count,
                    latency_p50_ms = excluded.latency_p50_ms,
                    latency_p95_ms = excluded.latency_p95_ms,
                    ttft_p50_ms = excluded.ttft_p50_ms,
                    ttft_p95_ms = excluded.ttft_p95_ms
                """,
                (bucket, model, len(grp),
                 sum(1 for x in grp if x["is_error"]),
                 sum(1 for x in grp if x["dispatched"]),
                 _percentile(lat, 0.50), _percentile(lat, 0.95),
                 _percentile(ttft, 0.50), _percentile(ttft, 0.95)),
            )
            upserts += 1
        cur = conn.execute(
            "DELETE FROM request_metrics WHERE ts < datetime('now', ?)",
            (f"-{older_than_h} hours",),
        )
        deleted = cur.rowcount or 0
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"buckets_upserted": upserts, "raw_deleted": deleted}


def purge_hourly_older_than(conn: sqlite3.Connection, *, days: int) -> int:
    cur = conn.execute(
        "DELETE FROM request_metrics_hourly WHERE bucket_start < datetime('now', ?)",
        (f"-{days} days",),
    )
    return cur.rowcount or 0
