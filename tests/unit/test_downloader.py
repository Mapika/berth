from unittest.mock import patch

from berth.lifecycle.downloader import download_model


def test_download_model_calls_snapshot_download(tmp_path):
    with patch("berth.lifecycle.downloader.snapshot_download") as mock_sd:
        mock_sd.return_value = str(tmp_path / "x")
        result = download_model(
            hf_repo="meta-llama/Llama-3.2-1B-Instruct",
            revision="main",
            cache_dir=tmp_path,
        )
    assert result == str(tmp_path / "x")
    mock_sd.assert_called_once()
    kwargs = mock_sd.call_args.kwargs
    assert kwargs["repo_id"] == "meta-llama/Llama-3.2-1B-Instruct"
    assert kwargs["revision"] == "main"
    assert kwargs["cache_dir"] == str(tmp_path)
