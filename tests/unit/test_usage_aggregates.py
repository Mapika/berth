"""usage_aggregates store + rollup_old_events.

Schema: migration 008.
Semantics: bounded storage - events older than retention land in
(base, adapter, hour_of_week) buckets here and the raw rows go away.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from berth.store import db
from berth.store import usage_aggregates as ua_store


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def _insert_event(conn, *, ts: datetime, base: str, adapter: str | None = None):
    conn.execute(
        """
        INSERT INTO usage_events (ts, api_key_id, model_name, base_name,
                                  adapter_name, deployment_id)
        VALUES (?, NULL, ?, ?, ?, NULL)
        """,
        (
            ts.strftime("%Y-%m-%d %H:%M:%S"),
            adapter or base,
            base,
            adapter,
        ),
    )


def test_upsert_creates_then_increments(tmp_path):
    """First upsert inserts; second adds to count instead of duplicating."""
    conn = _fresh(tmp_path)
    ua_store.upsert(conn, base_name="b", adapter_name=None, hour_of_week=14, count_delta=3)
    ua_store.upsert(conn, base_name="b", adapter_name=None, hour_of_week=14, count_delta=2)
    bucket = ua_store.get_bucket(conn, base_name="b", adapter_name=None, hour_of_week=14)
    assert bucket is not None
    assert bucket.count == 5


def test_upsert_treats_distinct_adapters_separately(tmp_path):
    conn = _fresh(tmp_path)
    ua_store.upsert(conn, base_name="b", adapter_name=None, hour_of_week=14, count_delta=1)
    ua_store.upsert(conn, base_name="b", adapter_name="x", hour_of_week=14, count_delta=1)
    rows = ua_store.list_all(conn)
    assert len(rows) == 2
    assert {(r.adapter_name, r.count) for r in rows} == {(None, 1), ("x", 1)}


def test_upsert_ignores_zero_or_negative_delta(tmp_path):
    """No-op on 0/negative delta keeps the storefront from polluting the
    table with empty rows on edge-case caller bugs."""
    conn = _fresh(tmp_path)
    ua_store.upsert(conn, base_name="b", adapter_name=None, hour_of_week=14, count_delta=0)
    ua_store.upsert(conn, base_name="b", adapter_name=None, hour_of_week=14, count_delta=-3)
    assert ua_store.list_all(conn) == []


def test_rollup_old_events_aggregates_and_deletes(tmp_path):
    """Old events get bucketed by (base, adapter, hour-of-week) and the
    raw rows go away. Newer events survive."""
    conn = _fresh(tmp_path)
    # 3 old events at the same hour-of-week (Mon 14:xx, 60 days ago).
    old_base_ts = datetime(2026, 3, 9, 14, 0, tzinfo=UTC).replace(tzinfo=None)  # Mon
    for i in range(3):
        _insert_event(conn, ts=old_base_ts + timedelta(minutes=i), base="m")
    # Two recent events (still within retention).
    fresh_ts = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    _insert_event(conn, ts=fresh_ts, base="m")
    _insert_event(conn, ts=fresh_ts, base="m")

    cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)).strftime(
        "%Y-%m-%d %H:%M:%S",
    )
    result = ua_store.rollup_old_events(conn, before_iso=cutoff)

    assert result == {"buckets_upserted": 1, "events_deleted": 3}
    # The aggregate bucket has the 3 old events.
    bucket = ua_store.get_bucket(conn, base_name="m", adapter_name=None, hour_of_week=24 + 14)
    assert bucket is not None
    assert bucket.count == 3
    # Recent events are still raw.
    remaining = conn.execute("SELECT COUNT(*) AS n FROM usage_events").fetchone()
    assert remaining["n"] == 2


def test_rollup_old_events_is_noop_on_empty_window(tmp_path):
    """No events older than cutoff -> zero work, both counters zero."""
    conn = _fresh(tmp_path)
    fresh_ts = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    _insert_event(conn, ts=fresh_ts, base="m")
    cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)).strftime(
        "%Y-%m-%d %H:%M:%S",
    )
    result = ua_store.rollup_old_events(conn, before_iso=cutoff)
    assert result == {"buckets_upserted": 0, "events_deleted": 0}
    # The single recent row survives.
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM usage_events",
    ).fetchone()["n"] == 1


def test_rollup_repeated_run_increments_existing_buckets(tmp_path):
    """Two rollup passes against overlapping old data must merge counts,
    not create duplicate buckets."""
    conn = _fresh(tmp_path)
    old_ts = datetime(2026, 3, 9, 14, 0, tzinfo=UTC).replace(tzinfo=None)
    _insert_event(conn, ts=old_ts, base="m")
    cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)).strftime(
        "%Y-%m-%d %H:%M:%S",
    )
    ua_store.rollup_old_events(conn, before_iso=cutoff)
    # Insert another old row + roll up again.
    _insert_event(conn, ts=old_ts + timedelta(minutes=5), base="m")
    ua_store.rollup_old_events(conn, before_iso=cutoff)
    rows = ua_store.list_all(conn)
    assert len(rows) == 1
    assert rows[0].count == 2
