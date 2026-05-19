from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from berth.auth.tiers import Limits, Overrides, resolve_limits
from berth.store import api_keys, key_usage


@dataclass(frozen=True)
class Allowed:
    pass


@dataclass(frozen=True)
class Denied:
    limit_name: str
    limit_value: int
    current: int
    window_s: int
    retry_after_s: int


Decision = Allowed | Denied


# (limit_attr, kind, window_s, label)  kind in {"req","tok"}
_WINDOWS = [
    ("rpm", "req", 60, "rpm"),
    ("tpm", "tok", 60, "tpm"),
    ("rph", "req", 3600, "rph"),
    ("tph", "tok", 3600, "tph"),
    ("rpd", "req", 86_400, "rpd"),
    ("tpd", "tok", 86_400, "tpd"),
    ("rpw", "req", 604_800, "rpw"),
    ("tpw", "tok", 604_800, "tpw"),
]


def check(
    conn: sqlite3.Connection,
    *,
    key: api_keys.ApiKey,
    tier_cfg: dict[str, Limits],
) -> Decision:
    overrides = Overrides(
        rpm=key.rpm_override, tpm=key.tpm_override,
        rph=key.rph_override, tph=key.tph_override,
        rpd=key.rpd_override, tpd=key.tpd_override,
        rpw=key.rpw_override, tpw=key.tpw_override,
    )
    limits = resolve_limits(tier_cfg, tier=key.tier, overrides=overrides)

    for attr, kind, window_s, label in _WINDOWS:
        limit = getattr(limits, attr)
        if limit is None:
            continue
        reqs, toks = key_usage.totals_in_window(conn, key_id=key.id, window_s=window_s)
        current = reqs if kind == "req" else toks
        if current >= limit:
            return Denied(
                limit_name=label,
                limit_value=limit,
                current=current,
                window_s=window_s,
                retry_after_s=_retry_after(window_s),
            )
    return Allowed()


def _retry_after(window_s: int) -> int:
    return max(1, window_s // 2)
