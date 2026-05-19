from __future__ import annotations

import sqlite3
from dataclasses import replace

from fastapi import HTTPException, Request, status

from berth.auth import limiter
from berth.auth.tiers import Limits
from berth.store import api_keys, db, key_usage


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
    bypassed for any caller (including TCP). This is the operator
    bring-up window. The deploy bootstrap mints the first admin key
    automatically so the window is closed before the public listener
    sees external traffic; a startup warning fires for operators who
    bring the daemon up by hand.
    """
    conn: sqlite3.Connection = request.app.state.conn
    if api_keys.count_active(conn) == 0:
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
    """Light-weight bearer-auth for /metrics on the public listener.

    Any non-revoked key (no tier requirement) is accepted — the only goal
    is to keep the deployment inventory / engine URLs / key counts off
    public scrapers. UDS callers bypass (so `serve metrics` over the
    local socket still works).
    """
    if request.scope.get("client") is None:
        return None  # UDS — operator surface
    conn: sqlite3.Connection = request.app.state.conn
    if api_keys.count_active(conn) == 0:
        return None  # bootstrap window — operator hasn't minted a key yet
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
    return key
