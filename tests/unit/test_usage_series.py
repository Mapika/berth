"""Store tests for the bucketed usage-series query that powers the Overview
dashboard's volume/throughput tiles. The /admin/usage/series route handler is
covered HTTP-level in test_admin_endpoints.py (reusing its app fixture, which
imports the daemon modules in the right order — admin_runtime imported on its
own hits a pre-existing admin<->admin_runtime circular import).
"""
from berth.store import db
from berth.store import usage_events as ue


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


# --- store: series_in_window -------------------------------------------------

def test_series_ungrouped_counts_and_tokens(tmp_path):
    conn = _fresh(tmp_path)
    for _ in range(3):
        ue.record(conn, model_name="m", base_name="m", tokens_in=2, tokens_out=10)
    rows = ue.series_in_window(conn, window_s=3600, bucket_s=3600, group_by=None)
    assert len(rows) == 1  # window_s // bucket_s == 1 bucket
    assert rows[-1]["count"] == 3
    assert rows[-1]["tokens_in"] == 6
    assert rows[-1]["tokens_out"] == 30


def test_series_zero_fills_empty_buckets_oldest_first(tmp_path):
    conn = _fresh(tmp_path)
    ue.record(conn, model_name="m", base_name="m", tokens_out=1)
    rows = ue.series_in_window(conn, window_s=3600, bucket_s=600, group_by=None)
    assert len(rows) == 6  # 3600 / 600
    # oldest first; all but the most-recent bucket are empty
    assert rows[0]["count"] == 0
    assert rows[-1]["count"] == 1
    # ts_offset decreases toward the present
    assert rows[0]["ts_offset_s"] > rows[-1]["ts_offset_s"]


def test_series_group_by_model_sorted_by_total(tmp_path):
    conn = _fresh(tmp_path)
    for _ in range(3):
        ue.record(conn, model_name="busy", base_name="busy", tokens_out=5)
    ue.record(conn, model_name="quiet", base_name="quiet", tokens_out=7)
    groups = ue.series_in_window(conn, window_s=3600, bucket_s=3600, group_by="model")
    assert [g["label"] for g in groups] == ["busy", "quiet"]  # busy first (higher total)
    by_label = {g["label"]: g for g in groups}
    assert by_label["busy"]["total"] == 3
    assert by_label["quiet"]["tokens_out"] == 7
    # each group still carries a zero-filled bucket list
    assert by_label["busy"]["buckets"][-1]["count"] == 3


def test_series_group_by_key(tmp_path):
    conn = _fresh(tmp_path)
    ue.record(conn, model_name="m", base_name="m", api_key_id=None, tokens_out=1)
    groups = ue.series_in_window(conn, window_s=3600, bucket_s=3600, group_by="key")
    # NULL api_key id collapses to the "—" label
    assert groups[0]["label"] == "—"
    assert groups[0]["total"] == 1


def test_series_empty_window_returns_zeroed_buckets(tmp_path):
    conn = _fresh(tmp_path)
    rows = ue.series_in_window(conn, window_s=3600, bucket_s=1800, group_by=None)
    assert len(rows) == 2
    assert all(r["count"] == 0 for r in rows)
