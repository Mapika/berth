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


def series_in_window(
    conn: sqlite3.Connection,
    *,
    window_s: int,
    bucket_s: int,
    group_by: str | None = None,
) -> list[dict]:
    """Time-bucketed request volume + tokens over the past `window_s` seconds.

    Mirrors `key_usage.bucketed_usage`: buckets are `bucket_s` wide, zero-filled,
    returned oldest-first so the UI can plot left-to-right.

    - ``group_by=None``  -> a flat list of bucket dicts:
      ``[{bucket_idx, ts_offset_s, count, tokens_in, tokens_out}, ...]``.
    - ``group_by='model'|'key'`` -> one entry per group, sorted by total desc:
      ``[{key, label, total, tokens_in, tokens_out, buckets:[...]}, ...]`` where
      each group carries its own zero-filled bucket list.

    Bounded by retention: rows older than the daemon's retention window have
    already been rolled up into usage_aggregates and dropped, so the effective
    history can be shorter than `window_s`.
    """
    bucket_s = max(1, int(bucket_s))
    num_buckets = max(1, int(window_s) // bucket_s)
    col = {None: None, "model": "model_name", "key": "api_key_id"}[group_by]

    def _empty_buckets() -> list[dict]:
        # bucket_idx 0 = most recent; emit oldest -> newest.
        return [
            {
                "bucket_idx": i,
                "ts_offset_s": i * bucket_s,
                "count": 0,
                "tokens_in": 0,
                "tokens_out": 0,
            }
            for i in range(num_buckets - 1, -1, -1)
        ]

    def _fill(buckets: list[dict], by_idx: dict) -> None:
        for b in buckets:
            r = by_idx.get(b["bucket_idx"])
            if r is not None:
                b["count"] = int(r["count"])
                b["tokens_in"] = int(r["tokens_in"])
                b["tokens_out"] = int(r["tokens_out"])

    select_grp = f"{col} AS grp," if col else ""
    group_grp = "grp, " if col else ""
    rows = conn.execute(
        f"""
        SELECT
            {select_grp}
            CAST(
                (CAST(strftime('%s', 'now') AS INTEGER)
                 - CAST(strftime('%s', ts) AS INTEGER)) / ?
                AS INTEGER
            ) AS bucket_idx,
            COUNT(*) AS count,
            COALESCE(SUM(tokens_in), 0) AS tokens_in,
            COALESCE(SUM(tokens_out), 0) AS tokens_out
        FROM usage_events
        WHERE ts > datetime('now', ?)
        GROUP BY {group_grp}bucket_idx
        """,
        (bucket_s, f"-{window_s} seconds"),
    ).fetchall()

    if col is None:
        out = _empty_buckets()
        _fill(out, {int(r["bucket_idx"]): r for r in rows})
        return out

    groups: dict[str, dict] = {}
    for r in rows:
        raw = r["grp"]
        gkey = "" if raw is None else str(raw)
        g = groups.setdefault(gkey, {
            "key": gkey,
            "label": "—" if raw is None else str(raw),
            "total": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "_by_idx": {},
        })
        g["total"] += int(r["count"])
        g["tokens_in"] += int(r["tokens_in"])
        g["tokens_out"] += int(r["tokens_out"])
        g["_by_idx"][int(r["bucket_idx"])] = r

    result: list[dict] = []
    for g in sorted(groups.values(), key=lambda x: x["total"], reverse=True):
        buckets = _empty_buckets()
        _fill(buckets, g["_by_idx"])
        result.append({
            "key": g["key"],
            "label": g["label"],
            "total": g["total"],
            "tokens_in": g["tokens_in"],
            "tokens_out": g["tokens_out"],
            "buckets": buckets,
        })
    return result


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
