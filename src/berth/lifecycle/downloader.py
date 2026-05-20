from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from huggingface_hub import snapshot_download
from huggingface_hub.utils import disable_progress_bars


def download_model(
    *,
    hf_repo: str,
    revision: str,
    cache_dir: Path,
    quiet: bool = False,
) -> str:
    progress = disable_progress_bars() if quiet else nullcontext()
    with progress:
        return snapshot_download(
            repo_id=hf_repo,
            revision=revision,
            cache_dir=str(cache_dir),
        )
