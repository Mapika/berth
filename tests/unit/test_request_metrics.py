"""Tests for the request_metrics store (raw per-request latency/error data
powering the Overview dashboard's historical latency/error views)."""
from berth.store import db
from berth.store import request_metrics as rm


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def test_migration_creates_tables_and_indexes(tmp_path):
    conn = _fresh(tmp_path)
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "request_metrics" in tables
    assert "request_metrics_hourly" in tables
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_rm_ts" in idx
    assert "idx_rm_model_ts" in idx


def test_record_and_summary_counts_and_percentiles(tmp_path):
    conn = _fresh(tmp_path)
    for ms in (100, 200, 300, 400):  # dispatched OK
        rm.record(conn, model_name="m", status_code=200, is_error=False,
                  dispatched=True, latency_ms=ms, ttft_ms=ms // 2)
    rm.record(conn, model_name="m", status_code=503, is_error=True,
              dispatched=False, latency_ms=None, ttft_ms=None)  # pre-dispatch err
    s = rm.summary(conn, window_s=3600)
    assert s["count"] == 5
    assert s["error_count"] == 1
    assert abs(s["error_rate"] - 0.2) < 1e-9
    assert s["latency_p50_ms"] == 250   # median of 100,200,300,400 -> avg(200,300)
    assert s["latency_p95_ms"] == 385   # linear interpolation: 0.95*(n-1)=2.85 -> 300..400


def test_history_buckets_zero_filled_oldest_first(tmp_path):
    conn = _fresh(tmp_path)
    rm.record(conn, model_name="m", status_code=200, is_error=False,
              dispatched=True, latency_ms=120, ttft_ms=40)
    rows = rm.history(conn, window_s=3600, bucket_s=900)
    assert len(rows) == 4
    assert rows[0]["count"] == 0 and rows[-1]["count"] == 1
    assert rows[-1]["latency_p95_ms"] == 120
    assert rows[0]["ts_offset_s"] > rows[-1]["ts_offset_s"]


def test_history_group_by_model(tmp_path):
    conn = _fresh(tmp_path)
    rm.record(conn, model_name="a", status_code=200, is_error=False,
              dispatched=True, latency_ms=100, ttft_ms=10)
    rm.record(conn, model_name="b", status_code=500, is_error=True,
              dispatched=True, latency_ms=900, ttft_ms=90)
    groups = rm.history(conn, window_s=3600, bucket_s=3600, group_by="model")
    by = {g["label"]: g for g in groups}
    assert by["b"]["summary"]["error_rate"] == 1.0
    assert by["a"]["summary"]["latency_p95_ms"] == 100


def test_summary_empty_window(tmp_path):
    conn = _fresh(tmp_path)
    s = rm.summary(conn, window_s=3600)
    assert s["count"] == 0
    assert s["error_rate"] == 0.0
    assert s["latency_p50_ms"] is None
