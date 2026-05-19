import pytest

from berth.auth import tiers


def test_load_default_tiers_contains_standard():
    cfg = tiers.load_tiers()
    assert "standard" in cfg
    assert cfg["standard"].rpm == 60
    assert cfg["admin"].rpm is None  # unlimited


def test_resolve_limits_no_override():
    cfg = tiers.load_tiers()
    limits = tiers.resolve_limits(cfg, tier="standard", overrides=tiers.Overrides())
    assert limits.rpm == 60
    assert limits.tpm == 100_000


def test_resolve_limits_with_override():
    cfg = tiers.load_tiers()
    limits = tiers.resolve_limits(
        cfg, tier="standard",
        overrides=tiers.Overrides(rpm=200, tpm=None),
    )
    assert limits.rpm == 200
    assert limits.tpm == 100_000


def test_unknown_tier_raises():
    cfg = tiers.load_tiers()
    with pytest.raises(KeyError):
        tiers.resolve_limits(cfg, tier="legendary", overrides=tiers.Overrides())
