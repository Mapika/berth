from __future__ import annotations

import asyncio
import json as _json
import sqlite3

from fastapi import Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from berth.backends.base import Backend
from berth.daemon.admin import get_backends, router
from berth.store import deployments as dep_store
from berth.store import nodes as nodes_store


@router.get("/requests")
def list_requests(request: Request):
    return request.app.state.request_tracer.snapshot()


@router.get("/requests/stream")
async def stream_requests(request: Request) -> StreamingResponse:
    if request.scope.get("type") != "http":
        raise HTTPException(400, "http only")
    tracer = request.app.state.request_tracer
    sub = tracer.subscribe()

    async def gen():
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


@router.get("/predictor/candidates")
def predictor_candidates(request: Request):
    task = getattr(request.app.state, "predictor_task", None)
    if task is None:
        return []
    return task.candidates_snapshot()


@router.get("/predictor/stats")
def predictor_stats(request: Request):
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
    return task.stats_snapshot()


@router.get("/deployments/current/logs")
def stream_current_logs(request: Request):
    conn: sqlite3.Connection = request.app.state.conn
    docker_client = request.app.state.manager._docker
    active = dep_store.find_active(conn)
    if active is None or active.container_id is None:
        raise HTTPException(404, "no active deployment with a running container")
    if docker_client is None:
        raise HTTPException(
            503,
            "this leader runs in control-plane only mode; use the per-deployment "
            "logs endpoint (which routes through the agent tunnel) instead",
        )

    def gen():
        for chunk in docker_client.stream_logs(active.container_id, follow=True):
            if isinstance(chunk, bytes):
                yield chunk
            else:
                yield chunk.encode()

    return StreamingResponse(gen(), media_type="text/plain")


@router.get("/deployments/{dep_id}/logs/stream")
async def stream_engine_logs_sse(dep_id: int, request: Request) -> StreamingResponse:
    conn: sqlite3.Connection = request.app.state.conn
    dep = dep_store.get_by_id(conn, dep_id)
    if dep is None:
        raise HTTPException(404, f"no deployment with id {dep_id}")
    if dep.container_id is None:
        raise HTTPException(404, f"deployment {dep_id} has no container")

    local_node = nodes_store.find_by_label(conn, "local")
    local_node_id = local_node.id if local_node else 0
    is_remote = dep.node_id != 0 and dep.node_id != local_node_id

    if is_remote:
        registry = request.app.state.agent_registry
        link = registry.get(dep.node_id)
        if link is None or not link.is_ready:
            raise HTTPException(
                503, f"node {dep.node_id} agent not connected; cannot stream logs",
            )

        async def gen_remote():
            yield ":ok\n\n"
            try:
                async for chunk in link.stream_logs(
                    container_id=dep.container_id, tail=500, follow=True,
                ):
                    if not chunk:
                        continue
                    if isinstance(chunk, bytes):
                        text = chunk.decode("utf-8", errors="replace")
                    else:
                        text = str(chunk)
                    for line in text.splitlines():
                        if line:
                            yield f"data: {line}\n\n"
            except Exception as e:
                yield f"data: [berth] log stream error: {e}\n\n"
            yield "data: [berth] log stream ended\n\n"

        return StreamingResponse(gen_remote(), media_type="text/event-stream")

    docker_client = request.app.state.manager._docker
    if docker_client is None:
        raise HTTPException(
            503,
            f"deployment {dep_id} is local but this leader is in "
            "control-plane only mode; cannot stream its logs",
        )

    async def gen():
        yield ":ok\n\n"
        try:
            sync_iter = docker_client.stream_logs(
                dep.container_id, follow=True, tail=500,
            )
        except Exception as e:
            yield f"data: [berth] failed to attach: {e}\n\n"
            return
        sentinel = object()
        while True:
            chunk = await asyncio.to_thread(next, sync_iter, sentinel)
            if chunk is sentinel:
                yield "data: [berth] log stream ended\n\n"
                return
            if isinstance(chunk, bytes):
                text = chunk.decode("utf-8", errors="replace")
            else:
                text = str(chunk)
            for line in text.splitlines():
                if line:
                    yield f"data: {line}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    bus = request.app.state.event_bus

    async def gen():
        async with bus.subscribe() as queue:
            yield ":ok\n\n"
            while True:
                try:
                    e = await asyncio.wait_for(queue.get(), timeout=15.0)
                    payload = _json.dumps({
                        "kind": e.kind, "payload": e.payload, "ts": e.ts,
                    })
                    yield f"data: {payload}\n\n"
                except TimeoutError:
                    yield ":hb\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/gpus")
def list_gpus():
    from berth.daemon import admin as admin_mod

    return [
        {
            "index": s.index,
            "memory_used_mb": s.memory_used_mb,
            "memory_total_mb": s.memory_total_mb,
            "gpu_util_pct": s.gpu_util_pct,
            "power_w": s.power_w,
        }
        for s in admin_mod._read_gpu_stats()
    ]


@router.get("/backends")
def list_backends(backends: dict[str, Backend] = Depends(get_backends)):
    return [
        {
            "name": name,
            "image_default": b.image_default,
            "supports_adapters": getattr(b, "supports_adapters", False),
        }
        for name, b in sorted(backends.items())
        if name != "adopted"
    ]
