from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from berth.backends.vllm import VLLMBackend
from berth.daemon.app import build_apps
from berth.store import db


def _apps(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return build_apps(
        conn=conn,
        docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        berth_home=tmp_path,
    )


def _paths(app) -> set[str]:
    return {route.path for route in app.routes}


def test_public_listener_does_not_mount_cluster_routes(tmp_path):
    public_app, _cluster_app, _uds_app = _apps(tmp_path)
    paths = _paths(public_app)

    assert "/v1/models" in paths
    assert "/metrics" in paths
    assert "/admin/keys" in paths
    assert "/" in paths
    assert "/cluster/agent" not in paths
    assert "/admin/nodes/register" not in paths
    assert "/admin/ca.pem" not in paths


def test_cluster_listener_only_mounts_agent_bootstrap_routes(tmp_path):
    _public_app, cluster_app, _uds_app = _apps(tmp_path)
    paths = _paths(cluster_app)

    assert "/admin/nodes/register" in paths
    assert "/admin/ca.pem" in paths
    assert "/cluster/agent" in paths
    assert "/v1/models" not in paths
    assert "/metrics" not in paths
    assert "/admin/keys" not in paths
    assert "/" not in paths
    assert "/assets" not in paths


@pytest.mark.asyncio
async def test_tcp_listeners_do_not_publish_openapi_or_docs(tmp_path):
    public_app, cluster_app, _uds_app = _apps(tmp_path)

    for app in (public_app, cluster_app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as c:
            for path in ("/openapi.json", "/docs", "/redoc"):
                r = await c.get(path)
                assert r.status_code == 404


@pytest.mark.asyncio
async def test_tcp_listeners_set_browser_security_headers(tmp_path):
    public_app, cluster_app, _uds_app = _apps(tmp_path)

    for app in (public_app, cluster_app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as c:
            r = await c.get("/healthz")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com" in csp
        assert "font-src 'self' https://fonts.gstatic.com" in csp
        assert "img-src 'self' data:" in csp
        assert "frame-ancestors 'none'" in csp
        assert "camera=()" in r.headers["permissions-policy"]


@pytest.mark.asyncio
async def test_tcp_listeners_do_not_cache_sensitive_routes(tmp_path):
    public_app, cluster_app, _uds_app = _apps(tmp_path)

    public_transport = httpx.ASGITransport(app=public_app)
    async with httpx.AsyncClient(
        transport=public_transport,
        base_url="http://test",
    ) as c:
        for path in ("/admin/keys", "/v1/models", "/metrics"):
            r = await c.get(path)
            assert r.headers["cache-control"] == "no-store"
            assert r.headers["pragma"] == "no-cache"
        r = await c.get("/healthz")
        assert r.headers.get("cache-control") != "no-store"

    cluster_transport = httpx.ASGITransport(app=cluster_app)
    async with httpx.AsyncClient(
        transport=cluster_transport,
        base_url="http://test",
    ) as c:
        r = await c.get("/admin/ca.pem")
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["pragma"] == "no-cache"
        r = await c.get("/healthz")
        assert r.headers.get("cache-control") != "no-store"
