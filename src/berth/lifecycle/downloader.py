from __future__ import annotations

from pathlib import Path

from huggingface_hub import snapshot_download


def download_model(
    *,
    hf_repo: str,
    revision: str,
    cache_dir: Path,
) -> str:
    return snapshot_download(
        repo_id=hf_repo,
        revision=revision,
        cache_dir=str(cache_dir),
    )
