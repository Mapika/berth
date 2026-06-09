from __future__ import annotations

from dataclasses import dataclass

import pytest

from berth.net_guard import assert_dialable_engine, is_blocked_adopted_address


@dataclass
class _Dep:
    source: str
    container_address: str | None


@pytest.mark.parametrize(
    "addr",
    [
        "169.254.169.254",  # cloud metadata endpoint
        "169.254.0.1",      # link-local
        "224.0.0.1",        # multicast
        "0.0.0.0",          # unspecified
    ],
)
def test_blocked_addresses(addr):
    assert is_blocked_adopted_address(addr) is True


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",        # loopback — adopt-localhost is legitimate
        "10.1.2.3",         # private — adopt-LAN
        "172.17.0.2",       # docker bridge
        "engine-abc",       # container name / hostname
        "tunnel",           # remote sentinel
        None,
        "",
    ],
)
def test_allowed_addresses(addr):
    assert is_blocked_adopted_address(addr) is False


def test_assert_dialable_managed_is_noop():
    # Managed deployments carry a berth-derived address: never blocked, even
    # if it somehow parses as a sensitive range.
    assert_dialable_engine(_Dep(source="managed", container_address="169.254.169.254"))


def test_assert_dialable_adopted_unsafe_raises():
    with pytest.raises(ValueError):
        assert_dialable_engine(_Dep(source="adopted", container_address="169.254.169.254"))


def test_assert_dialable_adopted_safe_ok():
    assert_dialable_engine(_Dep(source="adopted", container_address="10.0.0.5"))
