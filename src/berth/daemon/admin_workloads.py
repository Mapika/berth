from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import asdict
from typing import cast

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel

from berth.backends.base import Backend
from berth.daemon.admin import get_backends, get_conn, get_manager, router
from berth.lifecycle.manager import LifecycleManager
from berth.lifecycle.plan import BackendName, DeploymentPlan
from berth.store import deployments as dep_store
from berth.store import models as model_store
from berth.store import service_profiles as profile_store
from berth.store import service_routes as route_store


class _DeploymentRequestBase(BaseModel):
    model_config = {"protected_namespaces": ()}
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
    node_label: str | None = None


class CreateDeploymentRequest(_DeploymentRequestBase):
    pass


class CreateServiceProfileRequest(_DeploymentRequestBase):
    name: str


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
        raise HTTPException(400, f"--max-lora-rank must be an integer; got {raw!r}") from e


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


def _unsafe_deploy_options_allowed(manager: LifecycleManager) -> bool:
    cfg = getattr(manager, "resolved_cfg", None)
    return bool(getattr(cfg, "allow_unsafe_deploy_options", False))


def _validate_safe_deploy_options(
    *,
    body: _DeploymentRequestBase,
    backend_name: str,
    backend: Backend,
    manager: LifecycleManager,
) -> None:
    if _unsafe_deploy_options_allowed(manager):
        return
    if body.image_tag is not None and body.image_tag != backend.image_default:
        raise HTTPException(
            400,
            "custom engine images are disabled by default; set "
            "[server].allow_unsafe_deploy_options=true only on trusted "
            "leaders if you need --image",
        )
    if body.extra_args:
        raise HTTPException(
            400,
            "raw engine extra_args are disabled by default; set "
            "[server].allow_unsafe_deploy_options=true only on trusted "
            "leaders if you need advanced engine flags",
        )
    if backend_name == "trtllm":
        raise HTTPException(
            400,
            "trtllm deployments are disabled by default because the backend "
            "enables Hugging Face remote code loading; set "
            "[server].allow_unsafe_deploy_options=true only if you trust the "
            "model repository and deployment surface",
        )


def _validate_profile_safe_deploy_options(
    *,
    profile: profile_store.ServiceProfile,
    backend: Backend,
    manager: LifecycleManager,
) -> None:
    if _unsafe_deploy_options_allowed(manager):
        return
    body = CreateDeploymentRequest(
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
        extra_args=dict(profile.extra_args),
        node_label=profile.node_label,
    )
    _validate_safe_deploy_options(
        body=body,
        backend_name=profile.backend,
        backend=backend,
        manager=manager,
    )


def _profile_to_plan(profile: profile_store.ServiceProfile) -> DeploymentPlan:
    return DeploymentPlan(
        model_name=profile.model_name,
        hf_repo=profile.hf_repo,
        revision=profile.revision,
        backend=cast(BackendName, profile.backend),
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
        node_label=profile.node_label,
    )


def _request_to_plan(
    body: _DeploymentRequestBase,
    *,
    backend_name: str,
    image_tag: str,
    tensor_parallel: int,
    max_lora_rank: int,
) -> DeploymentPlan:
    return DeploymentPlan(
        model_name=body.model_name,
        hf_repo=body.hf_repo,
        revision=body.revision,
        backend=cast(BackendName, backend_name),
        image_tag=image_tag,
        gpu_ids=body.gpu_ids,
        tensor_parallel=tensor_parallel,
        max_model_len=body.max_model_len,
        dtype=body.dtype,
        pinned=body.pinned,
        idle_timeout_s=body.idle_timeout_s,
        target_concurrency=body.target_concurrency,
        max_loras=body.max_loras,
        max_lora_rank=max_lora_rank,
        extra_args=dict(body.extra_args),
        node_label=body.node_label,
    )


def _plan_from_request(
    body: _DeploymentRequestBase,
    backends: dict[str, Backend],
) -> tuple[DeploymentPlan, Backend]:
    from berth.backends.selection import load_selection, pick_backend

    backend_name = body.backend or pick_backend(load_selection(), body.model_name)
    if backend_name not in backends:
        raise HTTPException(400, f"backend {backend_name!r} not supported")
    backend = backends[backend_name]
    plan = _request_to_plan(
        body,
        backend_name=backend_name,
        image_tag=body.image_tag or backend.image_default,
        tensor_parallel=body.tensor_parallel or len(body.gpu_ids),
        max_lora_rank=_max_lora_rank_from_extra(body.extra_args),
    )
    _validate_backend_capabilities(plan=plan, backend=backend, backend_name=backend_name)
    return plan, backend


@router.get("/deployments")
def list_deployments(
    conn: sqlite3.Connection = Depends(get_conn),
    manager: LifecycleManager = Depends(get_manager),
):
    from berth.observability.gpu_stats import read_compute_process_vram

    pid_vram = read_compute_process_vram()
    out = []
    for dep in dep_store.list_all(conn):
        used_mb: int | None = None
        if (
            pid_vram
            and manager._docker is not None
            and dep.container_id
            and dep.status in ("loading", "ready")
        ):
            try:
                pids = manager._docker.container_pids(dep.container_id)
                used_mb = sum(pid_vram.get(pid, 0) for pid in pids) or None
            except Exception:
                used_mb = None
        out.append({**asdict(dep), "gpu_ids": dep.gpu_ids, "vram_used_mb": used_mb})
    return out


