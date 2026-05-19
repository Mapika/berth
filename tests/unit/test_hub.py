from unittest.mock import MagicMock, patch

from berth.backends.hub import latest_stable_tag


@patch("berth.backends.hub.httpx.get")
def test_latest_stable_tag_picks_first_semver(mock_get):
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "results": [
                {"name": "nightly-abc", "last_updated": "2026-05-11"},
                {"name": "v0.5.11", "last_updated": "2026-05-10"},
                {"name": "v0.5.5.post1", "last_updated": "2026-05-01"},
            ]
        },
    )
    mock_get.return_value.raise_for_status = MagicMock()
    tag = latest_stable_tag("lmsysorg/sglang")
    assert tag == "v0.5.11"


@patch("berth.backends.hub.httpx.get")
def test_latest_stable_tag_returns_none_on_no_match(mock_get):
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: {"results": [{"name": "nightly-xyz"}]},
    )
    mock_get.return_value.raise_for_status = MagicMock()
    assert latest_stable_tag("foo/bar") is None


@patch("berth.backends.hub.httpx.get", side_effect=Exception("network down"))
def test_latest_stable_tag_handles_network_error(mock_get):
    assert latest_stable_tag("foo/bar") is None
