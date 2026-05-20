from __future__ import annotations

import asyncio
import re
import shutil
import sqlite3
from pathlib import Path

import httpx
from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, field_validator

from berth.backends.base import Backend
from berth.daemon.admin import get_backends, get_conn, get_manager, router
from berth.lifecycle.manager import LifecycleManager
from berth.store import adapters as ad_store
from berth.store import deployment_adapters as da_store
from berth.store import deployments as dep_store

_ADAPTER_NAME_RE = re.compile(r"[a-zA-Z0-9_-]+")


def _validate_adapter_name(v: str) -> str:
    if not _ADAPTER_NAME_RE.fullmatch(v):
        raise ValueError("name must be alphanumeric, underscores, or dashes only")
    return v


class CreateAdapterRequest(BaseModel):
    name: str
    base_model_name: str
    hf_repo: str
    revision: str = "main"

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _validate_adapter_name(v)


class AddLocalAdapterRequest(BaseModel):
    name: str
    base_model_name: str
    local_path: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _validate_adapter_name(v)

@router.get("/adapters")
def list_adapters(conn: sqlite3.Connection = Depends(get_conn)):
    out = []
    for adapter in ad_store.list_all(conn):
        out.append({
            "id": adapter.id,
            "name": adapter.name,
            "base": adapter.base_model.name,
            "hf_repo": adapter.hf_repo,
            "revision": adapter.revision,
            "local_path": adapter.local_path,
            "size_mb": adapter.size_mb,
            "lora_rank": adapter.lora_rank,
            "loaded_into": da_store.find_deployments_with_adapter(conn, adapter.id),
            "downloaded": adapter.local_path is not None,
            "created_at": adapter.created_at,
            "updated_at": adapter.updated_at,
        })
    return out


