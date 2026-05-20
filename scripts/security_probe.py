#!/usr/bin/env python3
"""Black-box security probe for a deployed berth instance.

The checks intentionally exercise only HTTP-visible boundaries. They are
designed for a staging or production URL after the daemon/reverse proxy is
running, complementing unit tests and static scanners.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from typing import Any

import httpx

HeaderExpectation = str | tuple[str, ...]


@dataclass(frozen=True)
class CheckSpec:
    name: str
    method: str
    path: str
    expected: frozenset[int]
    json_body: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    expected_headers: dict[str, HeaderExpectation] | None = None


@dataclass(frozen=True)
class CheckResult:
    target: str
    name: str
    method: str
    path: str
    expected: tuple[int, ...]
    ok: bool
    actual_status: int | None
    error: str | None = None


PUBLIC_CHECKS = [
    CheckSpec("public healthz reachable", "GET", "/healthz", frozenset({200})),
    CheckSpec(
        "public browser security headers set",
        "GET",
        "/healthz",
        frozenset({200}),
        expected_headers={
            "content-security-policy": (
                "default-src 'self'",
                "script-src 'self'",
                "connect-src 'self'",
                "frame-ancestors 'none'",
            ),
            "permissions-policy": "camera=()",
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
        },
    ),
    CheckSpec("public readyz bounded", "GET", "/readyz", frozenset({200, 503})),
    CheckSpec("public generated OpenAPI hidden", "GET", "/openapi.json", frozenset({404})),
    CheckSpec("public generated docs hidden", "GET", "/docs", frozenset({404})),
    CheckSpec("public generated redoc hidden", "GET", "/redoc", frozenset({404})),
    CheckSpec("public metrics require auth", "GET", "/metrics", frozenset({401})),
    CheckSpec(
        "public metrics no-store",
        "GET",
        "/metrics",
        frozenset({401}),
        expected_headers={"cache-control": "no-store", "pragma": "no-cache"},
    ),
    CheckSpec(
        "public admin requires auth",
        "GET",
        "/admin/keys",
        frozenset({401}),
        expected_headers={"cache-control": "no-store", "pragma": "no-cache"},
    ),
    CheckSpec(
        "public chat requires auth",
        "POST",
        "/v1/chat/completions",
        frozenset({401}),
        json_body={"model": "probe", "messages": []},
        expected_headers={"cache-control": "no-store", "pragma": "no-cache"},
    ),
    CheckSpec("public cluster CA not mounted", "GET", "/admin/ca.pem", frozenset({404})),
    CheckSpec(
        "public agent registration not callable",
        "POST",
        "/admin/nodes/register",
        frozenset({404, 405}),
        json_body={"token": "invalid", "host_info": {}},  # nosec
    ),
]

CLUSTER_CHECKS = [
    CheckSpec("cluster healthz reachable", "GET", "/healthz", frozenset({200})),
    CheckSpec(
        "cluster browser security headers set",
        "GET",
        "/healthz",
        frozenset({200}),
        expected_headers={
            "content-security-policy": (
                "default-src 'self'",
                "script-src 'self'",
                "connect-src 'self'",
                "frame-ancestors 'none'",
            ),
            "permissions-policy": "camera=()",
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
        },
    ),
    CheckSpec("cluster readyz bounded", "GET", "/readyz", frozenset({200, 503})),
    CheckSpec("cluster generated OpenAPI hidden", "GET", "/openapi.json", frozenset({404})),
    CheckSpec("cluster generated docs hidden", "GET", "/docs", frozenset({404})),
    CheckSpec("cluster generated redoc hidden", "GET", "/redoc", frozenset({404})),
    CheckSpec(
        "cluster CA exposed for pinned enrollment",
        "GET",
        "/admin/ca.pem",
        frozenset({200}),
        expected_headers={"cache-control": "no-store", "pragma": "no-cache"},
    ),
    CheckSpec(
        "cluster rejects invalid enrollment token",
        "POST",
        "/admin/nodes/register",
        frozenset({403}),
        json_body={"token": "invalid", "host_info": {}},  # nosec
        expected_headers={"cache-control": "no-store", "pragma": "no-cache"},
    ),
    CheckSpec(
        "cluster public chat not mounted",
        "POST",
        "/v1/chat/completions",
        frozenset({404}),
        json_body={"model": "probe", "messages": []},
    ),
    CheckSpec("cluster metrics not mounted", "GET", "/metrics", frozenset({404})),
    CheckSpec("cluster admin keys not mounted", "GET", "/admin/keys", frozenset({404})),
]


def _with_bearer(spec: CheckSpec, bearer_token: str) -> CheckSpec:
    headers = dict(spec.headers or {})
    headers["Authorization"] = f"Bearer {bearer_token}"
    return CheckSpec(
        name=spec.name,
        method=spec.method,
        path=spec.path,
        expected=spec.expected,
        json_body=spec.json_body,
        headers=headers,
        expected_headers=spec.expected_headers,
    )


def _public_checks(bearer_token: str | None) -> list[CheckSpec]:
    checks = list(PUBLIC_CHECKS)
    if bearer_token:
        checks.append(
            _with_bearer(
                CheckSpec(
                    "public authenticated /v1/models works",
                    "GET",
                    "/v1/models",
                    frozenset({200}),
                    expected_headers={
                        "cache-control": "no-store",
                        "pragma": "no-cache",
                    },
                ),
                bearer_token,
            )
        )
        checks.append(
            _with_bearer(
                CheckSpec(
                    "public stream token rejects non-stream path",
                    "POST",
                    "/admin/stream-token",
                    frozenset({400}),
                    json_body={"path": "/admin/keys"},
                ),
                bearer_token,
            )
        )
    return checks


def _request_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _run_checks(
    client: httpx.Client,
    *,
    target: str,
    base_url: str,
    checks: list[CheckSpec],
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in checks:
        try:
            response = client.request(
                check.method,
                _request_url(base_url, check.path),
                headers=check.headers,
                json=check.json_body,
            )
        except httpx.HTTPError as e:
            results.append(
                CheckResult(
                    target=target,
                    name=check.name,
                    method=check.method,
                    path=check.path,
                    expected=tuple(sorted(check.expected)),
                    ok=False,
                    actual_status=None,
                    error=str(e),
                )
            )
            continue
        ok = response.status_code in check.expected
        error = None
        for header_name, expected_value in (check.expected_headers or {}).items():
            actual_value = response.headers.get(header_name)
            if actual_value is None:
                ok = False
                error = f"missing response header {header_name}"
                break
            expected_values = (
                (expected_value,) if isinstance(expected_value, str) else expected_value
            )
            missing_values = [
                value for value in expected_values if value not in actual_value
            ]
            if missing_values:
                ok = False
                missing = ", ".join(repr(value) for value in missing_values)
                error = (
                    f"response header {header_name}={actual_value!r} "
                    f"does not contain {missing}"
                )
                break
        results.append(
            CheckResult(
                target=target,
                name=check.name,
                method=check.method,
                path=check.path,
                expected=tuple(sorted(check.expected)),
                ok=ok,
                actual_status=response.status_code,
                error=error,
            )
        )
    return results


def _run_stream_token_scope_checks(
    client: httpx.Client,
    *,
    base_url: str,
    bearer_token: str,
) -> list[CheckResult]:
    auth = {"Authorization": f"Bearer {bearer_token}"}
    try:
        minted = client.post(
            _request_url(base_url, "/admin/stream-token"),
            headers=auth,
            json={"path": "/admin/events"},
        )
    except httpx.HTTPError as e:
        return [
            CheckResult(
                target="public",
                name="public stream token can be minted for stream path",
                method="POST",
                path="/admin/stream-token",
                expected=(200,),
                ok=False,
                actual_status=None,
                error=str(e),
            )
        ]

    mint_result = CheckResult(
        target="public",
        name="public stream token can be minted for stream path",
        method="POST",
        path="/admin/stream-token",
        expected=(200,),
        ok=minted.status_code == 200,
        actual_status=minted.status_code,
    )
    if not mint_result.ok:
        return [mint_result]

    try:
        token = str(minted.json()["token"])
    except (KeyError, TypeError, ValueError) as e:
        return [
            CheckResult(
                target="public",
                name="public stream token can be minted for stream path",
                method="POST",
                path="/admin/stream-token",
                expected=(200,),
                ok=False,
                actual_status=minted.status_code,
                error=f"missing token in response: {e}",
            )
        ]

    try:
        with client.stream(
            "GET",
            _request_url(
                base_url,
                f"/admin/requests/stream?stream_token={token}",
            ),
        ) as response:
            actual = response.status_code
    except httpx.HTTPError as e:
        return [
            mint_result,
            CheckResult(
                target="public",
                name="public stream token is path-bound",
                method="GET",
                path="/admin/requests/stream",
                expected=(401,),
                ok=False,
                actual_status=None,
                error=str(e),
            ),
        ]

    return [
        mint_result,
        CheckResult(
            target="public",
            name="public stream token is path-bound",
            method="GET",
            path="/admin/requests/stream",
            expected=(401,),
            ok=actual == 401,
            actual_status=actual,
        ),
    ]


def run_probe(
    *,
    public_url: str,
    cluster_url: str | None,
    bearer_token: str | None,
    transport: httpx.BaseTransport | None = None,
    verify: bool = True,
    timeout_s: float = 10.0,
) -> list[CheckResult]:
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout_s),
        "verify": verify,
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    with httpx.Client(**client_kwargs) as client:
        results = _run_checks(
            client,
            target="public",
            base_url=public_url,
            checks=_public_checks(bearer_token),
        )
        if bearer_token:
            results.extend(
                _run_stream_token_scope_checks(
                    client,
                    base_url=public_url,
                    bearer_token=bearer_token,
                )
            )
        if cluster_url:
            results.extend(
                _run_checks(
                    client,
                    target="cluster",
                    base_url=cluster_url,
                    checks=CLUSTER_CHECKS,
                )
            )
    return results


def _print_text(results: list[CheckResult]) -> None:
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        actual = "error" if result.actual_status is None else str(result.actual_status)
        expected = ",".join(str(s) for s in result.expected)
        suffix = f" error={result.error}" if result.error else ""
        print(
            f"[{status}] {result.target}: {result.name} "
            f"({result.method} {result.path}) actual={actual} expected={expected}{suffix}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe berth public/cluster listener security boundaries.",
    )
    parser.add_argument("--public-url", required=True, help="Public listener base URL")
    parser.add_argument("--cluster-url", help="Cluster listener base URL")
    parser.add_argument(
        "--token",
        help="Optional bearer token for positive authenticated public checks",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification for generated/self-signed staging certs",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout")
    parser.add_argument("--json", action="store_true", help="Emit JSON results")
    args = parser.parse_args(argv)

    results = run_probe(
        public_url=args.public_url,
        cluster_url=args.cluster_url,
        bearer_token=args.token,
        verify=not args.insecure,
        timeout_s=args.timeout,
    )
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        _print_text(results)
    return 1 if any(not r.ok for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
