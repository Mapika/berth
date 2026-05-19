"""Usage-event log feeding the predictor.

One row per request, written by the OpenAI proxy at dispatch time.
Schema is intentionally minimal - the predictor reads from indexed
columns only. Larger reasoning artifacts (per-token logs, full
prompts) are out of scope; if we ever want them they go to a
separate, opt-in `request_logs` table.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class UsageEvent:
    id: int
    ts: str
    api_key_id: int | None
    model_name: str
    base_name: str
    adapter_name: str | None
    deployment_id: int | None
    tokens_in: int
    tokens_out: int
    cold_loaded: bool
    source_peer_id: str | None


def _row(row: sqlite3.Row) -> UsageEvent:
    return UsageEvent(
        id=row["id"],
        ts=row["ts"],
        api_key_id=row["api_key_id"],
        model_name=row["model_name"],
        base_name=row["base_name"],
        adapter_name=row["adapter_name"],
        deployment_id=row["deployment_id"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        cold_loaded=bool(row["cold_loaded"]),
        source_peer_id=row["source_peer_id"],
    )


def record(
    conn: sqlite3.Connection,
    *,
    model_name: str,
    base_name: str,
    adapter_name: str | None = None,
    deployment_id: int | None = None,
    api_key_id: int | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cold_loaded: bool = False,
) -> int:
    """Insert one usage event. Hot path - called from the proxy on every
    request. Keep this fast; no SELECTs, no JOINs. Returns the new row id
    so the caller can patch in token counts after the upstream stream
    completes (the proxy doesn't know them at dispatch time).
    """
    cur = conn.execute(
        """
        INSERT INTO usage_events (
            api_key_id, model_name, base_name, adapter_name,
            deployment_id, tokens_in, tokens_out, cold_loaded
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            api_key_id, model_name, base_name, adapter_name,
            deployment_id, tokens_in, tokens_out, 1 if cold_loaded else 0,
        ),
    )
    return int(cur.lastrowid or 0)


def set_tokens(
    conn: sqlite3.Connection, event_id: int, *, tokens_in: int, tokens_out: int,
) -> None:
    """Patch in upstream-reported token counts after the stream completes.
    Called from the proxy's streamer finally-block, paired with the id
    returned by `record()`. Idempotent - silently no-ops for unknown ids."""
    conn.execute(
        "UPDATE usage_events SET tokens_in=?, tokens_out=? WHERE id=?",
        (tokens_in, tokens_out, event_id),
    )


def count_in_window(
    conn: sqlite3.Connection, *, since_iso: str, base_name: str | None = None,
) -> int:
    """Total events since `since_iso` (sqlite TIMESTAMP literal), optionally
    filtered to a base."""
    if base_name is None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM usage_events WHERE ts >= ?",
            (since_iso,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM usage_events "
            "WHERE base_name=? AND ts >= ?",
            (base_name, since_iso),
        ).fetchone()
    return int(row["n"]) if row and row["n"] is not None else 0


def cold_load_rate_in_window(
    conn: sqlite3.Connection, *, since_iso: str,
) -> float:
    """sum(cold_loaded) / count(*) over the window. The metric the
    predictor optimizes - v1 LRU baseline -> v2 with predictor on.
    Returns 0.0 if the window is empty."""
    row = conn.execute(
        """
        SELECT
            CAST(SUM(cold_loaded) AS REAL) AS cold,
            CAST(COUNT(*) AS REAL) AS total
        FROM usage_events WHERE ts >= ?
        """,
        (since_iso,),
    ).fetchone()
    total = (row["total"] or 0.0) if row else 0.0
    if total == 0:
        return 0.0
    return float(row["cold"] or 0.0) / total


def list_recent(
    conn: sqlite3.Connection, *, limit: int = 100,
) -> list[UsageEvent]:
    rows = conn.execute(
        "SELECT * FROM usage_events ORDER BY ts DESC LIMIT ?", (limit,),
    ).fetchall()
    return [_row(r) for r in rows]


def purge_older_than(
    conn: sqlite3.Connection, *, before_iso: str,
) -> int:
    """Drop rows older than `before_iso`. Returns rows deleted (for the
    background-GC log line). Operator policy in predictor.yaml controls
    the retention window."""
    cur = conn.execute(
        "DELETE FROM usage_events WHERE ts < ?", (before_iso,),
    )
    return cur.rowcount or 0