@router.post("/deployments", status_code=status.HTTP_201_CREATED)
async def create_deployment(
    body: CreateDeploymentRequest,
    manager: LifecycleManager = Depends(get_manager),
    backends: dict[str, Backend] = Depends(get_backends),
):
    try:
        plan, _backend = _plan_from_request(body, backends)
        _validate_safe_deploy_options(
            body=body,
            backend_name=plan.backend,
            backend=_backend,
            manager=manager,
        )
        dep = await manager.load(plan)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return {**asdict(dep), "gpu_ids": dep.gpu_ids}


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


@router.get("/models")
def list_models(conn: sqlite3.Connection = Depends(get_conn)):
    return [asdict(model) for model in model_store.list_all(conn)]


@router.post("/models", status_code=status.HTTP_201_CREATED)
def create_model(
    body: CreateModelRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        model = model_store.add(
            conn, name=body.name, hf_repo=body.hf_repo, revision=body.revision,
        )
    except model_store.AlreadyExists as e:
        raise HTTPException(409, str(e)) from e
    return asdict(model)


@router.post("/models/{name}/download")
async def download_model_endpoint(
    name: str,
    manager: LifecycleManager = Depends(get_manager),
    conn: sqlite3.Connection = Depends(get_conn),
):
    model = model_store.get_by_name(conn, name)
    if model is None:
        raise HTTPException(404, f"model {name!r} not registered")
    if model.local_path is not None:
        return {"name": model.name, "local_path": model.local_path, "already_present": True}
    from berth.lifecycle.downloader import download_model

    try:
        local_path = await asyncio.to_thread(
            download_model,
            hf_repo=model.hf_repo,
            revision=model.revision,
            cache_dir=manager.models_dir,
        )
    except Exception as e:
        raise HTTPException(502, f"download failed: {e}") from e
    model_store.set_local_path(conn, model.id, local_path)
    return {"name": model.name, "local_path": local_path, "already_present": False}


@router.delete("/models/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(name: str, conn: sqlite3.Connection = Depends(get_conn)):
    model = model_store.get_by_name(conn, name)
    if model is None:
        raise HTTPException(404, f"model {name!r} not found")
    model_deployments = [
        dep for dep in dep_store.list_all(conn)
        if dep.model_id == model.id
    ]
    blocking = [
        dep for dep in model_deployments
        if (
            dep.status in dep_store.ACTIVE_STATUSES
            or dep.status == "stopping"
            or (dep.status == "failed" and dep.container_id is not None)
        )
    ]
    if blocking:
        ids = ", ".join(f"#{dep.id}:{dep.status}" for dep in blocking)
        raise HTTPException(
            409,
            f"model {name!r} has deployments that must be stopped first: {ids}",
        )
    stopped_dep_ids = [dep.id for dep in model_deployments]
    if stopped_dep_ids:
        placeholders = ",".join(["?"] * len(stopped_dep_ids))
        conn.execute(
            f"UPDATE usage_events SET deployment_id=NULL WHERE deployment_id IN ({placeholders})",  # nosec
            stopped_dep_ids,
        )
    model_store.delete(conn, model.id)


@router.get("/service-profiles")
def list_service_profiles(conn: sqlite3.Connection = Depends(get_conn)):
    return [asdict(profile) for profile in profile_store.list_all(conn)]


@router.post("/service-profiles", status_code=status.HTTP_201_CREATED)
def create_service_profile(
    body: CreateServiceProfileRequest,
    manager: LifecycleManager = Depends(get_manager),
    backends: dict[str, Backend] = Depends(get_backends),
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        plan, _backend = _plan_from_request(body, backends)
        _validate_safe_deploy_options(
            body=body,
            backend_name=plan.backend,
            backend=_backend,
            manager=manager,
        )
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
            node_label=plan.node_label,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
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
        _validate_profile_safe_deploy_options(
            profile=profile,
            backend=backend,
            manager=manager,
        )
        _validate_backend_capabilities(plan=plan, backend=backend, backend_name=profile.backend)
        dep = await manager.load(plan)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
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
    return [asdict(route) for route in route_store.list_all(conn)]


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


@router.get("/routes/match/dry-run")
def match_route_dry_run(
    model: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    matched = route_store.find_enabled_for_model(conn, model)
    candidates = [
        asdict(route) for route in route_store.list_all(conn)
        if route.match_model == model
    ]

    def ready_for(model_name: str | None) -> bool | None:
        if not model_name:
            return None
        registered = model_store.get_by_name(conn, model_name)
        if registered is None:
            return False
        return any(
            dep.model_id == registered.id and dep.status == "ready"
            for dep in dep_store.list_all(conn)
        )

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
