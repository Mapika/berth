from __future__ import annotations

import logging as _audit_logging
import sqlite3
import time as _rl_time
from collections import OrderedDict, deque
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from berth.auth.middleware import require_auth_dep
from berth.backends.base import Backend
from berth.lifecycle.manager import LifecycleManager
from berth.observability import gpu_stats as _gpu_stats
from berth.store import api_keys as _ak_store
from berth.store import node_gpus as node_gpus_store
from berth.store import nodes as nodes_store

_read_gpu_stats = _gpu_stats.read_gpu_stats


def _is_uds_request(request: Request) -> bool:
    """True when the request arrived over the Unix domain socket, not TCP.

    Uvicorn's UDS server reports scope['client'] as None (no remote address)
    whereas TCP delivers a (host, port) tuple. We use 'client' rather than
    'server' because uvicorn fills 'server' with the listening address even
    on UDS (e.g. ('', 0)).
    """
    client = request.scope.get("client")
    return client is None


def _is_stream_ticket_request(request: Request) -> bool:
    if request.method != "GET":
        return False
    path = request.url.path
    return path in ("/admin/events", "/admin/requests/stream") or (
        path.startswith("/admin/deployments/")
        and path.endswith("/logs/stream")
    )


def require_admin_key(
    request: Request,
) -> _ak_store.ApiKey | None:
    """Authorize /admin/*.

    Trust model:
    - Local UDS requests bypass auth entirely. The user controls the host
      filesystem; presence on the socket is sufficient. This is also the
      bootstrap path: `berth key create web --tier admin` over UDS works
      even after other tier=admin keys exist.
    - TCP requests fall through to require_auth_dep. If no keys exist at
      all, that dep also bypasses (homelab UX). Otherwise it requires a
      valid Bearer; we then further require tier=admin here.
    """
    if _is_uds_request(request):
        return None
    stream_token = request.query_params.get("stream_token")
    if stream_token and _is_stream_ticket_request(request):
        store = getattr(request.app.state, "stream_tokens", None)
        if store is not None and store.validate(stream_token):
            return None
    key = require_auth_dep(request)
    if key is None:
        return None
    if key.tier != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="admin tier required for /admin/*",
        )
    return key


router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_key)])


def render_metrics_snapshot(aggregator, *, nodes) -> dict:
    """Pure assembler for the /admin/metrics/snapshot response.

    `aggregator` is a MetricsAggregator; `nodes` is an iterable of objects
    with `.id` and `.label`. Kept module-level + pure so tests don't have
    to stand up FastAPI/auth to validate output shape.
    """
    labels = {n.id: n.label for n in nodes}
    out: list[dict] = []
    for node_id, latest in sorted(aggregator.snapshot().items()):
        label = labels.get(node_id, str(node_id))
        series_gpu_util: dict[str, list[int]] = {}
        for g in latest.get("gpus", []):
            idx = g.get("index", -1)
            series_gpu_util[f"gpu{idx}"] = aggregator.series(
                node_id=node_id, key="gpu_util_pct", gpu=idx,
            )
        out.append({
            "node_id": node_id,
            "label": label,
            "gpus": latest.get("gpus", []),
            "deployments": latest.get("deployments", []),
            "series": {
                "gpu_util_pct": series_gpu_util,
                "request_rate": aggregator.series(
                    node_id=node_id, key="request_rate",
                ),
            },
        })
    return {"nodes": out}


def get_manager(request: Request) -> LifecycleManager:
    return request.app.state.manager


def get_conn(request: Request) -> sqlite3.Connection:
    return request.app.state.conn


def get_backends(request: Request) -> dict[str, Backend]:
    return request.app.state.backends


# ---------------------------------------------------------------------------
# Cert exchange — POST /admin/nodes/register
#
# This is the bootstrap path for a new agent: the enrollment token IS the
# auth, so the endpoint cannot live under the admin router (which requires
# an admin Bearer key once any key has been registered). The agent will be
# unable to present an admin key until *after* this endpoint hands it a
# durable client certificate.
# ---------------------------------------------------------------------------

unauthed_router = APIRouter(prefix="/admin")


# ---------------------------------------------------------------------------
# Per-IP fixed-window rate limit for unauthenticated cluster endpoints.
# Process-local; resets on daemon restart. Good enough as a brute-force
# brake — real DDoS protection is the firewall/CDN's job.
# ---------------------------------------------------------------------------

_audit_log = _audit_logging.getLogger("berth.audit")
# Bounded LRU cap on the rate-limit map. Without this, every distinct
# probe IP permanently consumes a key — straightforward unbounded memory
# growth from random-IP scanners. 10 000 entries is comfortably more than
# any legitimate operator surface needs and tiny in absolute terms.
_RL_MAX_BUCKETS = 10_000

_rl_buckets: OrderedDict[str, deque[float]] = OrderedDict()


def _client_ip(request: Request) -> str:
    """Return the request's apparent client IP.

    When the daemon trusts proxy headers (reverse-proxy mode set in
    config), the rightmost untrusted IP in X-Forwarded-For wins —
    otherwise every request looks like the reverse proxy's loopback
    address and the rate limiter collapses to one global bucket.
    """
    client = request.scope.get("client")
    if client is None:
        return "uds"
    trust = bool(getattr(request.app.state, "trust_proxy_headers", False))
    if trust:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # rightmost untrusted — for our use case the immediate
            # client is the value we want for per-IP bucketing.
            return xff.split(",")[-1].strip() or (
                client[0] if isinstance(client, tuple) else str(client)
            )
    return client[0] if isinstance(client, tuple) else str(client)


