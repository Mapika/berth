from __future__ import annotations

import asyncio
import json as _json
import logging as _audit_logging
import sqlite3
import time as _rl_time
from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from serve_engine.auth.middleware import require_auth_dep
from serve_engine.backends.base import Backend
from serve_engine.lifecycle.manager import LifecycleManager
from serve_engine.lifecycle.plan import DeploymentPlan
from serve_engine.observability.gpu_stats import read_gpu_stats as _read_gpu_stats
from serve_engine.store import adapters as ad_store
from serve_engine.store import api_keys as _ak_store
from serve_engine.store import deployment_adapters as da_store
from serve_engine.store import deployments as dep_store
from serve_engine.store import models as model_store
from serve_engine.store import node_gpus as node_gpus_store
from serve_engine.store import nodes as nodes_store
from serve_engine.store import service_profiles as profile_store
from serve_engine.store import service_routes as route_store


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
      bootstrap path: `serve key create web --tier admin` over UDS works
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


def get_manager(request: Request) -> LifecycleManager:
    return request.app.state.manager


def get_conn(request: Request) -> sqlite3.Connection:
    return request.app.state.conn


def get_backends(request: Request) -> dict[str, Backend]:
    return request.app.state.backends


class CreateDeploymentRequest(BaseModel):
    model_config = {"protected_namespaces": ()}
    model_name: str
    hf_repo: str
    revision: str = "main"
    backend: str | None = None   # default -> selection rules
    image_tag: str | None = None
    gpu_ids: list[int]
    tensor_parallel: int | None = None
    max_model_len: int = 8192
    dtype: str = "auto"
    pinned: bool = False
    idle_timeout_s: int | None = None
    target_concurrency: int | None = None
    max_loras: int = 0
    extra_args: dict[str, str] = {}


class CreateServiceProfileRequest(BaseModel):
    model_config = {"protected_namespaces": ()}
    name: str
    model_name: str
    hf_repo: str
    revision: str = "main"
    backend: str | None = None
    image_tag: str | None = None
    gpu_ids: list[int]
    tensor_parallel: int | None = None
    max_model_len: int = 8192
    dtype: str = "auto"
    pinned: bool = False
    idle_timeout_s: int | None = None
    target_concurrency: int | None = None
    max_loras: int = 0
    extra_args: dict[str, str] = {}


class CreateServiceRouteRequest(BaseModel):
    name: str
    match_model: str
    profile_name: str
    fallback_profile_name: str | None = None
    enabled: bool = True
    priority: int = 100


class CreateModelRequest(BaseModel):
    name: str
    hf_repo: str
    revision: str = "main"


def _max_lora_rank_from_extra(extra_args: dict[str, str]) -> int:
    raw = extra_args.get("--max-lora-rank")
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError as e:
        raise HTTPException(
            400, f"--max-lora-rank must be an integer; got {raw!r}",
        ) from e


def _validate_backend_capabilities(
    *,
    plan: DeploymentPlan,
    backend: Backend,
    backend_name: str,
) -> None:
    if plan.max_loras > 0 and not backend.supports_adapters:
        raise HTTPException(
            400,
            f"backend {backend_name!r} does not support LoRA adapters; "
            f"`max_loras` must be 0 (got {plan.max_loras})",
        )


def _profile_to_plan(profile: profile_store.ServiceProfile) -> DeploymentPlan:
    return DeploymentPlan(
        model_name=profile.model_name,
        hf_repo=profile.hf_repo,
        revision=profile.revision,
        backend=profile.backend,
        image_tag=profile.image_tag,
        gpu_ids=profile.gpu_ids,
        tensor_parallel=profile.tensor_parallel,
        max_model_len=profile.max_model_len,
        dtype=profile.dtype,
        pinned=profile.pinned,
        idle_timeout_s=profile.idle_timeout_s,
        target_concurrency=profile.target_concurrency,
        max_loras=profile.max_loras,
        max_lora_rank=profile.max_lora_rank,
        extra_args=dict(profile.extra_args),
    )


@router.get("/deployments")
def list_deployments(
    conn: sqlite3.Connection = Depends(get_conn),
    manager: LifecycleManager = Depends(get_manager),
):
    # Live per-process VRAM (NVML compute view) joined to each deployment by
    # the union of pids in its docker container. vram_reserved_mb is the
    # estimator's pre-load reservation; vram_used_mb is what nvidia-smi sees.
    from serve_engine.observability.gpu_stats import read_compute_process_vram
    pid_vram = read_compute_process_vram()
    out = []
    for d in dep_store.list_all(conn):
        used_mb: int | None = None
        if pid_vram and d.container_id and d.status in ("loading", "ready"):
            try:
                pids = manager._docker.container_pids(d.container_id)
                total = sum(pid_vram.get(p, 0) for p in pids)
                used_mb = total or None
            except Exception:
                used_mb = None
        out.append({**asdict(d), "gpu_ids": d.gpu_ids, "vram_used_mb": used_mb})
    return out


