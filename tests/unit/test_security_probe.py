from __future__ import annotations

import json

import httpx

from scripts import security_probe

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "object-src 'none'"
    ),
    "Permissions-Policy": "camera=()",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _handler(
    statuses: dict[tuple[str, str], int],
    *,
    include_security_headers: bool = True,
):
    def handle(request: httpx.Request) -> httpx.Response:
        key = (request.url.host or "", request.url.path)
        status = statuses.get(key, 404)
        headers = _SECURITY_HEADERS if include_security_headers else None
        return httpx.Response(status, headers=headers, json={"path": request.url.path})

    return handle


def test_public_probe_flags_exposed_docs():
    transport = httpx.MockTransport(
        _handler({
            ("public.test", "/healthz"): 200,
            ("public.test", "/readyz"): 503,
            ("public.test", "/openapi.json"): 200,
            ("public.test", "/docs"): 404,
            ("public.test", "/redoc"): 404,
            ("public.test", "/metrics"): 401,
            ("public.test", "/admin/keys"): 401,
            ("public.test", "/v1/chat/completions"): 401,
            ("public.test", "/admin/ca.pem"): 404,
            ("public.test", "/admin/nodes/register"): 405,
        })
    )
    results = security_probe.run_probe(
        public_url="https://public.test",
        cluster_url=None,
        bearer_token=None,
        transport=transport,
    )

    failures = [r for r in results if not r.ok]
    assert [f.name for f in failures] == ["public generated OpenAPI hidden"]
    assert failures[0].actual_status == 200


def test_public_probe_flags_missing_security_headers():
    transport = httpx.MockTransport(
        _handler(
            {
                ("public.test", "/healthz"): 200,
                ("public.test", "/readyz"): 503,
                ("public.test", "/openapi.json"): 404,
                ("public.test", "/docs"): 404,
                ("public.test", "/redoc"): 404,
                ("public.test", "/metrics"): 401,
                ("public.test", "/admin/keys"): 401,
                ("public.test", "/v1/chat/completions"): 401,
                ("public.test", "/admin/ca.pem"): 404,
                ("public.test", "/admin/nodes/register"): 405,
            },
            include_security_headers=False,
        )
    )
    results = security_probe.run_probe(
        public_url="https://public.test",
        cluster_url=None,
        bearer_token=None,
        transport=transport,
    )

    failures = [r for r in results if not r.ok]
    assert "public browser security headers set" in [f.name for f in failures]


