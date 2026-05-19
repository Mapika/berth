from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Limits:
    rpm: int | None
    tpm: int | None
    rph: int | None
    tph: int | None
    rpd: int | None
    tpd: int | None
    rpw: int | None
    tpw: int | None


@dataclass(frozen=True)
class Overrides:
    rpm: int | None = None
    tpm: int | None = None
    rph: int | None = None
    tph: int | None = None
    rpd: int | None = None
    tpd: int | None = None
    rpw: int | None = None
    tpw: int | None = None


def load_tiers(path: Path | None = None) -> dict[str, Limits]:
    if path is None:
        text = files("berth.auth").joinpath("tiers.yaml").read_text()
    else:
        text = Path(path).read_text()
    data = yaml.safe_load(text) or {}
    out: dict[str, Limits] = {}
    for name, lim in (data.get("tiers") or {}).items():
        out[name] = Limits(
            rpm=lim.get("rpm"), tpm=lim.get("tpm"),
            rph=lim.get("rph"), tph=lim.get("tph"),
            rpd=lim.get("rpd"), tpd=lim.get("tpd"),
            rpw=lim.get("rpw"), tpw=lim.get("tpw"),
        )
    return out


def resolve_limits(
    cfg: dict[str, Limits], *, tier: str, overrides: Overrides
) -> Limits:
    if tier not in cfg:
        raise KeyError(f"unknown tier {tier!r}")
    base = cfg[tier]

    def pick(o: int | None, b: int | None) -> int | None:
        return o if o is not None else b

    return Limits(
        rpm=pick(overrides.rpm, base.rpm),
        tpm=pick(overrides.tpm, base.tpm),
        rph=pick(overrides.rph, base.rph),
        tph=pick(overrides.tph, base.tph),
        rpd=pick(overrides.rpd, base.rpd),
        tpd=pick(overrides.tpd, base.tpd),
        rpw=pick(overrides.rpw, base.rpw),
        tpw=pick(overrides.tpw, base.tpw),
    )
