"""LRU replay simulator + CLI export/replay smoke tests.

The simulator is the headline metric: it backs the >=30% cold-load reduction
target by giving us an offline LRU baseline to compare recorded traces
against. Behavior we lock down:
- monotonic-clock sorting of out-of-order traces
- per-base cache isolation
- bare-base events excluded from both counters
- reduction_pct positive when recorded beats LRU; zero when LRU has nothing
  to reduce.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from berth import config
from berth.cli import app
from berth.lifecycle.replay import ReplayEvent, simulate_lru


def _ev(ts, base, adapter, cold=False):
    return ReplayEvent(ts=ts, base=base, adapter=adapter, cold_loaded=cold)


def test_simulate_lru_empty_trace_zero_everything():
    result = simulate_lru([], slots_per_base=4)
    assert result.total == 0
    assert result.lru_cold == 0
    assert result.recorded_cold == 0
    assert result.lru_rate == 0.0
    assert result.recorded_rate == 0.0
    assert result.reduction_pct == 0.0


def test_simulate_lru_first_miss_then_hits_same_adapter():
    """Repeated calls to the same (base, adapter) cold-load once."""
    events = [
        _ev("2026-05-13T10:00:00", "qwen3", "a", cold=True),
        _ev("2026-05-13T10:00:01", "qwen3", "a", cold=False),
        _ev("2026-05-13T10:00:02", "qwen3", "a", cold=False),
    ]
    r = simulate_lru(events, slots_per_base=4)
    assert r.total == 3
    assert r.lru_cold == 1
    assert r.recorded_cold == 1


def test_simulate_lru_evicts_lru_when_slots_full():
    """With slots=2, a->b->c forces eviction of `a`; revisiting `a` is cold."""
    events = [
        _ev("t1", "qwen3", "a"),
        _ev("t2", "qwen3", "b"),
        _ev("t3", "qwen3", "c"),  # evicts a
        _ev("t4", "qwen3", "a"),  # cold again
    ]
    r = simulate_lru(events, slots_per_base=2)
    assert r.lru_cold == 4  # all four were misses


def test_simulate_lru_recency_protects_against_eviction():
    """LRU = least-recently-used. Touching `a` keeps it warm despite later
    `b` and `c` arrivals."""
    events = [
        _ev("t1", "qwen3", "a"),
        _ev("t2", "qwen3", "b"),
        _ev("t3", "qwen3", "a"),  # touches a - now b is LRU
        _ev("t4", "qwen3", "c"),  # evicts b
        _ev("t5", "qwen3", "a"),  # still cached - hot
    ]
    r = simulate_lru(events, slots_per_base=2)
    # misses: a, b, c, then a-revisit is a hit -> 3 cold
    assert r.lru_cold == 3


def test_simulate_lru_caches_are_per_base():
    """qwen3:foo and llama:foo are different keys; one base shouldn't be
    able to evict another base's adapter."""
    events = [
        _ev("t1", "qwen3", "foo"),
        _ev("t2", "llama", "foo"),
        _ev("t3", "qwen3", "foo"),  # still in qwen3's cache -> hit
    ]
    r = simulate_lru(events, slots_per_base=1)
    assert r.lru_cold == 2  # only the first two are cold


def test_simulate_lru_excludes_bare_base_events():
    """v2.0 predictor only pre-warms adapters; bare-base events would muddy
    the comparison. simulate_lru must drop them from both counters."""
    events = [
        _ev("t1", "qwen3", None, cold=True),    # excluded
        _ev("t2", "qwen3", "a", cold=True),     # counted
        _ev("t3", "qwen3", None, cold=False),   # excluded
    ]
    r = simulate_lru(events, slots_per_base=4)
    assert r.total == 1
    assert r.recorded_cold == 1
    assert r.lru_cold == 1


def test_simulate_lru_sorts_unsorted_input():
    """Production exports may arrive in any order; the simulator must impose
    chronology before walking the trace."""
    events = [
        _ev("t3", "qwen3", "a"),
        _ev("t1", "qwen3", "a"),
        _ev("t2", "qwen3", "a"),
    ]
    r = simulate_lru(events, slots_per_base=4)
    assert r.lru_cold == 1  # one cold at t1, then two hits


def test_simulate_lru_reduction_pct_positive_when_recorded_better_than_lru():
    """The headline number: when the recorded trace shows fewer cold-loads
    than the LRU baseline, the reduction percentage is positive."""
    # 4 events, all repeated adapter accesses but recorded says only 1 was cold
    # (e.g. predictor pre-warmed before the first request).
    events = [
        _ev("t1", "qwen3", "a", cold=False),  # predictor pre-warmed
        _ev("t2", "qwen3", "b", cold=True),
        _ev("t3", "qwen3", "a", cold=False),
        _ev("t4", "qwen3", "b", cold=False),
    ]
    r = simulate_lru(events, slots_per_base=4)
    assert r.lru_cold == 2  # a and b each cold once on first access
    assert r.recorded_cold == 1
    # reduction = (2 - 1)/2 = 50%
    assert r.reduction_pct == 50.0