@router.post("/deployments", status_code=status.HTTP_201_CREATED)
async def create_deployment(
    body: CreateDeploymentRequest,
    manager: LifecycleManager = Depends(get_manager),
    backends: dict[str, Backend] = Depends(get_backends),
):
    from serve_engine.backends.selection import load_selection, pick_backend
    backend_name = body.backend
    if backend_name is None:
        backend_name = pick_backend(load_selection(), body.model_name)
    if backend_name not in backends:
        raise HTTPException(400, f"backend {backend_name!r} not supported")
    backend = backends[backend_name]
    image_tag = body.image_tag or backend.image_default
    tp = body.tensor_parallel or len(body.gpu_ids)
    # Mirror --max-lora-rank from extra_args into a first-class plan field
    # so we can pre-flight adapter rank against it. We don't strip it from
    # extra_args - the backend still uses extra_args to emit the flag to
    # the engine container.
    max_lora_rank = _max_lora_rank_from_extra(body.extra_args)
    try:
        plan = DeploymentPlan(
            model_name=body.model_name,
            hf_repo=body.hf_repo,
            revision=body.revision,
            backend=backend_name,
            image_tag=image_tag,
            gpu_ids=body.gpu_ids,
            tensor_parallel=tp,
            max_model_len=body.max_model_len,
            dtype=body.dtype,
            pinned=body.pinned,
            idle_timeout_s=body.idle_timeout_s,
            target_concurrency=body.target_concurrency,
            max_loras=body.max_loras,
            max_lora_rank=max_lora_rank,
            extra_args=dict(body.extra_args),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # Backend-aware validation: max_loras > 0 requires an adapter-capable backend.
    _validate_backend_capabilities(plan=plan, backend=backend, backend_name=backend_name)
    try:
        dep = await manager.load(plan)
    except RuntimeError as e:
        # manager.load raises RuntimeError for client-actionable load
        # failures: a same-name deployment is pinned, or placement found
        # no room. Surface as 409 so the CLI's IPC layer extracts the
        # message instead of showing a bare 500.
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return {**asdict(dep), "gpu_ids": dep.gpu_ids}


@router.get("/service-profiles")
def list_service_profiles(conn: sqlite3.Connection = Depends(get_conn)):
    return [asdict(p) for p in profile_store.list_all(conn)]


@router.post("/service-profiles", status_code=status.HTTP_201_CREATED)
def create_service_profile(
    body: CreateServiceProfileRequest,
    backends: dict[str, Backend] = Depends(get_backends),
    conn: sqlite3.Connection = Depends(get_conn),
):
    from serve_engine.backends.selection import load_selection, pick_backend
    backend_name = body.backend or pick_backend(load_selection(), body.model_name)
    if backend_name not in backends:
        raise HTTPException(400, f"backend {backend_name!r} not supported")
    backend = backends[backend_name]
    image_tag = body.image_tag or backend.image_default
    tp = body.tensor_parallel or len(body.gpu_ids)
    max_lora_rank = _max_lora_rank_from_extra(body.extra_args)
    try:
        plan = DeploymentPlan(
            model_name=body.model_name,
            hf_repo=body.hf_repo,
            revision=body.revision,
            backend=backend_name,
            image_tag=image_tag,
            gpu_ids=body.gpu_ids,
            tensor_parallel=tp,
            max_model_len=body.max_model_len,
            dtype=body.dtype,
            pinned=body.pinned,
            idle_timeout_s=body.idle_timeout_s,
            target_concurrency=body.target_concurrency,
            max_loras=body.max_loras,
            max_lora_rank=max_lora_rank,
            extra_args=dict(body.extra_args),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _validate_backend_capabilities(plan=plan, backend=backend, backend_name=backend_name)
    try:
        profile = profile_store.create(
            conn,
            name=body.name,
            model_name=plan.model_name,
            hf_repo=plan.hf_repo,
            revision=plan.revision,
            backend=plan.backend,
            image_tag=plan.image_tag,
            gpu_ids=plan.gpu_ids,
            tensor_parallel=plan.tensor_parallel,
            max_model_len=plan.max_model_len,
            dtype=plan.dtype,
            pinned=plan.pinned,
            idle_timeout_s=plan.idle_timeout_s,
            target_concurrency=plan.target_concurrency,
            max_loras=plan.max_loras,
            max_lora_rank=plan.max_lora_rank,
            extra_args=plan.extra_args,
        )
    except profile_store.AlreadyExists as e:
        raise HTTPException(409, str(e)) from e
    return asdict(profile)


@router.get("/service-profiles/{name}")
def get_service_profile(
    name: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    profile = profile_store.get_by_name(conn, name)
    if profile is None:
        raise HTTPException(404, f"service profile {name!r} not found")
    return asdict(profile)


@router.post("/service-profiles/{name}/deploy", status_code=status.HTTP_201_CREATED)
async def deploy_service_profile(
    name: str,
    manager: LifecycleManager = Depends(get_manager),
    backends: dict[str, Backend] = Depends(get_backends),
    conn: sqlite3.Connection = Depends(get_conn),
):
    profile = profile_store.get_by_name(conn, name)
    if profile is None:
        raise HTTPException(404, f"service profile {name!r} not found")
    backend = backends.get(profile.backend)
    if backend is None:
        raise HTTPException(400, f"backend {profile.backend!r} not supported")
    try:
        plan = _profile_to_plan(profile)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _validate_backend_capabilities(plan=plan, backend=backend, backend_name=profile.backend)
    try:
        dep = await manager.load(plan)
    except RuntimeError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return {**asdict(dep), "gpu_ids": dep.gpu_ids}


@router.delete("/service-profiles/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_service_profile(
    name: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    profile = profile_store.get_by_name(conn, name)
    if profile is None:
        raise HTTPException(404, f"service profile {name!r} not found")
    profile_store.delete(conn, profile.id)


@router.get("/routes")
def list_service_routes(conn: sqlite3.Connection = Depends(get_conn)):
    return [asdict(r) for r in route_store.list_all(conn)]


@router.post("/routes", status_code=status.HTTP_201_CREATED)
def create_service_route(
    body: CreateServiceRouteRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        route = route_store.create(
            conn,
            name=body.name,
            match_model=body.match_model,
            profile_name=body.profile_name,
            fallback_profile_name=body.fallback_profile_name,
            enabled=body.enabled,
            priority=body.priority,
        )
    except route_store.UnknownProfile as e:
        raise HTTPException(404, str(e)) from e
    except route_store.AlreadyExists as e:
        raise HTTPException(409, str(e)) from e
    return asdict(route)


@router.get("/routes/{name}")
def get_service_route(
    name: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    route = route_store.get_by_name(conn, name)
    if route is None:
        raise HTTPException(404, f"service route {name!r} not found")
    return asdict(route)


@router.delete("/routes/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_service_route(
    name: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    route = route_store.get_by_name(conn, name)
    if route is None:
        raise HTTPException(404, f"service route {name!r} not found")
    route_store.delete(conn, route.id)


@router.get("/requests")
def list_requests(request: Request):
    """Snapshot of the in-memory request tracer ring buffer. Newest last."""
    return request.app.state.request_tracer.snapshot()


@router.get("/requests/stream")
async def stream_requests(request: Request) -> StreamingResponse:
    """SSE stream of request trace events (started / updated / completed)."""
    if request.scope.get("type") != "http":
        raise HTTPException(400, "http only")
    tracer = request.app.state.request_tracer
    sub = tracer.subscribe()

    async def gen():
        # Replay the current snapshot first so a fresh client sees the
        # backlog without waiting for new traffic.
        snap = tracer.snapshot()
        yield f"event: snapshot\ndata: {_json.dumps(snap)}\n\n".encode()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(sub.queue.get(), timeout=15.0)
                except TimeoutError:
                    yield b": keep-alive\n\n"
                    continue
                yield (
                    f"event: {msg['event']}\n"
                    f"data: {_json.dumps(msg.get('trace', {}))}\n\n"
                ).encode()
        finally:
            tracer.unsubscribe(sub)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/routes/match/dry-run")
def match_route_dry_run(
    model: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Replay the proxy's route resolution for `model` without invoking any
    engine. Returns the matched route (if any), every candidate route that
    would have matched (so a user can see which disabled or lower-priority
    route got skipped), and whether the primary/fallback target has a ready
    deployment right now.
    """
    from serve_engine.store import models as model_store
    matched = route_store.find_enabled_for_model(conn, model)
    candidates = [
        asdict(r) for r in route_store.list_all(conn) if r.match_model == model
    ]

    def ready_for(model_name: str | None) -> bool | None:
        if not model_name:
            return None
        m = model_store.get_by_name(conn, model_name)
        if m is None:
            return False
        for d in dep_store.list_all(conn):
            if d.model_id == m.id and d.status == "ready":
                return True
        return False

    primary_target = matched.target_model_name if matched else None
    fallback_target = matched.fallback_model_name if matched else None
    return {
        "requested": model,
        "matched": asdict(matched) if matched else None,
        "candidates": candidates,
        "primary_target": primary_target,
        "primary_ready": ready_for(primary_target),
        "fallback_target": fallback_target,
        "fallback_ready": ready_for(fallback_target),
    }


@router.delete("/deployments/{dep_id}", status_code=status.HTTP_204_NO_CONTENT)
async def stop_deployment(
    dep_id: int,
    manager: LifecycleManager = Depends(get_manager),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if dep_store.get_by_id(conn, dep_id) is None:
        raise HTTPException(404, f"no deployment with id {dep_id}")
    await manager.stop(dep_id)


@router.delete("/deployments", status_code=status.HTTP_204_NO_CONTENT)
async def stop_all_deployments(manager: LifecycleManager = Depends(get_manager)):
    await manager.stop_all()


@router.get("/models")
def list_models(conn: sqlite3.Connection = Depends(get_conn)):
    return [asdict(m) for m in model_store.list_all(conn)]


@router.post("/models", status_code=status.HTTP_201_CREATED)
def create_model(
    body: CreateModelRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        m = model_store.add(conn, name=body.name, hf_repo=body.hf_repo, revision=body.revision)
    except model_store.AlreadyExists as e:
        raise HTTPException(409, str(e)) from e
    return asdict(m)


@router.post("/models/{name}/download")
async def download_model_endpoint(
    name: str,
    manager: LifecycleManager = Depends(get_manager),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Synchronously download a registered model's weights to the local cache.

    Returns when the download completes (or raises 5xx if it fails).
    The CLI's `serve pull` calls this after registering; clients should
    expect potentially many minutes for large models.
    """
    m = model_store.get_by_name(conn, name)
    if m is None:
        raise HTTPException(404, f"model {name!r} not registered")
    if m.local_path is not None:
        return {"name": m.name, "local_path": m.local_path, "already_present": True}
    from serve_engine.lifecycle.downloader import download_model
    try:
        local_path = await asyncio.to_thread(
            download_model,
            hf_repo=m.hf_repo,
            revision=m.revision,
            cache_dir=manager._models_dir,
        )
    except Exception as e:
        raise HTTPException(502, f"download failed: {e}") from e
    model_store.set_local_path(conn, m.id, local_path)
    return {"name": m.name, "local_path": local_path, "already_present": False}


@router.delete("/models/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(name: str, conn: sqlite3.Connection = Depends(get_conn)):
    m = model_store.get_by_name(conn, name)
    if m is None:
        raise HTTPException(404, f"model {name!r} not found")
    blocking = [
        d for d in dep_store.list_all(conn)
        if d.model_id == m.id
        and (
            d.status in dep_store.ACTIVE_STATUSES
            or d.status == "stopping"
            or (d.status == "failed" and d.container_id is not None)
        )
    ]
    if blocking:
        ids = ", ".join(f"#{d.id}:{d.status}" for d in blocking)
        raise HTTPException(
            409,
            f"model {name!r} has deployments that must be stopped first: {ids}",
        )
    model_store.delete(conn, m.id)


# -------- Adapters --------

class CreateAdapterRequest(BaseModel):
    name: str
    base_model_name: str
    hf_repo: str
    revision: str = "main"


class AddLocalAdapterRequest(BaseModel):
    name: str
    base_model_name: str
    local_path: str


@router.get("/adapters")
def list_adapters(conn: sqlite3.Connection = Depends(get_conn)):
    """List registered adapters, including which deployments have each loaded."""
    out = []
    for a in ad_store.list_all(conn):
        loaded_into = da_store.find_deployments_with_adapter(conn, a.id)
        out.append({
            "id": a.id,
            "name": a.name,
            "base": a.base_model.name,
            "hf_repo": a.hf_repo,
            "revision": a.revision,
            "local_path": a.local_path,
            "size_mb": a.size_mb,
            "lora_rank": a.lora_rank,
            "loaded_into": loaded_into,
            "downloaded": a.local_path is not None,
            "created_at": a.created_at,
            "updated_at": a.updated_at,
        })
    return out


@router.post("/adapters", status_code=status.HTTP_201_CREATED)
def create_adapter(
    body: CreateAdapterRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        a = ad_store.add(
            conn,
            name=body.name,
            base_model_name=body.base_model_name,
            hf_repo=body.hf_repo,
            revision=body.revision,
        )
    except ad_store.NameCollision as e:
        raise HTTPException(409, str(e)) from e
    except ad_store.BaseNotFound as e:
        raise HTTPException(404, str(e)) from e
    return {
        "id": a.id, "name": a.name, "base": a.base_model.name,
        "hf_repo": a.hf_repo, "revision": a.revision,
    }


@router.delete("/adapters/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_adapter(
    name: str,
    force: bool = False,
    backends: dict[str, Backend] = Depends(get_backends),
    conn: sqlite3.Connection = Depends(get_conn),
):
    a = ad_store.get_by_name(conn, name)
    if a is None:
        raise HTTPException(404, f"adapter {name!r} not found")
    deps = da_store.find_deployments_with_adapter(conn, a.id)
    if deps and not force:
        raise HTTPException(
            409,
            f"adapter {name!r} is loaded into deployments {deps}; "
            f"hot-unload first or pass ?force=true",
        )
    # `force` cascade: hot-unload from each engine, drop junction rows, then
    # delete the registry row. Adapter blob on disk is NOT auto-deleted  -
    # operator can clean up via the HF cache directly.
    for dep_id in deps:
        dep = dep_store.get_by_id(conn, dep_id)
        if dep is not None:
            backend = backends.get(dep.backend)
            if backend is not None:
                # Best-effort: even if the engine unload fails, we still want
                # to drop the junction so the registry isn't lying about
                # what's loaded.
                try:
                    await _engine_unload_adapter(backend, dep, a.name)
                except HTTPException:
                    pass
        da_store.detach(conn, dep_id, a.id)
    ad_store.delete(conn, a.id)


@router.post("/adapters/{name}/download")
async def download_adapter_endpoint(
    name: str,
    manager: LifecycleManager = Depends(get_manager),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Synchronously download an adapter's weights to the local cache.
    Idempotent - returns the existing local_path if already downloaded."""
    a = ad_store.get_by_name(conn, name)
    if a is None:
        raise HTTPException(404, f"adapter {name!r} not registered")
    if a.local_path is not None:
        return {
            "name": a.name, "local_path": a.local_path,
            "size_mb": a.size_mb, "already_present": True,
        }
    from serve_engine.lifecycle.adapter_downloader import (
        download_adapter,
        parse_adapter_metadata,
    )
    try:
        local_path, size_mb = await asyncio.to_thread(
            download_adapter,
            hf_repo=a.hf_repo,
            revision=a.revision,
            cache_dir=manager._models_dir,
        )
    except Exception as e:
        raise HTTPException(502, f"download failed: {e}") from e
    ad_store.set_local_path(conn, a.id, local_path)
    ad_store.set_size_mb(conn, a.id, size_mb)
    # Parse adapter_config.json so we can pre-flight rank against the
    # deployment's --max-lora-rank at load time. Missing/malformed config
    # is silently tolerated (parse returns None) - exotic formats still
    # pull, they just lose the early rank check.
    meta = parse_adapter_metadata(local_path)
    if meta is not None and "lora_rank" in meta:
        ad_store.set_lora_rank(conn, a.id, meta["lora_rank"])
    return {
        "name": a.name, "local_path": local_path,
        "size_mb": size_mb, "already_present": False,
        "lora_rank": (meta or {}).get("lora_rank"),
    }


@router.post("/adapters/local", status_code=status.HTTP_201_CREATED)
def add_local_adapter(
    body: AddLocalAdapterRequest,
    manager: LifecycleManager = Depends(get_manager),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Register a pre-downloaded adapter from a local directory.

    Counterpart to `POST /admin/adapters` + `/download`: skips the HF pull
    and copies a directory the operator already has on disk into the
    managed cache so the engine container's bind-mount can see it.

    The source directory must contain an `adapter_config.json` with a
    valid PEFT `r` (LoRA rank); we use that to pre-flight the
    deployment's `--max-lora-rank` at first hot-load.
    """
    import shutil

    from serve_engine.lifecycle.adapter_downloader import parse_adapter_metadata

    src = Path(body.local_path).expanduser().resolve()
    if not src.is_dir():
        raise HTTPException(
            400, f"local_path {body.local_path!r} is not a directory"
        )
    meta = parse_adapter_metadata(src)
    if meta is None or "lora_rank" not in meta:
        raise HTTPException(
            400,
            f"{src}/adapter_config.json missing or has no valid 'r' (LoRA rank)",
        )

    # Copy into the managed cache so the engine container's /cache bind-mount
    # picks it up via the existing host->container path translation in
    # hot_load_adapter.
    dest_root = manager._models_dir.resolve() / "local-adapters"
    dest = dest_root / body.name
    if dest.exists():
        # The name collision check below would catch this anyway, but if a
        # prior failed registration left orphaned files we don't want them
        # silently merged into a new copy.
        raise HTTPException(
            409,
            f"target cache dir already exists: {dest}; "
            f"remove it manually if the prior add failed midway",
        )

    try:
        a = ad_store.add(
            conn,
            name=body.name,
            base_model_name=body.base_model_name,
            hf_repo=f"local:{src}",
            revision="local",
        )
    except ad_store.NameCollision as e:
        raise HTTPException(409, str(e)) from e
    except ad_store.BaseNotFound as e:
        raise HTTPException(404, str(e)) from e

    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)
    except OSError as e:
        # Rollback the registry row so a half-finished copy can be retried.
        ad_store.delete(conn, a.id)
        raise HTTPException(500, f"copy failed: {e}") from e

    size_bytes = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    size_mb = int((size_bytes + 1024 * 1024 - 1) // (1024 * 1024))

    ad_store.set_local_path(conn, a.id, str(dest))
    ad_store.set_size_mb(conn, a.id, size_mb)
    ad_store.set_lora_rank(conn, a.id, meta["lora_rank"])

    return {
        "name": a.name,
        "base": a.base_model.name,
        "local_path": str(dest),
        "size_mb": size_mb,
        "lora_rank": meta["lora_rank"],
    }


@router.post(
    "/deployments/{dep_id}/adapters/{adapter_name}",
    status_code=status.HTTP_201_CREATED,
)
async def hot_load_adapter(
    dep_id: int,
    adapter_name: str,
    manager: LifecycleManager = Depends(get_manager),
    backends: dict[str, Backend] = Depends(get_backends),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Hot-load an adapter into a running deployment of its base.

    Steps:
    1. Validate deployment is ready and backend.supports_adapters.
    2. Validate adapter exists, is downloaded, and base matches.
    3. If deployment's adapter slots are full, hot-unload the LRU adapter
       in the deployment first (the new adapter takes its place).
    4. POST to the engine's adapter_load_path with lora_name + lora_path.
    5. On success, attach the (deployment, adapter) row.
    """
    import httpx

    dep = dep_store.get_by_id(conn, dep_id)
    if dep is None:
        raise HTTPException(404, f"deployment {dep_id} not found")
    if dep.status != "ready":
        raise HTTPException(409, f"deployment {dep_id} is {dep.status!r}, not ready")
    backend = backends.get(dep.backend)
    if backend is None or not backend.supports_adapters:
        raise HTTPException(
            409,
            f"backend {dep.backend!r} does not support adapter hot-load",
        )
    if dep.max_loras <= 0:
        raise HTTPException(
            409,
            f"deployment {dep_id} was started with max_loras=0; "
            "restart with --max-loras N to enable adapter hot-load",
        )
    async with manager.adapter_lock(dep.id):
        a = ad_store.get_by_name(conn, adapter_name)
        if a is None:
            raise HTTPException(404, f"adapter {adapter_name!r} not registered")
        if a.local_path is None:
            raise HTTPException(
                409,
                f"adapter {adapter_name!r} not downloaded; "
                f"POST /admin/adapters/{adapter_name}/download first",
            )
        if a.base_model.id != dep.model_id:
            raise HTTPException(
                409,
                f"adapter base {a.base_model.name!r} does not match "
                f"deployment model_id {dep.model_id}",
            )

        victim = None
        # If slots full, evict LRU adapter in this deployment first.
        if da_store.count_for_deployment(conn, dep.id) >= dep.max_loras:
            victim = da_store.lru_for_deployment(conn, dep.id)
            if victim is not None and victim.id != a.id:
                await _engine_unload_adapter(backend, dep, victim.name)
                da_store.detach(conn, dep.id, victim.id)

        # Translate host adapter path into in-container path (mounted at /cache).
        container_path = "/cache/" + str(
            Path(a.local_path).resolve().relative_to(manager._models_dir.resolve())
        )
        body = {"lora_name": a.name, "lora_path": container_path}
        url = (
            f"http://{dep.container_address}:{dep.container_port}"
            f"{backend.adapter_load_path}"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                r = await client.post(url, json=body)
            except httpx.HTTPError as e:
                raise HTTPException(502, f"engine adapter load failed: {e}") from e
        if r.status_code >= 400:
            raise HTTPException(
                502, f"engine returned {r.status_code}: {r.text[:200]}",
            )
        da_store.attach(conn, dep.id, a.id)
        return {
            "deployment_id": dep.id, "adapter": a.name,
            "evicted": victim.name if victim else None,
        }


@router.delete(
    "/deployments/{dep_id}/adapters/{adapter_name}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def hot_unload_adapter(
    dep_id: int,
    adapter_name: str,
    backends: dict[str, Backend] = Depends(get_backends),
    conn: sqlite3.Connection = Depends(get_conn),
):
    dep = dep_store.get_by_id(conn, dep_id)
    if dep is None:
        raise HTTPException(404, f"deployment {dep_id} not found")
    a = ad_store.get_by_name(conn, adapter_name)
    if a is None:
        raise HTTPException(404, f"adapter {adapter_name!r} not registered")
    backend = backends.get(dep.backend)
    if backend is None:
        raise HTTPException(409, f"backend {dep.backend!r} not registered")
    await _engine_unload_adapter(backend, dep, a.name)
    da_store.detach(conn, dep.id, a.id)


async def _engine_unload_adapter(
    backend: Backend, dep, adapter_name: str,
) -> None:
    """POST to the engine's unload path. Best-effort - if the engine
    container is gone the unload still succeeds at the registry level
    (via the surrounding detach call)."""
    import httpx

    url = (
        f"http://{dep.container_address}:{dep.container_port}"
        f"{backend.adapter_unload_path}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(url, json={"lora_name": adapter_name})
        except httpx.HTTPError:
            return  # engine gone; let detach proceed
    if r.status_code >= 500:
        # 4xx is fine (e.g., adapter wasn't loaded after all). 5xx means
        # the engine is in a bad state - surface it.
        raise HTTPException(
            502, f"engine returned {r.status_code} on unload: {r.text[:200]}",
        )


@router.get("/predictor/candidates")
def predictor_candidates(request: Request):
    """Current top-N predictor candidates with score + reason. Drives
    `serve predict`."""
    task = getattr(request.app.state, "predictor_task", None)
    if task is None:
        return []
    cands = task._predictor.candidates()
    return [
        {
            "base_name": c.base_name,
            "adapter_name": c.adapter_name,
            "score": round(c.score, 4),
            "reason": c.reason,
        }
        for c in cands
    ]


@router.get("/predictor/stats")
def predictor_stats(request: Request):
    """Tick-loop counters since daemon startup. Drives `serve predict --stats`."""
    task = getattr(request.app.state, "predictor_task", None)
    if task is None:
        return {
            "enabled": False,
            "preloads_attempted": 0,
            "preloads_succeeded": 0,
            "preloads_skipped_already_warm": 0,
            "preloads_skipped_no_deployment": 0,
            "base_prewarms_attempted": 0,
            "base_prewarms_succeeded": 0,
            "base_prewarms_skipped_no_plan": 0,
        }
    return {
        "enabled": task._config.enabled,
        "tick_interval_s": task._config.tick_interval_s,
        "max_prewarm_per_tick": task._config.max_prewarm_per_tick,
        "max_base_prewarm_per_tick": task._config.max_base_prewarm_per_tick,
        "preloads_attempted": task.preloads_attempted,
        "preloads_succeeded": task.preloads_succeeded,
        "preloads_skipped_already_warm": task.preloads_skipped_already_warm,
        "preloads_skipped_no_deployment": task.preloads_skipped_no_deployment,
        "base_prewarms_attempted": task.base_prewarms_attempted,
        "base_prewarms_succeeded": task.base_prewarms_succeeded,
        "base_prewarms_skipped_no_plan": task.base_prewarms_skipped_no_plan,
    }


@router.post("/deployments/{dep_id}/pin", status_code=status.HTTP_204_NO_CONTENT)
async def pin_deployment(
    dep_id: int,
    manager: LifecycleManager = Depends(get_manager),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if dep_store.get_by_id(conn, dep_id) is None:
        raise HTTPException(404, f"no deployment with id {dep_id}")
    await manager.pin(dep_id, True)


@router.post("/deployments/{dep_id}/unpin", status_code=status.HTTP_204_NO_CONTENT)
async def unpin_deployment(
    dep_id: int,
    manager: LifecycleManager = Depends(get_manager),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if dep_store.get_by_id(conn, dep_id) is None:
        raise HTTPException(404, f"no deployment with id {dep_id}")
    await manager.pin(dep_id, False)


@router.get("/deployments/current/logs")
def stream_current_logs(request: Request):
    conn: sqlite3.Connection = request.app.state.conn
    docker_client = request.app.state.manager._docker
    active = dep_store.find_active(conn)
    if active is None or active.container_id is None:
        raise HTTPException(404, "no active deployment with a running container")

    def gen():
        for chunk in docker_client.stream_logs(active.container_id, follow=True):
            if isinstance(chunk, bytes):
                yield chunk
            else:
                yield chunk.encode()

    return StreamingResponse(gen(), media_type="text/plain")


@router.get("/deployments/{dep_id}/logs/stream")
async def stream_engine_logs_sse(dep_id: int, request: Request) -> StreamingResponse:
    """SSE: stdout/stderr of the engine container for this deployment.

    Designed for the browser EventSource - each docker log chunk is reframed
    as one or more `data: <line>` SSE events. Includes the last 500 lines as
    history before following. The underlying sync iterator from docker-py is
    bridged into the event loop via asyncio.to_thread; on client disconnect
    the consumer task is cancelled but the docker iterator may continue to
    drain in the background until the container stops (small thread leak,
    acceptable since these are bounded by container lifetime).
    """
    conn: sqlite3.Connection = request.app.state.conn
    docker_client = request.app.state.manager._docker
    dep = dep_store.get_by_id(conn, dep_id)
    if dep is None:
        raise HTTPException(404, f"no deployment with id {dep_id}")
    if dep.container_id is None:
        raise HTTPException(404, f"deployment {dep_id} has no container")

    async def gen():
        yield ":ok\n\n"
        try:
            sync_iter = docker_client.stream_logs(
                dep.container_id, follow=True, tail=500,
            )
        except Exception as e:
            yield f"data: [serve-engine] failed to attach: {e}\n\n"
            return
        sentinel = object()
        while True:
            chunk = await asyncio.to_thread(next, sync_iter, sentinel)
            if chunk is sentinel:
                yield "data: [serve-engine] log stream ended\n\n"
                return
            if isinstance(chunk, bytes):
                text = chunk.decode("utf-8", errors="replace")
            else:
                text = str(chunk)
            for line in text.splitlines():
                # Each SSE event must be a single line; collapse blank lines.
                if line:
                    yield f"data: {line}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


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
    # None = unrestricted (default), [] = deny-all, [<name>...] = allowlist.
    allowed_models: list[str] | None = None


class UpdateKeyRequest(BaseModel):
    # We use the `default factory sentinel` pattern via the explicit-None
    # semantics of the column: PATCH bodies must include `allowed_models`
    # to change it. There is no partial-update path that omits the field,
    # so a missing key in JSON is rejected as 422 by pydantic.
    allowed_models: list[str] | None = None


@router.post("/stream-token")
def create_stream_token(request: Request):
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
    secret, k = _ak_store.create(
        conn, name=body.name, tier=body.tier,
        rpm_override=body.rpm_override, tpm_override=body.tpm_override,
        rph_override=body.rph_override, tph_override=body.tph_override,
        rpd_override=body.rpd_override, tpd_override=body.tpd_override,
        rpw_override=body.rpw_override, tpw_override=body.tpw_override,
        allowed_models=body.allowed_models,
    )
    return {
        "id": k.id,
        "name": k.name,
        "prefix": k.prefix,
        "tier": k.tier,
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
    """Per-key time-bucketed usage over the past `window_s` seconds. Defaults
    to a 24h window with 1h buckets. Used by the UI to render per-key
    request/token sparklines.
    """
    from serve_engine.store import api_keys as api_key_store
    from serve_engine.store import key_usage as key_usage_store
    if api_key_store.get_by_id(conn, key_id) is None:
        raise HTTPException(404, f"api key {key_id} not found")
    if window_s <= 0 or bucket_s <= 0:
        raise HTTPException(400, "window_s and bucket_s must be positive")
    if window_s // bucket_s > 1024:
        raise HTTPException(400, "too many buckets requested (cap is 1024)")
    return {
        "key_id": key_id,
        "window_s": window_s,
        "bucket_s": bucket_s,
        "buckets": key_usage_store.bucketed_usage(
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


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    """SSE: lifecycle events as `data: <json>\n\n` chunks. Heartbeat every 15s."""
    bus = request.app.state.event_bus

    async def gen():
        async with bus.subscribe() as queue:
            yield ":ok\n\n"  # initial heartbeat
            while True:
                try:
                    e = await asyncio.wait_for(queue.get(), timeout=15.0)
                    payload = _json.dumps({
                        "kind": e.kind, "payload": e.payload, "ts": e.ts,
                    })
                    yield f"data: {payload}\n\n"
                except TimeoutError:
                    yield ":hb\n\n"  # SSE comment heartbeat

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/gpus")
def list_gpus():
    """Per-GPU live snapshot: memory, utilization, power."""
    return [
        {
            "index": s.index,
            "memory_used_mb": s.memory_used_mb,
            "memory_total_mb": s.memory_total_mb,
            "gpu_util_pct": s.gpu_util_pct,
            "power_w": s.power_w,
        }
        for s in _read_gpu_stats()
    ]


@router.get("/backends")
def list_backends(backends: dict[str, Backend] = Depends(get_backends)):
    """Registered engine backends — names, default image, capabilities."""
    return [
        {
            "name": name,
            "image_default": b.image_default,
            "supports_adapters": getattr(b, "supports_adapters", False),
        }
        for name, b in sorted(backends.items())
    ]


# ---------------------------------------------------------------------------
# Multi-node — /admin/nodes/*
# ---------------------------------------------------------------------------


class EnrollBody(BaseModel):
    label: str


@router.post("/nodes/enroll")
def admin_nodes_enroll(body: EnrollBody, request: Request):
    """Mint a single-use enrollment token bound to `label`.

    Returns the token, the leader's cluster URL the agent should dial,
    the CA cert in PEM, and the CA fingerprint (the agent pins this
    fingerprint to detect a swapped CA during the bootstrap fetch)."""
    tokens = request.app.state.enrollment_tokens
    token = tokens.mint(label=body.label)
    return {
        "token": token,
        "leader_url": request.app.state.leader_url,
        "ca_cert": request.app.state.ca_cert_pem,
        "ca_fingerprint": request.app.state.ca_fingerprint,
    }


@router.get("/nodes")
def admin_nodes_list(conn: sqlite3.Connection = Depends(get_conn)):
    """All known nodes (the local row + every enrolled remote agent)."""
    return {"nodes": [asdict(n) for n in nodes_store.list_all(conn)]}


@router.get("/nodes/{node_id}")
def admin_nodes_show(
    node_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    n = nodes_store.get(conn, node_id)
    if n is None:
        raise HTTPException(404, f"node {node_id} not found")
    return {
        "node": asdict(n),
        "gpus": [asdict(g) for g in node_gpus_store.list_for_node(conn, node_id)],
    }


@router.delete("/nodes/{node_id}")
def admin_nodes_remove(
    node_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    n = nodes_store.get(conn, node_id)
    if n is None:
        raise HTTPException(404, f"node {node_id} not found")
    if n.label == "local":
        raise HTTPException(400, "cannot remove the local node")
    # Drop any live link before removing the row so its cert fingerprint
    # can't continue to authenticate connections.
    registry = request.app.state.agent_registry
    if registry.get(node_id) is not None:
        registry.unregister(node_id)
    nodes_store.delete(conn, node_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Cluster + config inspection — read-only views over the daemon's running
# state, for the UI's Cluster / Settings pages.
# ---------------------------------------------------------------------------


@router.get("/cluster")
def admin_cluster_info(request: Request):
    """Snapshot of cluster transport state: leader URLs the daemon advertises,
    CA fingerprint, server cert SAN/expiry, listener bindings.

    Used by the UI's Cluster page to render the trust-domain summary."""
    from datetime import UTC, datetime

    from cryptography import x509

    from serve_engine import config as _cfg

    server_crt_path = _cfg.LEADER_DIR / "server.crt"
    server_info: dict[str, object] = {"present": False}
    if server_crt_path.exists():
        try:
            cert = x509.load_pem_x509_certificate(server_crt_path.read_bytes())
            try:
                san = cert.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName,
                ).value
                sans = [
                    str(e.value) if isinstance(e, x509.IPAddress) else e.value
                    for e in san
                ]
            except x509.ExtensionNotFound:
                sans = []
            not_after = cert.not_valid_after_utc
            days_left = (not_after - datetime.now(UTC)).days
            server_info = {
                "present": True,
                "san": sans,
                "not_after": not_after.isoformat(),
                "days_left": days_left,
            }
        except Exception as e:  # pragma: no cover — defensive
            server_info = {"present": True, "error": str(e)}

    public_tls_configured = bool(
        request.app.state.__dict__.get("public_tls_configured", False)
    )
    # The daemon's resolved config isn't kept on app.state today; recompute
    # via the same resolver so the UI shows what the running process is
    # advertising. resolve_config is cheap (file + autodetect + dataclass).
    cfg = _cfg.resolve_config()
    return {
        "leader_url": request.app.state.leader_url,
        "ca_fingerprint": request.app.state.ca_fingerprint,
        "public_url": cfg.public_url,
        "cluster_url": cfg.cluster_url,
        "public_bind": f"{cfg.public_bind}:{cfg.public_port}",
        "cluster_bind": f"{cfg.cluster_bind}:{cfg.cluster_port}",
        "public_tls_configured": cfg.public_cert_path is not None,
        "leader_server_cert": server_info,
    }


@router.get("/config")
def admin_config(request: Request):  # noqa: ARG001
    """Resolved address/TLS config + which source layer each field came from.

    Mirrors `serve config show` for the UI's Settings page."""
    from serve_engine import config as _cfg

    cfg = _cfg.resolve_config()
    return {
        "values": {
            "public_host": cfg.public_host,
            "public_port": cfg.public_port,
            "public_bind": cfg.public_bind,
            "cluster_host": cfg.cluster_host,
            "cluster_port": cfg.cluster_port,
            "cluster_bind": cfg.cluster_bind,
            "public_cert_path": (
                str(cfg.public_cert_path) if cfg.public_cert_path else None
            ),
            "public_key_path": (
                str(cfg.public_key_path) if cfg.public_key_path else None
            ),
            "leader_url_override": cfg.leader_url_override,
        },
        "sources": cfg.source,
        "config_file": str(_cfg.CONFIG_FILE),
        "config_file_exists": _cfg.CONFIG_FILE.exists(),
    }


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

_audit_log = _audit_logging.getLogger("serve_engine.audit")
_rl_buckets: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    client = request.scope.get("client")
    if client is None:
        return "uds"
    return client[0] if isinstance(client, tuple) else str(client)


def _rate_limit(
    request: Request, *, route: str, limit: int, window_s: float = 60.0,
) -> None:
    """Raise 429 if `_client_ip` has exceeded `limit` calls to `route`
    within the last `window_s` seconds. UDS callers are exempt."""
    ip = _client_ip(request)
    if ip == "uds":
        return
    key = f"{route}|{ip}"
    bucket = _rl_buckets[key]
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

    from serve_engine.cluster.ca import fingerprint_sha256, issue_agent_cert

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
