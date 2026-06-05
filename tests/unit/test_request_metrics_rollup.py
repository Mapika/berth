"""Hourly rollup of aged request_metrics raw rows, and long-window history
served from the rollup."""
import pytest

from berth.store import db
from berth.store import request_metrics as rm
from berth.store import request_metrics_rollup as rmr


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def test_rollup_aggregates_old_raw_and_purges(tmp_path):
    conn = _fresh(tmp_path)
    for ms in (100, 300):  # two rows for model 'a', same hour bucket (3h ago)
        conn.execute(
            "INSERT INTO request_metrics (ts, model_name, is_error, dispatched, latency_ms) "
            "VALUES (datetime('now','-3 hours'), 'a', 0, 1, ?)", (ms,))
    res = rmr.rollup_aged_raw(conn, older_than_h=1)
    assert res["raw_deleted"] == 2
    row = conn.execute(
        "SELECT * FROM request_metrics_hourly WHERE model_name='a'").fetchone()
    assert row["count"] == 2
    assert row["latency_p50_ms"] == 200   # interp median of (100,300)
    allrow = conn.execute(
        "SELECT * FROM request_metrics_hourly WHERE model_name IS NULL").fetchone()
    assert allrow["count"] == 2
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM request_metrics").fetchone()["n"] == 0


def test_rollup_noop_when_no_aged_rows(tmp_path):
    conn = _fresh(tmp_path)
    rm.record(conn, model_name="a", dispatched=True, latency_ms=100)  # recent
    res = rmr.rollup_aged_raw(conn, older_than_h=1)
    assert res == {"buckets_upserted": 0, "raw_deleted": 0}


def test_purge_hourly_older_than(tmp_path):
    conn = _fresh(tmp_path)
    conn.execute(
        "INSERT INTO request_metrics_hourly (bucket_start, model_name, count, "
        "error_count, dispatched_count) VALUES (datetime('now','-40 days'), NULL, 1, 0, 1)")
    deleted = rmr.purge_hourly_older_than(conn, days=30)
    assert deleted == 1


def test_history_long_window_reads_rollup(tmp_path):
    conn = _fresh(tmp_path)
    # only hourly rollup rows exist (10 days ago), no raw rows
    conn.execute(
        "INSERT INTO request_metrics_hourly (bucket_start, model_name, count, "
        "error_count, dispatched_count, latency_p50_ms, latency_p95_ms) "
        "VALUES (datetime('now','-10 days'), NULL, 50, 5, 50, 120, 800)")
    rows = rm.history(conn, window_s=30 * 86400, bucket_s=3600)
    hit = [b for b in rows if b["count"] > 0]
    assert hit and hit[0]["count"] == 50
    assert hit[0]["latency_p95_ms"] == 800
    assert abs(hit[0]["error_rate"] - 0.1) < 1e-9


@pytest.mark.asyncio
async def test_metrics_rollup_task_tick(tmp_path):
    from berth.lifecycle.metrics_rollup_task import MetricsRollupTask
    conn = _fresh(tmp_path)
    conn.execute(
        "INSERT INTO request_metrics (ts, model_name, is_error, dispatched, latency_ms) "
        "VALUES (datetime('now','-60 hours'), 'a', 0, 1, 100)")  # aged past 48h
    task = MetricsRollupTask(conn=conn)
    res = await task.tick_once()
    assert res["raw_deleted"] == 1
    assert "hourly_purged" in res
    n = conn.execute("SELECT COUNT(*) AS n FROM request_metrics_hourly").fetchone()["n"]
    assert n >= 1  # all-models + per-model rows
