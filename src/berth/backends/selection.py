from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SelectionRule:
    pattern: str
    backend: str


@dataclass(frozen=True)
class SelectionConfig:
    rules: list[SelectionRule]
    default: str


def load_selection(path: Path | None = None) -> SelectionConfig:
    """Load selection rules from a file path, or from the packaged default."""
    if path is None:
        text = files("berth.backends").joinpath("selection.yaml").read_text()
    else:
        text = Path(path).read_text()
    data = yaml.safe_load(text) or {}
    rules = [
        SelectionRule(pattern=r["pattern"], backend=r["backend"])
        for r in data.get("rules", [])
    ]
    return SelectionConfig(rules=rules, default=data.get("default", "vllm"))


def pick_backend(cfg: SelectionConfig, model_name: str) -> str:
    name_lower = model_name.lower()
    for rule in cfg.rules:
        if fnmatch.fnmatch(name_lower, rule.pattern.lower()):
            return rule.backend
    return cfg.default
