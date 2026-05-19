from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from huggingface_hub import snapshot_download


def download_adapter(
    *,
    hf_repo: str,
    revision: str,
    cache_dir: Path,
) -> tuple[str, int]:
    """Download a LoRA / DoRA adapter via HF Hub. Returns (local_path, size_mb).

    Adapter dirs are typically <500 MB so this stays synchronous +
    simple, matching `lifecycle.downloader.download_model`. The size
    return value is stored in `adapters.size_mb` for UI display and
    quota planning; computed by walking the snapshot dir.
    """
    local_path = snapshot_download(
        repo_id=hf_repo,
        revision=revision,
        cache_dir=str(cache_dir),
    )
    size_bytes = sum(
        f.stat().st_size for f in Path(local_path).rglob("*") if f.is_file()
    )
    size_mb = (size_bytes + 1024 * 1024 - 1) // (1024 * 1024)  # ceil
    return local_path, int(size_mb)


def parse_adapter_metadata(local_path: str | Path) -> dict[str, Any] | None:
    """Extract structural metadata from a downloaded adapter's PEFT config.

    Returns `{"lora_rank": int}` when adapter_config.json is present and
    `r` is set; None for any other case (missing file, malformed JSON,
    no `r` field). Callers use rank for early validation against a
    deployment's --max-lora-rank, avoiding cryptic 502s from the engine
    on first hot-load.
    """
    cfg_path = Path(local_path) / "adapter_config.json"
    if not cfg_path.is_file():
        return None
    try:
        cfg = json.loads(cfg_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    rank = cfg.get("r")
    if not isinstance(rank, int) or rank <= 0:
        return None
    return {"lora_rank": rank}
