"""Per-request outcome/latency store powering the Overview latency/error views.

Reads serve exact percentiles from raw rows for short windows and from
request_metrics_hourly for long windows (see source selection in `history`/
`summary`). SQL is literal — no identifier interpolation (Bandit B608);
grouping selects a row-dict key in Python, never an SQL column name.
"""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

# Raw rows are kept this long for exact sub-hour percentiles + drill-down;
# older data lives only in the hourly rollup.
RAW_RETENTION_H = 48
_RAW_WINDOW_S = RAW_RETENTION_H * 3600

_GROUP_COL = {"model": "model_name", "route": "route_name"}


def _use_rollup(window_s: int, group_by: str | None) -> bool:
    # Long windows read the hourly rollup. The rollup only stores per-model
    # (and all-models) rows, so route grouping always uses raw (≤ retention).
    return window_s > _RAW_WINDOW_S and group_by in (None, "model")


def record(
    conn: sqlite3.Connection, *,
    model_name: str | None = None,
    route_name: str | None = None,
    deployment_id: int | None = None,
    backend: str | None = None,
    status_code: int | None = None,
    is_error: bool = False,
    dispatched: bool = False,
    latency_ms: int | None = None,
    ttft_ms: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO request_metrics (
            model_name, route_name, deployment_id, backend, status_code,
            is_error, dispatched, latency_ms, ttft_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (model_name, route_name, deployment_id, backend, status_code,
         1 if is_error else 0, 1 if dispatched else 0, latency_ms, ttft_ms),
    )
    return int(cur.lastrowid or 0)


def record_from_trace(conn: sqlite3.Connection, trace) -> None:
    """Best-effort sink for RequestTracer.finalize. Never raises into the
    request path."""
    try:
        dispatched = trace.dispatched_at is not None
        latency_ms = (
            round((trace.completed_at - trace.dispatched_at) * 1000)
            if dispatched and trace.completed_at is not None else None
        )
        ttft_ms = (
            round((trace.first_byte_at - trace.dispatched_at) * 1000)
            if dispatched and trace.first_byte_at is not None else None
        )
        sc = trace.status_code
        is_error = bool(trace.error) or sc is None or sc >= 400
        record(
            conn,
            model_name=trace.target_model or trace.model_requested,
            route_name=trace.route_name,
            deployment_id=trace.deployment_id,
            backend=trace.backend,
            status_code=sc,
            is_error=is_error,
            dispatched=dispatched,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
        )
    except Exception:  # sink must never break serving
        log.exception("request_metrics sink failed")


def _percentile(sorted_vals: list[int], q: float) -> int | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return round(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def _summarize(rows: list[sqlite3.Row]) -> dict:
    count = len(rows)
    error_count = sum(1 for r in rows if r["is_error"])
    lat = sorted(r["latency_ms"] for r in rows if r["latency_ms"] is not None)
    ttft = sorted(r["ttft_ms"] for r in rows if r["ttft_ms"] is not None)
    return {
        "count": count,
        "error_count": error_count,
        "dispatched_count": sum(1 for r in rows if r["dispatched"]),
        "error_rate": (error_count / count) if count else 0.0,
        "latency_p50_ms": _percentile(lat, 0.50),
        "latency_p95_ms": _percentile(lat, 0.95),
        "ttft_p50_ms": _percentile(ttft, 0.50),
        "ttft_p95_ms": _percentile(ttft, 0.95),
    }


def _raw_rows_in_window(conn: sqlite3.Connection, window_s: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT model_name, route_name, is_error, dispatched, latency_ms, ttft_ms,
               CAST((CAST(strftime('%s','now') AS INTEGER)
                     - CAST(strftime('%s', ts) AS INTEGER)) AS INTEGER) AS age_s
        FROM request_metrics
        WHERE ts > datetime('now', ?)
        """,
        (f"-{window_s} seconds",),
    ).fetchall()


def _group_key(row: sqlite3.Row, col: str) -> str:
    return row[col] or "—"


def _hourly_rows_in_window(conn: sqlite3.Connection, window_s: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT bucket_start, model_name, count, error_count, dispatched_count,
               latency_p50_ms, latency_p95_ms, ttft_p50_ms, ttft_p95_ms,
               CAST((CAST(strftime('%s','now') AS INTEGER)
                     - CAST(strftime('%s', bucket_start) AS INTEGER)) AS INTEGER) AS age_s
        FROM request_metrics_hourly
        WHERE bucket_start > datetime('now', ?)
        """,
        (f"-{window_s} seconds",),
    ).fetchall()


def _weighted(rows: list[sqlite3.Row], col: str) -> int | None:
    # count-weighted mean of a per-bucket percentile column (an approximation:
    # percentiles can't be recombined exactly across buckets).
    num = sum((r[col] or 0) * r["count"] for r in rows if r[col] is not None)
    den = sum(r["count"] for r in rows if r[col] is not None)
    return round(num / den) if den else None


def _summarize_hourly(rows: list[sqlite3.Row]) -> dict:
    count = sum(r["count"] for r in rows)
    error_count = sum(r["error_count"] for r in rows)
    return {
        "count": count,
        "error_count": error_count,
        "dispatched_count": sum(r["dispatched_count"] for r in rows),
        "error_rate": (error_count / count) if count else 0.0,
        "latency_p50_ms": _weighted(rows, "latency_p50_ms"),
        "latency_p95_ms": _weighted(rows, "latency_p95_ms"),
        "ttft_p50_ms": _weighted(rows, "ttft_p50_ms"),
        "ttft_p95_ms": _weighted(rows, "ttft_p95_ms"),
    }


def summary(
    conn: sqlite3.Connection, *, window_s: int, group_by: str | None = None,
) -> object:
    if _use_rollup(window_s, group_by):
        hourly = _hourly_rows_in_window(conn, window_s)
        if group_by is None:
            return _summarize_hourly([r for r in hourly if r["model_name"] is None])
        hkeyed: dict[str, list] = {}
        for r in hourly:
            if r["model_name"] is None:
                continue
            hkeyed.setdefault(r["model_name"], []).append(r)
        items = [(sum(x["count"] for x in v),
                  {"key": k, "label": k, "summary": _summarize_hourly(v)})
                 for k, v in hkeyed.items()]
        items.sort(key=lambda t: t[0], reverse=True)
        return [g for _, g in items]

    rows = _raw_rows_in_window(conn, window_s)
    if group_by is None:
        return _summarize(rows)
    col = _GROUP_COL[group_by]
    keyed: dict[str, list] = {}
    for r in rows:
        keyed.setdefault(_group_key(r, col), []).append(r)
    # sort by group size (descending); tuple key keeps mypy happy vs indexing
    # into the object-typed summary dict.
    items = [(len(v), {"key": k, "label": k, "summary": _summarize(v)})
             for k, v in keyed.items()]
    items.sort(key=lambda t: t[0], reverse=True)
    return [g for _, g in items]


def _hourly_history(
    conn: sqlite3.Connection, window_s: int, group_by: str | None,
) -> list[dict]:
    # Long-window history from the rollup: hourly granularity (percentiles are
    # already exact per hour and must not be recombined).
    bucket_s = 3600
    num_buckets = max(1, int(window_s) // bucket_s)
    hourly = _hourly_rows_in_window(conn, window_s)

    def empty() -> list[dict]:
        return [
            {"bucket_idx": i, "ts_offset_s": i * bucket_s,
             "count": 0, "error_count": 0, "error_rate": 0.0,
             "latency_p50_ms": None, "latency_p95_ms": None}
            for i in range(num_buckets - 1, -1, -1)
        ]

    def fill(buckets: list[dict], rowset: list[sqlite3.Row]) -> list[dict]:
        by_idx = {min(int(r["age_s"]) // bucket_s, num_buckets - 1): r for r in rowset}
        for b in buckets:
            r = by_idx.get(b["bucket_idx"])
            if r:
                b["count"] = r["count"]
                b["error_count"] = r["error_count"]
                b["error_rate"] = (r["error_count"] / r["count"]) if r["count"] else 0.0
                b["latency_p50_ms"] = r["latency_p50_ms"]
                b["latency_p95_ms"] = r["latency_p95_ms"]
        return buckets

    if group_by is None:
        return fill(empty(), [r for r in hourly if r["model_name"] is None])

    keyed: dict[str, list] = {}
    for r in hourly:
        if r["model_name"] is None:
            continue
        keyed.setdefault(r["model_name"], []).append(r)
    items = [
        (sum(x["count"] for x in v),
         {"key": k, "label": k, "summary": _summarize_hourly(v), "buckets": fill(empty(), v)})
        for k, v in keyed.items()
    ]
    items.sort(key=lambda t: t[0], reverse=True)
    return [g for _, g in items]


def history(
    conn: sqlite3.Connection, *, window_s: int, bucket_s: int,
    group_by: str | None = None,
) -> list[dict]:
    if _use_rollup(window_s, group_by):
        return _hourly_history(conn, window_s, group_by)
    bucket_s = max(1, int(bucket_s))
    num_buckets = max(1, int(window_s) // bucket_s)
    rows = _raw_rows_in_window(conn, window_s)

    def empty() -> list[dict]:
        return [
            {"bucket_idx": i, "ts_offset_s": i * bucket_s,
             "count": 0, "error_count": 0, "error_rate": 0.0,
             "latency_p50_ms": None, "latency_p95_ms": None}
            for i in range(num_buckets - 1, -1, -1)
        ]

    def fill(buckets: list[dict], rowset: list[sqlite3.Row]) -> list[dict]:
        by_idx: dict[int, list] = {}
        for r in rowset:
            idx = min(int(r["age_s"]) // bucket_s, num_buckets - 1)
            by_idx.setdefault(idx, []).append(r)
        for b in buckets:
            grp = by_idx.get(b["bucket_idx"])
            if grp:
                s = _summarize(grp)
                b["count"] = s["count"]
                b["error_count"] = s["error_count"]
                b["error_rate"] = s["error_rate"]
                b["latency_p50_ms"] = s["latency_p50_ms"]
                b["latency_p95_ms"] = s["latency_p95_ms"]
        return buckets

    if group_by is None:
        return fill(empty(), rows)

    col = _GROUP_COL[group_by]
    keyed: dict[str, list] = {}
    for r in rows:
        keyed.setdefault(_group_key(r, col), []).append(r)
    items = [
        (len(v), {"key": k, "label": k,
                  "summary": _summarize(v), "buckets": fill(empty(), v)})
        for k, v in keyed.items()
    ]
    items.sort(key=lambda t: t[0], reverse=True)
    return [g for _, g in items]
