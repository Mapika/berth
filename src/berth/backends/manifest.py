from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml

from berth import config


@dataclass(frozen=True)
class Headroom:
    factor: float
    min_extra_mb: int
    min_floor_pct: int

    def effective_util(self, *, reserved_mb: int, per_gpu_mb: int) -> float:
        """Compute the gpu_memory_utilization fraction to hand to the engine."""
        target = max(
            reserved_mb * self.factor,
            reserved_mb + self.min_extra_mb,
            per_gpu_mb * (self.min_floor_pct / 100.0),
        )
        return min(0.95, max(0.05, target / per_gpu_mb))


@dataclass(frozen=True)
class EngineManifest:
    name: str
    image: str
    pinned_tag: str
    health_path: str
    openai_base: str
    metrics_path: str
    internal_port: int
    headroom: Headroom
    # Engine-version-specific flags appended to argv after the backend's own
    # flags but before plan.extra_args. Use this for workarounds tied to a
    # pinned image tag - when the tag is bumped, revisit these.
    extra_launch_args: tuple[str, ...] = ()

    @property
    def image_default(self) -> str:
        return f"{self.image}:{self.pinned_tag}"


def _parse(text: str) -> dict:
    return yaml.safe_load(text) or {}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for name, ov in override.items():
        if name in out and isinstance(ov, dict):
            merged = dict(out[name])
            for k, v in ov.items():
                if k == "headroom" and isinstance(v, dict) and "headroom" in merged:
                    hr = dict(merged["headroom"])
                    hr.update(v)
                    merged["headroom"] = hr
                else:
                    merged[k] = v
            out[name] = merged
        else:
            out[name] = ov
    return out


def load_manifest(path: Path | None = None) -> dict[str, EngineManifest]:
    if path is None:
        text = files("berth.backends").joinpath("backends.yaml").read_text()
        raw = _parse(text)
        override_path = config.BERTH_DIR / "backends.override.yaml"
        if override_path.exists():
            raw = _merge(raw, _parse(override_path.read_text()))
    else:
        raw = _parse(Path(path).read_text())

    out: dict[str, EngineManifest] = {}
    for name, e in raw.items():
        hr = e.get("headroom") or {}
        out[name] = EngineManifest(
            name=name,
            image=e["image"],
            pinned_tag=e["pinned_tag"],
            health_path=e.get("health_path", "/health"),
            openai_base=e.get("openai_base", "/v1"),
            metrics_path=e.get("metrics_path", "/metrics"),
            internal_port=int(e["internal_port"]),
            headroom=Headroom(
                factor=float(hr.get("factor", 1.5)),
                min_extra_mb=int(hr.get("min_extra_mb", 2048)),
                min_floor_pct=int(hr.get("min_floor_pct", 15)),
            ),
            extra_launch_args=tuple(e.get("extra_launch_args") or ()),
        )
    return out


def write_override(updates: dict[str, dict]) -> Path:
    """Persist user-level overrides to ~/.berth/backends.override.yaml.

    `updates` is {engine_name: partial_engine_config} (typically just
    {'pinned_tag': 'v0.20.5'}). Existing override entries are merged.
    """
    override_path = config.BERTH_DIR / "backends.override.yaml"
    override_path.parent.mkdir(parents=True, exist_ok=True)
    if override_path.exists():
        current = _parse(override_path.read_text())
    else:
        current = {}
    merged = _merge(current, updates)
    override_path.write_text(yaml.safe_dump(merged, sort_keys=True))
    return override_path
