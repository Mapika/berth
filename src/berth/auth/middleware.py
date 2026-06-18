from __future__ import annotations

import sqlite3
from dataclasses import replace

from fastapi import HTTPException, Request, status

from berth.auth import limiter
from berth.auth.tiers import Limits
from berth.store import api_keys, db, key_usage


def _is_local_control_request(request: Request) -> bool:
    # Trust the explicit per-app flag only (set True solely on the UDS app).
    # Inferring locality from ``scope['client'] is None`` is a fragile,
    # silently-failing heuristic that could disable auth on a TCP listener;
    # the explicit flag fails closed. See _is_uds_request in daemon/admin.py.
    return bool(getattr(request.app.state, "local_control_surface", False))


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def require_auth_dep(request: Request) -> api_keys.ApiKey | None:
    """FastAPI dependency. Returns the ApiKey on success, raises 401/429 on failure.

    Auth source: `Authorization: Bearer sk-...` header.

    Bootstrap exemption: when no keys exist in the table, auth is
    bypassed only for the local control surface. TCP listeners require a
    bearer even before the first key exists, so an explicitly exposed
    daemon never starts as an open inference/admin endpoint.
    """
    conn: sqlite3.Connection = request.app.state.conn
    if api_keys.count_active(conn) == 0 and _is_local_control_request(request):
        return None

    auth_header = request.headers.get("authorization")
    secret = _extract_bearer(auth_header)
    if not secret:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header (expected: Bearer sk-...)",
            headers={"WWW-Authenticate": 'Bearer realm="berth"'},
        )

    key = api_keys.verify(conn, secret)
    if key is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="invalid or revoked API key",
        )

    tier_cfg: dict[str, Limits] = request.app.state.tier_cfg
    usage_event_id: int | None = None
    # Rate-limit semantics: request-per-window limits (rpm/rpd) are enforced
    # hard at admission. Token-per-window limits (tpm/tpd) are necessarily
    # *advisory / post-hoc*: a request's own token cost is unknown at admission
    # and is backfilled later via key_usage.set_tokens, so the check evaluates
    # only previously-completed requests. A single request can therefore exceed
    # a token budget; the budget reasserts on the next request. Only /v1/* calls
    # record a usage event, so other authenticated routes are intentionally
    # unmetered (admin/control traffic is not billed against tenant quotas).
    with db.locked(conn):
        decision = limiter.check(conn, key=key, tier_cfg=tier_cfg)
        if isinstance(decision, limiter.Denied):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"{decision.limit_name} limit reached "
                    f"({decision.current}/{decision.limit_value} in {decision.window_s}s)"
                ),
                headers={"Retry-After": str(decision.retry_after_s)},
            )
        if request.url.path.startswith("/v1/"):
            usage_event_id = key_usage.record(
                conn, key_id=key.id, tokens_in=0, tokens_out=0,
            )
    return replace(key, usage_event_id=usage_event_id)


def require_metrics_key(request: Request) -> api_keys.ApiKey | None:
    """Bearer-auth for /metrics on the public listener — admin tier only.

    /metrics exposes the deployment inventory, scraped engine metrics, active
    key counts, and per-node cluster topology/labels. That is operational
    detail useful for reconnaissance, so it is restricted to admin-tier keys
    rather than any valid tenant key — a low-tier tenant must not be able to
    map internal deployments/nodes. Point Prometheus (etc.) at an admin key.
    UDS callers bypass (so local control commands over the local socket still
    work).
    """
    if _is_local_control_request(request):
        return None  # UDS — operator surface
    conn: sqlite3.Connection = request.app.state.conn
    secret = _extract_bearer(request.headers.get("authorization"))
    if not secret:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header (expected: Bearer sk-...)",
            headers={"WWW-Authenticate": 'Bearer realm="berth"'},
        )
    key = api_keys.verify(conn, secret)
    if key is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="invalid or revoked API key",
        )
    if key.tier != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="admin tier required for /metrics",
        )
    return key
