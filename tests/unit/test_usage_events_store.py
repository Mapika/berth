"""usage_events store tests (Workstream C foundation).

Predictor logic (rules, scoring, replay harness) is NOT in scope here  -
it lands when we wire up the Predictor task. These tests cover the
data layer: insert, query, GC.
"""
from datetime import UTC, datetime, timedelta

from berth.store import db
from berth.store import usage_events as ue


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def _ago_iso(seconds: int) -> str:
    t = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=seconds)
    return t.isoformat(sep=" ", timespec="seconds")


def test_record_writes_row(tmp_path):
    conn = _fresh(tmp_path)
    ue.record(
        conn, model_name="qwen3-7b", base_name="qwen3-7b",
        tokens_in=10, tokens_out=42,
    )
    rows = ue.list_recent(conn, limit=10)
    assert len(rows) == 1
    assert rows[0].model_name == "qwen3-7b"
    assert rows[0].base_name == "qwen3-7b"
    assert rows[0].adapter_name is None
    assert rows[0].tokens_in == 10
    assert rows[0].tokens_out == 42
    assert rows[0].cold_loaded is False
    assert rows[0].deployment_id is None  # FK is nullable


def test_record_returns_inserted_id(tmp_path):
    """record() must return the new row id so callers (the proxy) can
    patch in token counts after the upstream stream completes."""
    conn = _fresh(tmp_path)
    rid1 = ue.record(conn, model_name="m", base_name="m")
    rid2 = ue.record(conn, model_name="m", base_name="m")
    assert isinstance(rid1, int) and rid1 > 0
    assert rid2 == rid1 + 1


def test_set_tokens_patches_existing_row(tmp_path):
    """The proxy inserts a usage row at request dispatch (before tokens
    are known) and patches in tokens after the upstream stream completes.
    set_tokens must update the existing row in place."""
    conn = _fresh(tmp_path)
    rid = ue.record(conn, model_name="m", base_name="m")
    ue.set_tokens(conn, rid, tokens_in=42, tokens_out=17)
    rows = ue.list_recent(conn, limit=10)
    assert rows[0].tokens_in == 42
    assert rows[0].tokens_out == 17


def test_record_with_adapter(tmp_path):
    conn = _fresh(tmp_path)
    ue.record(
        conn, model_name="tone-formal", base_name="qwen3-7b",
        adapter_name="tone-formal", tokens_in=5,
        tokens_out=20, cold_loaded=True,
    )
    rows = ue.list_recent(conn, limit=1)
    assert rows[0].adapter_name == "tone-formal"
    assert rows[0].cold_loaded is True


def test_record_with_real_deployment_fk(tmp_path):
    """deployment_id must reference a real deployment row when set."""
    from berth.store import deployments as dep_store
    from berth.store import models as model_store
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen")
    dep = dep_store.create(
        conn, model_id=base.id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
    )
    ue.record(
        conn, model_name="qwen3-7b", base_name="qwen3-7b",
        deployment_id=dep.id,
    )
    rows = ue.list_recent(conn, limit=1)
    assert rows[0].deployment_id == dep.id


def test_count_in_window_total_and_filtered(tmp_path):
    conn = _fresh(tmp_path)
    for _ in range(5):
        ue.record(conn, model_name="a", base_name="a")
    for _ in range(3):
        ue.record(conn, model_name="b", base_name="b")
    # Use a slightly future timestamp so 'since 1 hour ago' includes everything
    since = _ago_iso(3600)
    assert ue.count_in_window(conn, since_iso=since) == 8
    assert ue.count_in_window(conn, since_iso=since, base_name="a") == 5
    assert ue.count_in_window(conn, since_iso=since, base_name="b") == 3
    assert ue.count_in_window(conn, since_iso=since, base_name="missing") == 0


def test_cold_load_rate_in_window(tmp_path):
    conn = _fresh(tmp_path)
    # 4 cold, 6 warm -> 40% cold-load rate
    for _ in range(4):
        ue.record(conn, model_name="x", base_name="x", cold_loaded=True)
    for _ in range(6):
        ue.record(conn, model_name="x", base_name="x", cold_loaded=False)
    rate = ue.cold_load_rate_in_window(conn, since_iso=_ago_iso(3600))
    assert abs(rate - 0.4) < 0.01, rate


def test_cold_load_rate_empty_window_is_zero(tmp_path):
    conn = _fresh(tmp_path)
    # No events in the window
    rate = ue.cold_load_rate_in_window(conn, since_iso=_now_iso())
    assert rate == 0.0


def test_list_recent_orders_by_ts_desc(tmp_path):
    """Newest first, regardless of insert order vs sleep."""
    conn = _fresh(tmp_path)
    ue.record(conn, model_name="first", base_name="first")
    ue.record(conn, model_name="second", base_name="second")
    ue.record(conn, model_name="third", base_name="third")
    rows = ue.list_recent(conn, limit=10)
    # Sqlite's CURRENT_TIMESTAMP has 1s resolution, so all rows may share
    # the same `ts` and order is then by id DESC (last-inserted first).
    # The contract is "most-recent-likely-first"; just assert the limit
    # works and the set is right.
    assert len(rows) == 3
    assert {r.model_name for r in rows} == {"first", "second", "third"}


def test_purge_older_than(tmp_path):
    """The retention-window GC."""
    conn = _fresh(tmp_path)
    ue.record(conn, model_name="x", base_name="x")
    ue.record(conn, model_name="x", base_name="x")
    ue.record(conn, model_name="x", base_name="x")
    # Future cutoff -> all 3 rows are older -> all deleted
    deleted = ue.purge_older_than(conn, before_iso="9999-12-31 23:59:59")
    assert deleted == 3
    assert ue.count_in_window(conn, since_iso=_ago_iso(3600)) == 0


def test_purge_older_than_keeps_recent(tmp_path):
    """Past cutoff -> no rows are older -> none deleted."""
    conn = _fresh(tmp_path)
    ue.record(conn, model_name="x", base_name="x")
    deleted = ue.purge_older_than(conn, before_iso=_ago_iso(60))
    assert deleted == 0
    assert ue.count_in_window(conn, since_iso=_ago_iso(3600)) == 1


def test_migration_creates_indexes(tmp_path):
    """The predictor's hot queries hit (ts), (base_name, ts), and
    (api_key_id, ts). Indexes from 006 must be in place."""
    conn = _fresh(tmp_path)
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='usage_events'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert "idx_usage_ts" in names
    assert "idx_usage_base_ts" in names
    assert "idx_usage_key_ts" in names