def _rate_limit(
    request: Request, *, route: str, limit: int, window_s: float = 60.0,
) -> None:
    """Raise 429 if `_client_ip` has exceeded `limit` calls to `route`
    within the last `window_s` seconds. UDS callers are exempt.

    The bucket map is bounded by `_RL_MAX_BUCKETS` with LRU eviction so
    a scanner cycling through addresses can't blow up daemon memory.
    """
    ip = _client_ip(request)
    if ip == "uds":
        return
    key = f"{route}|{ip}"
    bucket = _rl_buckets.get(key)
    if bucket is None:
        bucket = deque()
        _rl_buckets[key] = bucket
    else:
        _rl_buckets.move_to_end(key)
    now = _rl_time.monotonic()
    cutoff = now - window_s
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
            headers={"Retry-After": str(int(window_s))},
        )
    bucket.append(now)
    # LRU eviction — the oldest-untouched bucket falls off the back.
    while len(_rl_buckets) > _RL_MAX_BUCKETS:
        _rl_buckets.popitem(last=False)


# ---------------------------------------------------------------------------
# Cluster-only routes: hosted on the cluster_app listener (and uds_app),
# never on the public listener. Includes the unauthenticated CA endpoint
# and the rate-limited enrollment registration.
# ---------------------------------------------------------------------------

cluster_router = APIRouter(prefix="/admin")


@cluster_router.get("/ca.pem")
def admin_ca_pem(request: Request):
    """Serve the cluster CA cert. Pinned by SHA-256 fingerprint in the
    enrollment URI, so this endpoint is intentionally unauthenticated."""
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(
        request.app.state.ca_cert_pem,
        headers={"X-Serve-CA-Fingerprint": request.app.state.ca_fingerprint},
        media_type="application/x-pem-file",
    )


class RegisterBody(BaseModel):
    token: str
    host_info: dict


@unauthed_router.post("/nodes/register")
def admin_nodes_register(body: RegisterBody, request: Request):
    import time as _time

    from berth.cluster.ca import fingerprint_sha256, issue_agent_cert

    _rate_limit(request, route="register", limit=10, window_s=60.0)

    tokens = request.app.state.enrollment_tokens
    ip = _client_ip(request)
    token_prefix = body.token[:8] if body.token else ""
    label = tokens.consume(body.token)
    if label is None:
        _audit_log.warning(
            "agent_register reject ip=%s token_prefix=%s reason=invalid_or_expired",
            ip, token_prefix,
        )
        raise HTTPException(403, "invalid or expired enrollment token")
    _audit_log.info(
        "agent_register accept ip=%s token_prefix=%s label=%s",
        ip, token_prefix, label,
    )

    ca = request.app.state.ca
    bundle = issue_agent_cert(ca, label=label)
    fp = fingerprint_sha256(bundle.cert_pem)
    now = _time.time()
    conn: sqlite3.Connection = request.app.state.conn
    info = body.host_info

    existing = nodes_store.find_by_label(conn, label)
    if existing is not None:
        node_id = existing.id
        nodes_store.update_inventory(
            conn, node_id,
            agent_version=info.get("agent_version", "unknown"),
            cpu_count=int(info.get("cpu_count", 0)),
            total_ram_mb=int(info.get("total_ram_mb", 0)),
            gpu_count=int(info.get("gpu_count", 0)),
            total_vram_mb=int(info.get("total_vram_mb", 0)),
        )
        conn.execute(
            "UPDATE nodes SET fingerprint = ? WHERE id = ?",
            (fp, node_id),
        )
    else:
        node_id = nodes_store.insert(
            conn,
            label=label, fingerprint=fp,
            reachable_as=None,
            first_seen=now, last_seen=now,
            agent_version=info.get("agent_version", "unknown"),
            cpu_count=int(info.get("cpu_count", 0)),
            total_ram_mb=int(info.get("total_ram_mb", 0)),
            gpu_count=int(info.get("gpu_count", 0)),
            total_vram_mb=int(info.get("total_vram_mb", 0)),
        )
    node_gpus_store.delete_for_node(conn, node_id)
    for g in info.get("gpus", []):
        node_gpus_store.upsert(
            conn,
            node_id=node_id, gpu_index=int(g["index"]),
            name=str(g["name"]),
            total_vram_mb=int(g["total_vram_mb"]),
            driver_version=g.get("driver_version"),
        )
    return {
        "node_id": node_id,
        "agent_cert": bundle.cert_pem.decode("ascii"),
        "agent_key": bundle.key_pem.decode("ascii"),
        "ca_cert": request.app.state.ca_cert_pem,
    }


from berth.daemon import admin_adapters as _admin_adapters  # noqa: E402,F401
from berth.daemon import admin_cluster as _admin_cluster  # noqa: E402,F401
from berth.daemon import admin_keys as _admin_keys  # noqa: E402,F401
from berth.daemon import admin_runtime as _admin_runtime  # noqa: E402
from berth.daemon import admin_workloads as _admin_workloads  # noqa: E402,F401

_admin_runtime_any = cast(Any, _admin_runtime)
events = _admin_runtime_any.events
stream_engine_logs_sse = _admin_runtime_any.stream_engine_logs_sse
