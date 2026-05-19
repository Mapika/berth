from berth.store import api_keys, db, key_usage


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def test_record_and_count_in_window(tmp_path):
    conn = _fresh(tmp_path)
    _, k = api_keys.create(conn, name="a", tier="standard")
    key_usage.record(conn, key_id=k.id, tokens_in=100, tokens_out=50, model_name="qwen-0_5b")
    key_usage.record(conn, key_id=k.id, tokens_in=10, tokens_out=20)
    requests, tokens = key_usage.totals_in_window(conn, key_id=k.id, window_s=60)
    assert requests == 2
    assert tokens == 100 + 50 + 10 + 20


def test_purge_older_than(tmp_path):
    conn = _fresh(tmp_path)
    _, k = api_keys.create(conn, name="a", tier="standard")
    key_usage.record(conn, key_id=k.id, tokens_in=1, tokens_out=1)
    conn.execute(
        "UPDATE key_usage_events SET ts=datetime('now', '-2 seconds')"
    )
    key_usage.record(conn, key_id=k.id, tokens_in=2, tokens_out=2)
    purged = key_usage.purge_older_than_s(conn, max_age_s=1)
    assert purged == 1
    requests, _ = key_usage.totals_in_window(conn, key_id=k.id, window_s=60)
    assert requests == 1