def test_simulate_lru_reduction_zero_when_lru_baseline_zero():
    """Empty trace -> no LRU misses -> reduction is 0, not a div-by-zero."""
    r = simulate_lru([], slots_per_base=4)
    assert r.reduction_pct == 0.0


def test_simulate_lru_rejects_nonpositive_slots():
    import pytest
    with pytest.raises(ValueError):
        simulate_lru([], slots_per_base=0)


# ----- CLI integration smoke tests -----

def _seed_usage_db(db_path: Path) -> None:
    """Build a minimal usage_events table at `db_path` for the CLI tests
    to read. We deliberately don't run the migration loader - this is the
    only column set the CLI reads."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE usage_events (
            id INTEGER PRIMARY KEY,
            ts TIMESTAMP,
            api_key_id INTEGER,
            model_name TEXT,
            base_name TEXT,
            adapter_name TEXT,
            deployment_id INTEGER,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            cold_loaded INTEGER DEFAULT 0,
            source_peer_id TEXT
        )
    """)
    rows = [
        ("2026-05-13T10:00:00", "qwen3", "a", 1, 1),
        ("2026-05-13T10:00:01", "qwen3", "a", 0, 1),
        ("2026-05-13T10:00:02", "qwen3", "b", 1, 1),
        ("2026-05-13T10:00:03", "qwen3", None, 0, 1),  # bare base
    ]
    for ts, base, adapter, cold, dep in rows:
        conn.execute(
            "INSERT INTO usage_events (ts, model_name, base_name, "
            "adapter_name, deployment_id, cold_loaded) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, adapter or base, base, adapter, dep, cold),
        )
    conn.commit()
    conn.close()


def test_predict_export_writes_jsonl(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    _seed_usage_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)

    out = tmp_path / "trace.jsonl"
    result = CliRunner().invoke(app, ["predict", "--export", str(out)])
    assert result.exit_code == 0, result.output
    assert "exported 4 events" in result.output

    lines = out.read_text().strip().splitlines()
    assert len(lines) == 4
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["base"] == "qwen3"
    assert parsed[0]["adapter"] == "a"
    assert parsed[0]["cold_loaded"] is True
    assert parsed[3]["adapter"] is None


def test_predict_export_missing_db_clean_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "nope.sqlite")
    result = CliRunner().invoke(app, ["predict", "--export", str(tmp_path / "x.jsonl")])
    assert result.exit_code != 0


def test_predict_replay_reports_comparison(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text("\n".join([
        json.dumps({
            "ts": "t1", "base": "qwen3", "adapter": "a", "cold_loaded": False,
        }),
        json.dumps({
            "ts": "t2", "base": "qwen3", "adapter": "b", "cold_loaded": True,
        }),
        json.dumps({
            "ts": "t3", "base": "qwen3", "adapter": "a", "cold_loaded": False,
        }),
    ]))
    result = CliRunner().invoke(app, ["predict", "--replay", str(trace)])
    assert result.exit_code == 0, result.output
    # The header line shows the comparable subset is 3 events.
    assert "events (adapter-bearing):  3" in result.output
    assert "recorded cold-loads:       1" in result.output
    # LRU misses: a (cold), b (cold), a (hit) -> 2 cold
    assert "LRU baseline cold-loads:   2" in result.output
    # Reduction (2-1)/2 = 50%
    assert "reduction vs LRU:          50.0%" in result.output


def test_predict_replay_rejects_malformed_jsonl(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text("not json\n")
    result = CliRunner().invoke(app, ["predict", "--replay", str(trace)])
    assert result.exit_code != 0


def test_predict_replay_rejects_missing_fields(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({"ts": "t1"}) + "\n")
    result = CliRunner().invoke(app, ["predict", "--replay", str(trace)])
    assert result.exit_code != 0


def test_predict_mutually_exclusive_flags(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text("")
    result = CliRunner().invoke(
        app, ["predict", "--export", str(trace), "--replay", str(trace)],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_predict_replay_all_bare_base_reports_nothing_to_compare(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text("\n".join([
        json.dumps({"ts": "t1", "base": "qwen3", "adapter": None, "cold_loaded": True}),
    ]))
    result = CliRunner().invoke(app, ["predict", "--replay", str(trace)])
    assert result.exit_code == 0
    assert "nothing to compare" in result.output
