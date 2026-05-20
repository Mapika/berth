"""Predictor rules + combination. Each rule is tested against a hand-
crafted usage_events trace; the combined `candidates()` test asserts
the dedupe + score-max semantics from the design.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from berth.lifecycle.predictor import (
    Candidate,
    KeyAffinityConfig,
    Predictor,
    PredictorConfig,
    RuleConfig,
    SequencingConfig,
    _hour_of_week,
)
from berth.store import db


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def _seed_key(conn: sqlite3.Connection, key_id: int) -> None:
    """Ensure an api_keys row exists for the FK target. Idempotent."""
    conn.execute(
        """
        INSERT OR IGNORE INTO api_keys (id, name, prefix, key_hash, tier)
        VALUES (?, ?, ?, ?, 'standard')
        """,
        (key_id, f"k{key_id}", f"sk-test{key_id}", f"hash-{key_id}"),
    )


def _insert(
    conn: sqlite3.Connection,
    *,
    ts: datetime,
    base: str,
    adapter: str | None = None,
    api_key_id: int | None = None,
) -> None:
    """Direct INSERT with explicit ts (the store's record() uses
    CURRENT_TIMESTAMP and the predictor needs deterministic times)."""
    if api_key_id is not None:
        _seed_key(conn, api_key_id)
    conn.execute(
        """
        INSERT INTO usage_events (ts, api_key_id, model_name, base_name,
                                  adapter_name, deployment_id, tokens_in,
                                  tokens_out, cold_loaded)
        VALUES (?, ?, ?, ?, ?, NULL, 0, 0, 0)
        """,
        (
            ts.strftime("%Y-%m-%d %H:%M:%S"),
            api_key_id,
            adapter or base,
            base,
            adapter,
        ),
    )


# ---- _hour_of_week ----

def test_hour_of_week_matches_sqlite_strftime():
    """The Python helper must agree with SQLite's strftime('%w','%H')
    so the time-of-day rule's SQL filter and the Python now-computation
    pick the same bucket."""
    # Sunday 2026-05-10 14:35 UTC -> SQLite weekday=0, hour=14 -> 14
    dt = datetime(2026, 5, 10, 14, 35, tzinfo=UTC)  # Sunday
    assert _hour_of_week(dt) == 14
    # Monday 00:00 UTC -> 24
    dt = datetime(2026, 5, 11, 0, 0, tzinfo=UTC)
    assert _hour_of_week(dt) == 24
    # Saturday 23:59 UTC -> 6*24 + 23 = 167 (last bucket)
    dt = datetime(2026, 5, 16, 23, 59, tzinfo=UTC)
    assert _hour_of_week(dt) == 167


# ---- Rule 1: time-of-day ----

def test_time_of_day_returns_models_active_in_next_hour(tmp_path):
    """A model loaded N times in the same hour-of-week over past 30d
    becomes a candidate; the rule pre-warms for the upcoming hour."""
    conn = _fresh(tmp_path)
    # Pin "now" so the predictor looks at the same bucket we seed.
    fake_now = datetime(2026, 5, 11, 13, 30, tzinfo=UTC)  # Mon 13:30 -> next 14:00
    # 4 events at Mon 14:xx across the past 4 weeks, all for the same model.
    for week in range(4):
        ts = fake_now - timedelta(days=7 * week) + timedelta(minutes=50)
        _insert(conn, ts=ts, base="qwen3-7b")
    # And one unrelated event at a different hour-of-week.
    _insert(conn, ts=fake_now - timedelta(hours=3), base="other")

    p = Predictor(
        conn,
        config=PredictorConfig(
            sequencing=SequencingConfig(enabled=False),
            key_affinity=KeyAffinityConfig(enabled=False),
        ),
        now_fn=lambda: fake_now,
    )
    cands = p.candidates()
    assert len(cands) == 1
    assert cands[0].base_name == "qwen3-7b"
    assert cands[0].adapter_name is None
    assert cands[0].score == 1.0  # normalized to max
    assert "time-of-day" in cands[0].reason
    assert "4x" in cands[0].reason


def test_time_of_day_ignores_events_outside_retention(tmp_path):
    """retention_days=30 must drop older events from the bucket count."""
    conn = _fresh(tmp_path)
    fake_now = datetime(2026, 5, 11, 13, 30, tzinfo=UTC)
    # Event 60 days ago at the matching hour-of-week.
    _insert(conn, ts=fake_now - timedelta(days=60) + timedelta(hours=1), base="old-model")
    # And a fresh one this week.
    _insert(conn, ts=fake_now + timedelta(hours=1) - timedelta(days=7), base="recent-model")

    p = Predictor(
        conn,
        config=PredictorConfig(
            retention_days=30,
            sequencing=SequencingConfig(enabled=False),
            key_affinity=KeyAffinityConfig(enabled=False),
        ),
        now_fn=lambda: fake_now,
    )
    cands = p.candidates()
    names = {c.base_name for c in cands}
    assert "old-model" not in names
    assert "recent-model" in names


# ---- Rule 2: sequencing ----

def test_sequencing_emits_candidate_when_p_exceeds_threshold(tmp_path):
    """If Y follows X within window in >=30% of historical X events,
    Y is a candidate when X just fired."""
    conn = _fresh(tmp_path)
    fake_now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    # Historical: 4 X events, 3 of them followed by Y within 30s.
    for i in range(4):
        x_ts = fake_now - timedelta(hours=2) - timedelta(minutes=i)
        _insert(conn, ts=x_ts, base="qwen3-7b")
        if i < 3:
            _insert(conn, ts=x_ts + timedelta(seconds=10), base="other-model")
    # Trigger: an X event within the last 30s.
    _insert(conn, ts=fake_now - timedelta(seconds=5), base="qwen3-7b")

    p = Predictor(
        conn,
        config=PredictorConfig(
            time_of_day=RuleConfig(enabled=False),
            key_affinity=KeyAffinityConfig(enabled=False),
            sequencing=SequencingConfig(window_s=30, min_p=0.30),
        ),
        now_fn=lambda: fake_now,
    )
    cands = p.candidates()
    # Y should be the only candidate (X is the trigger, not a candidate
    # for itself).
    assert len(cands) == 1
    assert cands[0].base_name == "other-model"
    # P = 3/5 historical X events (4 historical + 1 trigger). Score
    # equals P, weight = 1.0.
    assert 0.55 < cands[0].score < 0.65
    assert "sequencing" in cands[0].reason


def test_sequencing_returns_empty_when_no_recent_trigger(tmp_path):
    """No event in the last window_s -> no sequencing candidates."""
    conn = _fresh(tmp_path)
    fake_now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    _insert(conn, ts=fake_now - timedelta(minutes=5), base="x")
    p = Predictor(
        conn,
        config=PredictorConfig(
            time_of_day=RuleConfig(enabled=False),
            key_affinity=KeyAffinityConfig(enabled=False),
        ),
        now_fn=lambda: fake_now,
    )
    assert p.candidates() == []


def test_sequencing_below_min_p_filters_out(tmp_path):
    """If P(Y|X) < min_p the candidate is dropped."""
    conn = _fresh(tmp_path)
    fake_now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    # 10 X events, only 2 followed by Y (P=0.18, below default 0.30).
    for i in range(10):
        x_ts = fake_now - timedelta(hours=2) - timedelta(minutes=i)
        _insert(conn, ts=x_ts, base="x")
        if i < 2:
            _insert(conn, ts=x_ts + timedelta(seconds=5), base="y")
    _insert(conn, ts=fake_now - timedelta(seconds=5), base="x")

    p = Predictor(
        conn,
        config=PredictorConfig(
            time_of_day=RuleConfig(enabled=False),
            key_affinity=KeyAffinityConfig(enabled=False),
        ),
        now_fn=lambda: fake_now,
    )
    assert p.candidates() == []


# ---- Rule 3: key-affinity ----

def test_key_affinity_returns_top_k_for_active_keys(tmp_path):
    """API key with a recent event gets its top_k pairs as candidates."""
    conn = _fresh(tmp_path)
    fake_now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    # Key 42: heavy on a + b, light on c.
    for _ in range(5):
        _insert(conn, ts=fake_now - timedelta(hours=1), base="a", api_key_id=42)
    for _ in range(3):
        _insert(conn, ts=fake_now - timedelta(hours=2), base="b", api_key_id=42)
    _insert(conn, ts=fake_now - timedelta(hours=2), base="c", api_key_id=42)
    # Recent event to mark the key as "active".
    _insert(conn, ts=fake_now - timedelta(seconds=30), base="a", api_key_id=42)

    p = Predictor(
        conn,
        config=PredictorConfig(
            time_of_day=RuleConfig(enabled=False),
            sequencing=SequencingConfig(enabled=False),
            key_affinity=KeyAffinityConfig(top_k_per_key=2, idle_seconds=300),
        ),
        now_fn=lambda: fake_now,
    )
    cands = p.candidates()
    names = {c.base_name for c in cands}
    assert names == {"a", "b"}  # top 2; c excluded
    # `a` is the heaviest -> score=1.0 (normalized to per-key max).
    by_name = {c.base_name: c for c in cands}
    assert by_name["a"].score == 1.0
    assert by_name["b"].score < 1.0


def test_key_affinity_ignores_idle_keys(tmp_path):
    """Keys with no event in the last idle_seconds contribute nothing."""
    conn = _fresh(tmp_path)
    fake_now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    # Key 42 fired 1 hour ago - well outside idle_seconds=300.
    _insert(conn, ts=fake_now - timedelta(hours=1), base="x", api_key_id=42)
    p = Predictor(
        conn,
        config=PredictorConfig(
            time_of_day=RuleConfig(enabled=False),
            sequencing=SequencingConfig(enabled=False),
        ),
        now_fn=lambda: fake_now,
    )
    assert p.candidates() == []


# ---- Combined ----

def test_candidates_dedup_keeps_max_score(tmp_path):
    """When two rules vote for the same (base, adapter), the
    higher-score one wins. Reason follows that winner."""
    conn = _fresh(tmp_path)
    fake_now = datetime(2026, 5, 11, 13, 30, tzinfo=UTC)
    # Time-of-day on `shared` (one event at next-hour bucket).
    _insert(conn, ts=fake_now + timedelta(hours=1) - timedelta(days=7), base="shared")
    # Key-affinity on `shared` (recent key activity).
    _insert(conn, ts=fake_now - timedelta(hours=1), base="shared", api_key_id=1)
    _insert(conn, ts=fake_now - timedelta(hours=1), base="shared", api_key_id=1)
    _insert(conn, ts=fake_now - timedelta(seconds=30), base="shared", api_key_id=1)

    p = Predictor(
        conn,
        config=PredictorConfig(
            sequencing=SequencingConfig(enabled=False),
            # Bias weights so we can predict the winner deterministically.
            time_of_day=RuleConfig(enabled=True, weight=0.5),
            key_affinity=KeyAffinityConfig(enabled=True, weight=1.0),
        ),
        now_fn=lambda: fake_now,
    )
    cands = p.candidates()
    assert len(cands) == 1
    # Key-affinity weight=1.0 > time-of-day weight=0.5; key-affinity wins.
    assert "key-affinity" in cands[0].reason


def test_candidates_sorted_descending_by_score(tmp_path):
    conn = _fresh(tmp_path)
    fake_now = datetime(2026, 5, 11, 13, 30, tzinfo=UTC)
    # Three models in the next-hour bucket with different counts.
    next_hour = fake_now + timedelta(hours=1)
    for _ in range(5):
        _insert(conn, ts=next_hour - timedelta(days=7), base="heavy")
    for _ in range(2):
        _insert(conn, ts=next_hour - timedelta(days=14), base="medium")
    _insert(conn, ts=next_hour - timedelta(days=21), base="light")

    p = Predictor(
        conn,
        config=PredictorConfig(
            sequencing=SequencingConfig(enabled=False),
            key_affinity=KeyAffinityConfig(enabled=False),
        ),
        now_fn=lambda: fake_now,
    )
    cands = p.candidates()
    assert [c.base_name for c in cands] == ["heavy", "medium", "light"]
    assert cands[0].score > cands[1].score > cands[2].score


def test_empty_db_returns_empty_candidates(tmp_path):
    conn = _fresh(tmp_path)
    p = Predictor(conn)
    assert p.candidates() == []


def test_disabled_rule_contributes_nothing(tmp_path):
    """If a rule is disabled it must not even be queried - verified via
    no candidates surfacing from data the rule would otherwise match."""
    conn = _fresh(tmp_path)
    fake_now = datetime(2026, 5, 11, 13, 30, tzinfo=UTC)
    _insert(conn, ts=fake_now + timedelta(hours=1) - timedelta(days=7), base="x")
    p = Predictor(
        conn,
        config=PredictorConfig(
            time_of_day=RuleConfig(enabled=False),
            sequencing=SequencingConfig(enabled=False),
            key_affinity=KeyAffinityConfig(enabled=False),
        ),
        now_fn=lambda: fake_now,
    )
    assert p.candidates() == []


def test_candidate_key_separates_base_from_adapter(tmp_path):
    """qwen3-0_6b and qwen3-0_6b:tone-formal are distinct candidates."""
    conn = _fresh(tmp_path)
    fake_now = datetime(2026, 5, 11, 13, 30, tzinfo=UTC)
    bucket_ts = fake_now + timedelta(hours=1) - timedelta(days=7)
    _insert(conn, ts=bucket_ts, base="qwen3-0_6b")
    _insert(conn, ts=bucket_ts, base="qwen3-0_6b", adapter="tone-formal")

    p = Predictor(
        conn,
        config=PredictorConfig(
            sequencing=SequencingConfig(enabled=False),
            key_affinity=KeyAffinityConfig(enabled=False),
        ),
        now_fn=lambda: fake_now,
    )
    keys = {(c.base_name, c.adapter_name) for c in p.candidates()}
    assert keys == {("qwen3-0_6b", None), ("qwen3-0_6b", "tone-formal")}


def test_candidate_key_property_round_trips():
    """Candidate.key collapses (base, adapter) for dedupe - both branches."""
    c1 = Candidate(base_name="a", adapter_name=None, score=0.1, reason="r")
    c2 = Candidate(base_name="a", adapter_name="x", score=0.1, reason="r")
    assert c1.key == ("a", None)
    assert c2.key == ("a", "x")
    assert c1.key != c2.key


# ---- PredictorConfig.load (predictor.yaml) ----

def test_predictor_config_load_returns_defaults_when_file_absent(tmp_path):
    cfg = PredictorConfig.load(tmp_path / "missing.yaml")
    assert cfg.enabled is False
    assert cfg.tick_interval_s == 30
    assert cfg.max_prewarm_per_tick == 2
    assert cfg.retention_days == 30
    assert cfg.sequencing.window_s == 30
    assert cfg.sequencing.min_p == 0.30
    assert cfg.key_affinity.top_k_per_key == 5
    assert cfg.key_affinity.idle_seconds == 300


def test_predictor_config_load_partial_yaml_keeps_defaults_for_missing_keys(tmp_path):
    p = tmp_path / "predictor.yaml"
    p.write_text(
        "enabled: false\n"
        "rules:\n"
        "  sequencing:\n"
        "    min_p: 0.5\n"
    )
    cfg = PredictorConfig.load(p)
    assert cfg.enabled is False
    assert cfg.tick_interval_s == 30  # default
    assert cfg.sequencing.min_p == 0.5  # overridden
    assert cfg.sequencing.window_s == 30  # default
    assert cfg.time_of_day.enabled is True  # default


def test_predictor_config_load_malformed_yaml_falls_back_to_defaults(tmp_path):
    p = tmp_path / "predictor.yaml"
    p.write_text("not: valid: yaml:")
    assert PredictorConfig.load(p) == PredictorConfig()
