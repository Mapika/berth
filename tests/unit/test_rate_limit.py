from __future__ import annotations

import pytest
from fastapi import HTTPException, Request

from serve_engine.daemon import admin as admin_mod


def _make_request(
    client_ip: str | None,
    *,
    xff: str | None = None,
    trust_proxy_headers: bool = False,
) -> Request:
    """Build a minimal ASGI scope so `_client_ip` and `_rate_limit`
    can run without spinning up a real server."""
    raw_headers: list[tuple[bytes, bytes]] = []
    if xff:
        raw_headers.append((b"x-forwarded-for", xff.encode()))
    # Synthesize an app.state object the limiter reads for the trust flag.
    from types import SimpleNamespace
    app = SimpleNamespace(state=SimpleNamespace(
        trust_proxy_headers=trust_proxy_headers,
    ))
    scope = {
        "type": "http",
        "client": (client_ip, 4242) if client_ip else None,
        "headers": raw_headers,
        "app": app,
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


def test_client_ip_uses_xff_when_proxy_trusted():
    """With trust_proxy_headers on, the limiter buckets by the rightmost
    XFF hop, not the TCP peer (which would be the reverse proxy)."""
    req = _make_request(
        "127.0.0.1", xff="203.0.113.5", trust_proxy_headers=True,
    )
    assert admin_mod._client_ip(req) == "203.0.113.5"


def test_client_ip_ignores_xff_when_proxy_untrusted():
    req = _make_request("127.0.0.1", xff="203.0.113.5")
    assert admin_mod._client_ip(req) == "127.0.0.1"


def test_rate_limit_buckets_distinct_xff_clients_independently():
    """Two clients behind the same proxy must get independent buckets
    when proxy_headers are trusted."""
    admin_mod._rl_buckets.clear()
    a = _make_request("127.0.0.1", xff="1.1.1.1", trust_proxy_headers=True)
    b = _make_request("127.0.0.1", xff="2.2.2.2", trust_proxy_headers=True)
    for _ in range(2):
        admin_mod._rate_limit(a, route="t", limit=2, window_s=60.0)
    # b is untouched
    admin_mod._rate_limit(b, route="t", limit=2, window_s=60.0)
    admin_mod._rate_limit(b, route="t", limit=2, window_s=60.0)
    with pytest.raises(HTTPException):
        admin_mod._rate_limit(b, route="t", limit=2, window_s=60.0)


def test_rate_limit_is_bounded_by_lru_capacity(monkeypatch):
    """Distinct IPs that hit the limiter once must not leak keys forever.
    With the LRU cap, the map size never exceeds _RL_MAX_BUCKETS."""
    admin_mod._rl_buckets.clear()
    # Shrink the cap so the test stays fast.
    monkeypatch.setattr(admin_mod, "_RL_MAX_BUCKETS", 50)
    for i in range(200):
        admin_mod._rate_limit(
            _make_request(f"10.0.0.{i}"),
            route="t", limit=10, window_s=60.0,
        )
    assert len(admin_mod._rl_buckets) <= 50
    # The most recent IPs are retained; the earliest evicted.
    assert "t|10.0.0.199" in admin_mod._rl_buckets
    assert "t|10.0.0.0" not in admin_mod._rl_buckets
