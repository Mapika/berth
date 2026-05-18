from __future__ import annotations

import pytest
from fastapi import HTTPException, Request

from serve_engine.daemon import admin as admin_mod


def _make_request(client_ip: str | None) -> Request:
    """Build a minimal ASGI scope so `_client_ip` and `_rate_limit`
    can run without spinning up a real server."""
    scope = {
        "type": "http",
        "client": (client_ip, 4242) if client_ip else None,
        "headers": [],
    }
    return Request(scope)


def test_rate_limit_allows_under_limit(monkeypatch):
    admin_mod._rl_buckets.clear()
    req = _make_request("1.2.3.4")
    for _ in range(5):
        admin_mod._rate_limit(req, route="t", limit=10, window_s=60.0)


def test_rate_limit_blocks_over_limit():
    admin_mod._rl_buckets.clear()
    req = _make_request("1.2.3.4")
    for _ in range(3):
        admin_mod._rate_limit(req, route="t", limit=3, window_s=60.0)
    with pytest.raises(HTTPException) as ei:
        admin_mod._rate_limit(req, route="t", limit=3, window_s=60.0)
    assert ei.value.status_code == 429


def test_rate_limit_per_ip_independent():
    admin_mod._rl_buckets.clear()
    a = _make_request("1.1.1.1")
    b = _make_request("2.2.2.2")
    for _ in range(2):
        admin_mod._rate_limit(a, route="t", limit=2, window_s=60.0)
    # B still has full quota.
    admin_mod._rate_limit(b, route="t", limit=2, window_s=60.0)
    admin_mod._rate_limit(b, route="t", limit=2, window_s=60.0)
    with pytest.raises(HTTPException):
        admin_mod._rate_limit(b, route="t", limit=2, window_s=60.0)


def test_rate_limit_uds_exempt():
    admin_mod._rl_buckets.clear()
    req = _make_request(None)  # UDS
    for _ in range(1000):
        admin_mod._rate_limit(req, route="t", limit=2, window_s=60.0)


def test_rate_limit_window_resets(monkeypatch):
    admin_mod._rl_buckets.clear()
    now = [1000.0]
    monkeypatch.setattr(
        admin_mod._rl_time, "monotonic", lambda: now[0]
    )
    req = _make_request("9.9.9.9")
    for _ in range(2):
        admin_mod._rate_limit(req, route="t", limit=2, window_s=10.0)
    with pytest.raises(HTTPException):
        admin_mod._rate_limit(req, route="t", limit=2, window_s=10.0)
    # Advance past the window.
    now[0] += 20.0
    admin_mod._rate_limit(req, route="t", limit=2, window_s=10.0)
