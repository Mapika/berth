from __future__ import annotations

import pytest

from berth.cli.agent_cmd import parse_enrollment_uri
from berth.cli.nodes_cmd import build_enrollment_uri


def test_round_trip_simple():
    uri = build_enrollment_uri(
        leader="https://cluster.example:11501",
        token="abc123",
        ca_fp="sha256:deadbeef",
    )
    leader, token, fp = parse_enrollment_uri(uri)
    assert leader == "https://cluster.example:11501"
    assert token == "abc123"
    assert fp == "sha256:deadbeef"


def test_round_trip_special_chars():
    """Token + fingerprint must survive URL-encoding intact."""
    uri = build_enrollment_uri(
        leader="https://leader.io:11501/",
        token="a/b+c=d",
        ca_fp="sha256:7f3a:4d",
    )
    leader, token, fp = parse_enrollment_uri(uri)
    assert leader == "https://leader.io:11501/"
    assert token == "a/b+c=d"
    assert fp == "sha256:7f3a:4d"


def test_rejects_wrong_scheme():
    with pytest.raises(ValueError, match="berth://enroll"):
        parse_enrollment_uri("https://enroll?token=x")


def test_rejects_missing_params():
    with pytest.raises(ValueError, match="missing required param"):
        parse_enrollment_uri("berth://enroll?leader=https://x")


def test_rejects_non_http_leader():
    with pytest.raises(ValueError, match="http"):
        parse_enrollment_uri(
            "berth://enroll?leader=ftp://x&token=t&ca_fp=sha256:x"
        )
