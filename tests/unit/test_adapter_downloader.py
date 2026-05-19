import json
from unittest.mock import patch

from berth.lifecycle.adapter_downloader import (
    download_adapter,
    parse_adapter_metadata,
)


def test_download_adapter_calls_snapshot_download(tmp_path):
    fake_dir = tmp_path / "snap"
    fake_dir.mkdir()
    # Two files totaling 3 MB (3 * 1024 * 1024 + 1 byte to force ceil)
    (fake_dir / "adapter_model.safetensors").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    (fake_dir / "adapter_config.json").write_bytes(b"x" * (1 * 1024 * 1024))

    with patch(
        "berth.lifecycle.adapter_downloader.snapshot_download",
        return_value=str(fake_dir),
    ) as mock_sd:
        path, size_mb = download_adapter(
            hf_repo="org/some-lora",
            revision="main",
            cache_dir=tmp_path,
        )
    assert path == str(fake_dir)
    assert size_mb == 4  # 3 MB + 1 byte rounds up to 4
    kwargs = mock_sd.call_args.kwargs
    assert kwargs["repo_id"] == "org/some-lora"
    assert kwargs["revision"] == "main"
    assert kwargs["cache_dir"] == str(tmp_path)


def test_download_adapter_size_zero_for_empty_dir(tmp_path):
    fake_dir = tmp_path / "empty_snap"
    fake_dir.mkdir()
    with patch(
        "berth.lifecycle.adapter_downloader.snapshot_download",
        return_value=str(fake_dir),
    ):
        _, size_mb = download_adapter(
            hf_repo="o/x", revision="main", cache_dir=tmp_path,
        )
    assert size_mb == 0


def test_parse_adapter_metadata_extracts_rank(tmp_path):
    """The PEFT `r` value (LoRA rank) determines the min --max-lora-rank
    the engine needs. We surface it at pull time so the operator can
    catch rank mismatches before the first request."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "adapter_config.json").write_text(json.dumps({
        "base_model_name_or_path": "Qwen/Qwen3-0.6B",
        "peft_type": "LORA",
        "r": 64,
        "lora_alpha": 128,
        "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
    }))
    meta = parse_adapter_metadata(snap)
    assert meta is not None
    assert meta["lora_rank"] == 64


def test_parse_adapter_metadata_missing_config_returns_none(tmp_path):
    """If adapter_config.json is missing (e.g., non-PEFT format), we
    return None rather than raise - older / exotic formats still pull,
    they just don't get rank validation."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "adapter_model.safetensors").write_bytes(b"x")  # no config
    assert parse_adapter_metadata(snap) is None


def test_parse_adapter_metadata_malformed_json_returns_none(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "adapter_config.json").write_text("{not valid json")
    assert parse_adapter_metadata(snap) is None


def test_parse_adapter_metadata_no_rank_field_returns_none(tmp_path):
    """If adapter_config.json exists but has no `r` field, return None
    rather than guess. Operator gets visibility via the engine error
    on first load attempt."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "adapter_config.json").write_text(json.dumps({"peft_type": "OTHER"}))
    assert parse_adapter_metadata(snap) is None


def test_download_adapter_recurses_into_subdirs(tmp_path):
    fake_dir = tmp_path / "snap"
    fake_dir.mkdir()
    nested = fake_dir / "subdir" / "deeper"
    nested.mkdir(parents=True)
    (nested / "weights.bin").write_bytes(b"x" * (5 * 1024 * 1024))
    with patch(
        "berth.lifecycle.adapter_downloader.snapshot_download",
        return_value=str(fake_dir),
    ):
        _, size_mb = download_adapter(
            hf_repo="o/x", revision="main", cache_dir=tmp_path,
        )
    assert size_mb == 5
