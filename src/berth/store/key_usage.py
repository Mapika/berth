from __future__ import annotations

import sqlite3


def record(
    conn: sqlite3.Connection,
    *,
    key_id: int,
    tokens_in: int,
    tokens_out: int,
    model_name: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO key_usage_events (key_id, tokens_in, tokens_out, model_name)
        VALUES (?, ?, ?, ?)
        """,
        (key_id, tokens_in, tokens_out, model_name),
    )
    return int(cur.lastrowid or 0)


def set_tokens(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    tokens_in: int,
    tokens_out: int,
) -> None:
    conn.execute(
        """
        UPDATE key_usage_events
        SET tokens_in=?, tokens_out=?
        WHERE id=?
        """,
        (tokens_in, tokens_out, event_id),
    )


def totals_in_window(
    conn: sqlite3.Connection,
    *,
    key_id: int,
    window_s: int,
) -> tuple[int, int]:
    """Returns (request_count, total_tokens) for events with ts > now - window_s."""
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS n,
            COALESCE(SUM(tokens_in + tokens_out), 0) AS tok
        FROM key_usage_events
        WHERE key_id = ?
          AND ts > datetime('now', ?)
        """,
        (key_id, f"-{window_s} seconds"),
    ).fetchone()
    return int(row["n"]), int(row["tok"])


def bucketed_usage(
    conn: sqlite3.Connection,
    *,
    key_id: int,
    window_s: int,
    bucket_s: int,
) -> list[dict]:
    """Return time-bucketed usage over the past `window_s` seconds in
    `bucket_s`-wide buckets. Buckets with no events are zero-filled. Returned
    oldest first so the UI can plot left-to-right without re-sorting.
    """
    bucket_s = max(1, int(bucket_s))
    num_buckets = max(1, int(window_s) // bucket_s)
    rows = conn.execute(
        """
        SELECT
            CAST(
                (CAST(strftime('%s', 'now') AS INTEGER)
                 - CAST(strftime('%s', ts) AS INTEGER)) / ?
                AS INTEGER
            ) AS bucket_idx,
            COUNT(*) AS requests,
            COALESCE(SUM(tokens_in), 0) AS tokens_in,
            COALESCE(SUM(tokens_out), 0) AS tokens_out
        FROM key_usage_events
        WHERE key_id = ?
          AND ts > datetime('now', ?)
        GROUP BY bucket_idx
        """,
        (bucket_s, key_id, f"-{window_s} seconds"),
    ).fetchall()
    by_idx = {int(r["bucket_idx"]): r for r in rows}
    out: list[dict] = []
    # bucket_idx 0 = most recent bucket; iterate oldest -> newest
    for i in range(num_buckets - 1, -1, -1):
        r = by_idx.get(i)
        out.append({
            "bucket_idx": i,
            "requests": int(r["requests"]) if r else 0,
            "tokens_in": int(r["tokens_in"]) if r else 0,
            "tokens_out": int(r["tokens_out"]) if r else 0,
        })
    return out


def purge_older_than_s(conn: sqlite3.Connection, *, max_age_s: float) -> int:
    """Delete usage events older than `max_age_s` seconds. Returns rows deleted."""
    cur = conn.execute(
        "DELETE FROM key_usage_events WHERE ts < datetime('now', ?)",
        (f"-{max_age_s} seconds",),
    )
    return cur.rowcount