def test_public_probe_flags_weak_content_security_policy():
    weak_headers = {
        **_SECURITY_HEADERS,
        "Content-Security-Policy": "frame-ancestors 'none'",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        statuses = {
            "/healthz": 200,
            "/readyz": 503,
            "/openapi.json": 404,
            "/docs": 404,
            "/redoc": 404,
            "/metrics": 401,
            "/admin/keys": 401,
            "/v1/chat/completions": 401,
            "/admin/ca.pem": 404,
            "/admin/nodes/register": 405,
        }
        return httpx.Response(
            statuses.get(request.url.path, 404),
            headers=weak_headers,
            json={},
        )

    results = security_probe.run_probe(
        public_url="https://public.test",
        cluster_url=None,
        bearer_token=None,
        transport=httpx.MockTransport(handle),
    )

    failures = [r for r in results if not r.ok]
    assert "public browser security headers set" in [f.name for f in failures]


def test_public_probe_flags_missing_sensitive_cache_headers():
    def handle(request: httpx.Request) -> httpx.Response:
        headers = {
            k: v for k, v in _SECURITY_HEADERS.items()
            if k not in {"Cache-Control", "Pragma"}
        }
        statuses = {
            "/healthz": 200,
            "/readyz": 200,
            "/openapi.json": 404,
            "/docs": 404,
            "/redoc": 404,
            "/metrics": 401,
            "/admin/keys": 401,
            "/v1/chat/completions": 401,
            "/admin/ca.pem": 404,
            "/admin/nodes/register": 405,
        }
        return httpx.Response(
            statuses.get(request.url.path, 404),
            headers=headers,
            json={},
        )

    results = security_probe.run_probe(
        public_url="https://public.test",
        cluster_url=None,
        bearer_token=None,
        transport=httpx.MockTransport(handle),
    )

    failures = [r for r in results if not r.ok]
    assert "public admin requires auth" in [f.name for f in failures]


def test_public_and_cluster_probe_pass_for_expected_locked_down_surface():
    transport = httpx.MockTransport(
        _handler({
            ("public.test", "/healthz"): 200,
            ("public.test", "/readyz"): 200,
            ("public.test", "/openapi.json"): 404,
            ("public.test", "/docs"): 404,
            ("public.test", "/redoc"): 404,
            ("public.test", "/metrics"): 401,
            ("public.test", "/admin/keys"): 401,
            ("public.test", "/v1/chat/completions"): 401,
            ("public.test", "/admin/ca.pem"): 404,
            ("public.test", "/admin/nodes/register"): 405,
            ("cluster.test", "/healthz"): 200,
            ("cluster.test", "/readyz"): 200,
            ("cluster.test", "/openapi.json"): 404,
            ("cluster.test", "/docs"): 404,
            ("cluster.test", "/redoc"): 404,
            ("cluster.test", "/admin/ca.pem"): 200,
            ("cluster.test", "/admin/nodes/register"): 403,
            ("cluster.test", "/v1/chat/completions"): 404,
            ("cluster.test", "/metrics"): 404,
            ("cluster.test", "/admin/keys"): 404,
        })
    )
    results = security_probe.run_probe(
        public_url="https://public.test",
        cluster_url="https://cluster.test",
        bearer_token=None,
        transport=transport,
    )

    assert [r.name for r in results if not r.ok] == []


def test_bearer_token_enables_optional_public_positive_checks():
    statuses = {
        ("public.test", "/healthz"): 200,
        ("public.test", "/readyz"): 200,
        ("public.test", "/openapi.json"): 404,
        ("public.test", "/docs"): 404,
        ("public.test", "/redoc"): 404,
        ("public.test", "/metrics"): 401,
        ("public.test", "/admin/keys"): 401,
        ("public.test", "/v1/chat/completions"): 401,
        ("public.test", "/admin/ca.pem"): 404,
        ("public.test", "/admin/nodes/register"): 405,
        ("public.test", "/v1/models"): 200,
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/stream-token":
            body = json.loads(request.content.decode("utf-8"))
            if body["path"] == "/admin/events":
                return httpx.Response(200, json={"token": "ticket", "expires_at": 1})
            if body["path"] == "/admin/keys":
                return httpx.Response(400, json={"detail": "bad path"})
        if request.url.path == "/admin/requests/stream":
            return httpx.Response(
                401,
                headers=_SECURITY_HEADERS,
                json={"detail": "missing bearer"},
            )
        return httpx.Response(
            statuses.get((request.url.host or "", request.url.path), 404),
            headers=_SECURITY_HEADERS,
            json={},
        )

    transport = httpx.MockTransport(handle)
    results = security_probe.run_probe(
        public_url="https://public.test",
        cluster_url=None,
        bearer_token="sk-test",
        transport=transport,
    )

    names = [r.name for r in results]
    assert "public authenticated /v1/models works" in names
    assert [r.name for r in results if not r.ok] == []


def test_bearer_token_enables_stream_ticket_scope_checks():
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/stream-token":
            body = json.loads(request.content.decode("utf-8"))
            if body["path"] == "/admin/events":
                return httpx.Response(200, json={"token": "ticket", "expires_at": 1})
            if body["path"] == "/admin/keys":
                return httpx.Response(400, json={"detail": "bad path"})
        if request.url.path == "/admin/requests/stream":
            assert request.url.params["stream_token"] == "ticket"
            return httpx.Response(
                401,
                headers=_SECURITY_HEADERS,
                json={"detail": "missing bearer"},
            )
        statuses = {
            "/healthz": 200,
            "/readyz": 200,
            "/openapi.json": 404,
            "/docs": 404,
            "/redoc": 404,
            "/metrics": 401,
            "/admin/keys": 401,
            "/v1/chat/completions": 401,
            "/admin/ca.pem": 404,
            "/admin/nodes/register": 405,
            "/v1/models": 200,
        }
        return httpx.Response(
            statuses.get(request.url.path, 404),
            headers=_SECURITY_HEADERS,
            json={},
        )

    results = security_probe.run_probe(
        public_url="https://public.test",
        cluster_url=None,
        bearer_token="sk-test",
        transport=httpx.MockTransport(handle),
    )

    names = [r.name for r in results]
    assert "public stream token rejects non-stream path" in names
    assert "public stream token is path-bound" in names
    assert [r.name for r in results if not r.ok] == []
