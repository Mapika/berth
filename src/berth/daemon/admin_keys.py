from __future__ import annotations

import sqlite3

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel

from berth.daemon.admin import get_conn, router
from berth.store import api_keys as _ak_store
from berth.store import key_usage as _key_usage_store


class CreateKeyRequest(BaseModel):
    name: str
    tier: str = "standard"
    rpm_override: int | None = None
    tpm_override: int | None = None
    rph_override: int | None = None
    tph_override: int | None = None
    rpd_override: int | None = None
    tpd_override: int | None = None
    rpw_override: int | None = None
    tpw_override: int | None = None
    allowed_models: list[str] | None = None


class UpdateKeyRequest(BaseModel):
    allowed_models: list[str] | None = None


@router.post("/stream-token")
def create_stream_token(request: Request):
    from berth.daemon.admin import _rate_limit

    _rate_limit(request, route="stream-token", limit=60, window_s=60.0)
    token, expires_at = request.app.state.stream_tokens.issue()
    return {"token": token, "expires_at": expires_at}


@router.get("/keys")
def list_keys(conn: sqlite3.Connection = Depends(get_conn)):
    return [
        {
            "id": k.id,
            "name": k.name,
            "prefix": k.prefix,
            "tier": k.tier,
            "revoked": k.revoked_at is not None,
            "allowed_models": k.allowed_models,
        }
        for k in _ak_store.list_all(conn)
    ]


@router.post("/keys", status_code=status.HTTP_201_CREATED)
def create_key(
    body: CreateKeyRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    secret, key = _ak_store.create(
        conn, name=body.name, tier=body.tier,
        rpm_override=body.rpm_override, tpm_override=body.tpm_override,
        rph_override=body.rph_override, tph_override=body.tph_override,
        rpd_override=body.rpd_override, tpd_override=body.tpd_override,
        rpw_override=body.rpw_override, tpw_override=body.tpw_override,
        allowed_models=body.allowed_models,
    )
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        "tier": key.tier,
        "secret": secret,
    }


@router.patch("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def update_key(
    key_id: int,
    body: UpdateKeyRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    if _ak_store.get_by_id(conn, key_id) is None:
        raise HTTPException(404, f"no key with id {key_id}")
    _ak_store.set_allowed_models(conn, key_id, body.allowed_models)


@router.get("/keys/{key_id}/usage")
def key_usage(
    key_id: int,
    window_s: int = 86400,
    bucket_s: int = 3600,
    conn: sqlite3.Connection = Depends(get_conn),
):
    if _ak_store.get_by_id(conn, key_id) is None:
        raise HTTPException(404, f"api key {key_id} not found")
    if window_s <= 0 or bucket_s <= 0:
        raise HTTPException(400, "window_s and bucket_s must be positive")
    if window_s // bucket_s > 1024:
        raise HTTPException(400, "too many buckets requested (cap is 1024)")
    return {
        "key_id": key_id,
        "window_s": window_s,
        "bucket_s": bucket_s,
        "buckets": _key_usage_store.bucketed_usage(
            conn, key_id=key_id, window_s=window_s, bucket_s=bucket_s,
        ),
    }


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_key(
    key_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    if _ak_store.get_by_id(conn, key_id) is None:
        raise HTTPException(404, f"no key with id {key_id}")
    _ak_store.revoke(conn, key_id)
