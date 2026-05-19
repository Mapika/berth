from berth.auth import limiter, tiers
from berth.store import api_keys, db, key_usage


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def test_allow_when_no_history(tmp_path):
    conn = _fresh(tmp_path)
    _, k = api_keys.create(conn, name="a", tier="trial")
    cfg = tiers.load_tiers()
    decision = limiter.check(conn, key=k, tier_cfg=cfg)
    assert isinstance(decision, limiter.Allowed)


def test_deny_when_rpm_exceeded(tmp_path):
    conn = _fresh(tmp_path)
    _, k = api_keys.create(conn, name="a", tier="trial")  # rpm=10
    cfg = tiers.load_tiers()
    for _ in range(10):
        key_usage.record(conn, key_id=k.id, tokens_in=1, tokens_out=0)
    decision = limiter.check(conn, key=k, tier_cfg=cfg)
    assert isinstance(decision, limiter.Denied)
    assert decision.limit_name == "rpm"


def test_deny_when_tpm_exceeded(tmp_path):
    conn = _fresh(tmp_path)
    _, k = api_keys.create(conn, name="a", tier="trial")  # tpm=10000
    cfg = tiers.load_tiers()
    key_usage.record(conn, key_id=k.id, tokens_in=10000, tokens_out=1)
    decision = limiter.check(conn, key=k, tier_cfg=cfg)
    assert isinstance(decision, limiter.Denied)
    assert decision.limit_name == "tpm"


def test_admin_tier_unlimited(tmp_path):
    conn = _fresh(tmp_path)
    _, k = api_keys.create(conn, name="a", tier="admin")
    cfg = tiers.load_tiers()
    for _ in range(10_000):
        key_usage.record(conn, key_id=k.id, tokens_in=1_000_000, tokens_out=0)
    decision = limiter.check(conn, key=k, tier_cfg=cfg)
    assert isinstance(decision, limiter.Allowed)


def test_per_key_override_loosens(tmp_path):
    conn = _fresh(tmp_path)
    _, k = api_keys.create(conn, name="a", tier="trial", rpm_override=100)
    cfg = tiers.load_tiers()
    for _ in range(50):
        key_usage.record(conn, key_id=k.id, tokens_in=1, tokens_out=0)
    decision = limiter.check(conn, key=k, tier_cfg=cfg)
    assert isinstance(decision, limiter.Allowed)