@router.post("/adapters", status_code=status.HTTP_201_CREATED)
def create_adapter(
    body: CreateAdapterRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        adapter = ad_store.add(
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
        "id": adapter.id,
        "name": adapter.name,
        "base": adapter.base_model.name,
        "hf_repo": adapter.hf_repo,
        "revision": adapter.revision,
    }


@router.delete("/adapters/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_adapter(
    name: str,
    force: bool = False,
    backends: dict[str, Backend] = Depends(get_backends),
    conn: sqlite3.Connection = Depends(get_conn),
):
    adapter = ad_store.get_by_name(conn, name)
    if adapter is None:
        raise HTTPException(404, f"adapter {name!r} not found")
    deployments = da_store.find_deployments_with_adapter(conn, adapter.id)
    if deployments and not force:
        raise HTTPException(
            409,
            f"adapter {name!r} is loaded into deployments {deployments}; "
            f"hot-unload first or pass ?force=true",
        )
    for dep_id in deployments:
        dep = dep_store.get_by_id(conn, dep_id)
        if dep is not None:
            backend = backends.get(dep.backend)
            if backend is not None:
                try:
                    await _engine_unload_adapter(backend, dep, adapter.name)
                except HTTPException:
                    pass
        da_store.detach(conn, dep_id, adapter.id)
    ad_store.delete(conn, adapter.id)


@router.post("/adapters/{name}/download")
async def download_adapter_endpoint(
    name: str,
    manager: LifecycleManager = Depends(get_manager),
    conn: sqlite3.Connection = Depends(get_conn),
):
    adapter = ad_store.get_by_name(conn, name)
    if adapter is None:
        raise HTTPException(404, f"adapter {name!r} not registered")
    if adapter.local_path is not None:
        return {
            "name": adapter.name,
            "local_path": adapter.local_path,
            "size_mb": adapter.size_mb,
            "already_present": True,
        }
    from berth.lifecycle.adapter_downloader import (
        download_adapter,
        parse_adapter_metadata,
    )

    try:
        local_path, size_mb = await asyncio.to_thread(
            download_adapter,
            hf_repo=adapter.hf_repo,
            revision=adapter.revision,
            cache_dir=manager.models_dir,
        )
    except Exception as e:
        raise HTTPException(502, f"download failed: {e}") from e
    ad_store.set_local_path(conn, adapter.id, local_path)
    ad_store.set_size_mb(conn, adapter.id, size_mb)
    meta = parse_adapter_metadata(local_path)
    if meta is not None and "lora_rank" in meta:
        ad_store.set_lora_rank(conn, adapter.id, meta["lora_rank"])
    return {
        "name": adapter.name,
        "local_path": local_path,
        "size_mb": size_mb,
        "already_present": False,
        "lora_rank": (meta or {}).get("lora_rank"),
    }


@router.post("/adapters/local", status_code=status.HTTP_201_CREATED)
def add_local_adapter(
    body: AddLocalAdapterRequest,
    manager: LifecycleManager = Depends(get_manager),
    conn: sqlite3.Connection = Depends(get_conn),
):
    from berth.lifecycle.adapter_downloader import parse_adapter_metadata

    src = Path(body.local_path).expanduser().resolve()
    if not src.is_dir():
        raise HTTPException(400, f"local_path {body.local_path!r} is not a directory")
    meta = parse_adapter_metadata(src)
    if meta is None or "lora_rank" not in meta:
        raise HTTPException(
            400,
            f"{src}/adapter_config.json missing or has no valid 'r' (LoRA rank)",
        )

    dest_root = manager.models_dir.resolve() / "local-adapters"
    dest = (dest_root / body.name).resolve()
    if not dest.is_relative_to(dest_root.resolve()):
        raise HTTPException(400, "Invalid adapter name or path traversal detected")
    if dest.exists():
        raise HTTPException(
            409,
            f"target cache dir already exists: {dest}; "
            f"remove it manually if the prior add failed midway",
        )

    try:
        adapter = ad_store.add(
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
        ad_store.delete(conn, adapter.id)
        raise HTTPException(500, f"copy failed: {e}") from e

    size_bytes = sum(file.stat().st_size for file in dest.rglob("*") if file.is_file())
    size_mb = int((size_bytes + 1024 * 1024 - 1) // (1024 * 1024))

    ad_store.set_local_path(conn, adapter.id, str(dest))
    ad_store.set_size_mb(conn, adapter.id, size_mb)
    ad_store.set_lora_rank(conn, adapter.id, meta["lora_rank"])

    return {
        "name": adapter.name,
        "base": adapter.base_model.name,
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
        adapter = ad_store.get_by_name(conn, adapter_name)
        if adapter is None:
            raise HTTPException(404, f"adapter {adapter_name!r} not registered")
        if adapter.local_path is None:
            raise HTTPException(
                409,
                f"adapter {adapter_name!r} not downloaded; "
                f"POST /admin/adapters/{adapter_name}/download first",
            )
        if adapter.base_model.id != dep.model_id:
            raise HTTPException(
                409,
                f"adapter base {adapter.base_model.name!r} does not match "
                f"deployment model_id {dep.model_id}",
            )

        victim = None
        if da_store.count_for_deployment(conn, dep.id) >= dep.max_loras:
            victim = da_store.lru_for_deployment(conn, dep.id)
            if victim is not None and victim.id != adapter.id:
                await _engine_unload_adapter(backend, dep, victim.name)
                da_store.detach(conn, dep.id, victim.id)

        if dep.container_address == "tunnel":
            # Remote deployment — adapter file lives on the leader, the
            # engine HTTP API is only reachable through the agent's WS
            # tunnel, and neither half of that bridge is implemented yet
            # for adapter ops. Reject explicitly instead of falling
            # through to a direct-dial of the (untrusted) container_address.
            raise HTTPException(
                501,
                "adapter hot-load is not supported on remote deployments",
            )
        container_path = "/cache/" + str(
            Path(adapter.local_path).resolve().relative_to(manager.models_dir.resolve())
        )
        url = (
            f"http://{dep.container_address}:{dep.container_port}"
            f"{backend.adapter_load_path}"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    url,
                    json={"lora_name": adapter.name, "lora_path": container_path},
                )
            except httpx.HTTPError as e:
                raise HTTPException(502, f"engine adapter load failed: {e}") from e
        if response.status_code >= 400:
            raise HTTPException(
                502, f"engine returned {response.status_code}: {response.text[:200]}",
            )
        da_store.attach(conn, dep.id, adapter.id)
        return {
            "deployment_id": dep.id,
            "adapter": adapter.name,
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
    adapter = ad_store.get_by_name(conn, adapter_name)
    if adapter is None:
        raise HTTPException(404, f"adapter {adapter_name!r} not registered")
    backend = backends.get(dep.backend)
    if backend is None:
        raise HTTPException(409, f"backend {dep.backend!r} not registered")
    await _engine_unload_adapter(backend, dep, adapter.name)
    da_store.detach(conn, dep.id, adapter.id)


async def _engine_unload_adapter(backend: Backend, dep, adapter_name: str) -> None:
    if dep.container_address == "tunnel":
        return  # remote — no direct-dial unload path
    url = (
        f"http://{dep.container_address}:{dep.container_port}"
        f"{backend.adapter_unload_path}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, json={"lora_name": adapter_name})
        except httpx.HTTPError:
            return
    if response.status_code >= 500:
        raise HTTPException(
            502,
            f"engine returned {response.status_code} on unload: {response.text[:200]}",
        )
