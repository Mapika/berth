from __future__ import annotations

import json
import sqlite3
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from serve_engine.auth.middleware import require_auth_dep
from serve_engine.backends.base import Backend
from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.lifecycle.adapter_router import (
    UnknownModel,
    ensure_adapter_loaded,
    find_deployment_for,
    resolve_target,
)
from serve_engine.store import adapters as ad_store
from serve_engine.store import api_keys as _api_keys_store
from serve_engine.store import deployment_adapters as da_store
from serve_engine.store import deployments as dep_store
from serve_engine.store import key_usage as _key_usage_store
from serve_engine.store import models as model_store
from serve_engine.store import service_routes as _route_store
from serve_engine.store import usage_events as _usage_events_store

router = APIRouter()

ENGINE_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0)


def make_engine_client(base_url: str) -> httpx.AsyncClient:
    """Factory wrapper so tests can monkeypatch transport."""
    return httpx.AsyncClient(base_url=base_url, timeout=ENGINE_TIMEOUT)


async def _proxy_via_link(
    *,
    registry: AgentRegistry,
    node_id: int,
    container_id: str,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
) -> tuple[int, dict[str, str], list[bytes]]:
    """Materialize the proxy response into a list. Used by tests to verify
    the routing contract without exercising the FastAPI streaming response
    plumbing. The route handler streams via the same `proxy_request`
    generator directly."""
    link = registry.get(node_id)
    if link is None or not link.is_ready:
        raise RuntimeError(f"node {node_id} not connected")
    status_code: int | None = None
    out_headers: dict[str, str] = {}
    body_chunks: list[bytes] = []
    async for chunk in link.proxy_request(
        container_id=container_id,
        method=method,
        path=path,
        headers=headers,
        body=body,
    ):
        if chunk.status is not None and status_code is None:
            status_code = chunk.status
        if chunk.headers is not None and not out_headers:
            out_headers = dict(chunk.headers)
        if chunk.body:
            body_chunks.append(chunk.body)
        if chunk.eof:
            break
    if status_code is None:
        raise RuntimeError("agent returned no status chunk")
    out_headers.pop("content-length", None)
    return status_code, out_headers, body_chunks


async def _proxy(
    request: Request,
    openai_subpath: str,
    *,
    key: _api_keys_store.ApiKey | None,
) -> StreamingResponse:
    conn: sqlite3.Connection = request.app.state.conn
    backends: dict[str, Backend] = request.app.state.backends
    tracer = request.app.state.request_tracer
    trace = tracer.start(method=request.method, path=request.url.path)
    if key is not None:
        tracer.update(trace, api_key_id=key.id, api_key_name=key.name)

    body = await request.body()
    model_name: str | None = None
    try:
        parsed = json.loads(body) if body else {}
        if isinstance(parsed, dict):
            model_name = parsed.get("model")
    except json.JSONDecodeError:
        pass

    if not model_name:
        tracer.finalize(trace, status_code=400, error="missing model")
        raise HTTPException(400, detail="request body must include 'model'")
    tracer.update(trace, model_requested=model_name)

    # Optional per-key model allowlist (migration 013).
    # - key is None: UDS request or no-keys-registered bypass; skip the check.
    # - key.allowed_models is None: unrestricted key; skip the check.
    # - key.allowed_models == []: deny-all; the loop below rejects every model.
    # - key.allowed_models == [...]: restrict to listed names.
    # The check fires on the user-facing requested model name (the one the
    # client wrote in the request body), NOT the resolved upstream target -
    # otherwise a route alias would silently bypass the allowlist.
    if key is not None and key.allowed_models is not None:
        if model_name not in key.allowed_models:
            tracer.finalize(trace, status_code=403, error="model not in key allowlist")
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=(
                    f"key {key.name!r} does not have access to model {model_name!r}"
                ),
            )

    requested_model_name = model_name
    route = _route_store.find_enabled_for_model(conn, requested_model_name)
    candidate_model_names = [requested_model_name]
    if route is not None:
        candidate_model_names = [route.target_model_name]
        if route.fallback_model_name is not None:
            candidate_model_names.append(route.fallback_model_name)
    tracer.update(
        trace,
        route_resolved_at=time.monotonic(),
        route_name=route.name if route else None,
        profile_name=route.profile_name if route else None,
        target_model=route.target_model_name if route else None,
    )

    # Resolve `model` to (base, optional adapter). Bare base names route
    # exactly as v1 did. Adapter names cause us to (a) pick a deployment
    # of the adapter's base that has the adapter loaded or can hot-load
    # it, and (b) rewrite the upstream payload's `model` to the adapter
    # name so vLLM/SGLang dispatch against the right LoRA slot.
    target = None
    active = None
    routed_model_name = requested_model_name
    unknown_error: UnknownModel | None = None
    for candidate_model_name in candidate_model_names:
        try:
            candidate_target = resolve_target(conn, candidate_model_name)
        except UnknownModel as e:
            unknown_error = e
            continue
        candidate = find_deployment_for(
            conn,
            candidate_target.base_model_name,
            candidate_target.adapter_name,
        )
        if candidate is not None and candidate.container_address is not None:
            target = candidate_target
            active = candidate
            routed_model_name = candidate_model_name
            break

    if target is None:
        if route is not None:
            tracer.finalize(trace, status_code=503, error="route has no ready service")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"route {route.name!r} for model {requested_model_name!r} "
                    "has no ready primary or fallback service"
                ),
            )
        if unknown_error is not None:
            tracer.finalize(trace, status_code=404, error=str(unknown_error))
            raise HTTPException(404, detail=str(unknown_error)) from unknown_error
        try:
            target = resolve_target(conn, requested_model_name)
        except UnknownModel as e:
            tracer.finalize(trace, status_code=404, error=str(e))
            raise HTTPException(404, detail=str(e)) from e
        active = find_deployment_for(conn, target.base_model_name, target.adapter_name)

    if active is None or active.container_address is None:
        if target.adapter_name:
            detail = (
                f"no ready deployment of base {target.base_model_name!r} "
                f"with --max-loras > 0 for adapter {target.adapter_name!r}"
            )
        else:
            detail = f"no ready deployment for model {requested_model_name!r}"
        tracer.finalize(trace, status_code=503, error=detail)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)

    tracer.update(trace, deployment_id=active.id, backend=active.backend)
    backend = backends.get(active.backend)
    if backend is None:
        tracer.finalize(trace, status_code=500, error=f"unknown backend {active.backend!r}")
        raise HTTPException(500, detail=f"unknown backend {active.backend!r}")

    # If adapter requested, ensure it's loaded into the chosen deployment.
    # This is the hot-load path: ~100-500ms for the first request to a
    # given adapter; sub-second on subsequent requests (already loaded).
    cold_loaded = False
    if target.adapter_name:
        manager = request.app.state.manager
        async with manager.adapter_lock(active.id):
            try:
                cold_loaded = await ensure_adapter_loaded(
                    conn, backend, active, target.adapter_name,
                    models_dir=manager._models_dir,
                )
            except UnknownModel as e:
                tracer.finalize(trace, status_code=404, error=str(e))
                raise HTTPException(404, detail=str(e)) from e
            except RuntimeError as e:
                tracer.finalize(trace, status_code=502, error=f"adapter load failed: {e}")
                raise HTTPException(502, detail=f"adapter load failed: {e}") from e
    tracer.update(trace, cold_loaded=cold_loaded)

    dep_store.touch_last_request(conn, active.id)
    request.app.state.request_count += 1
    # Log this request for the predictor. Tokens are 0 at dispatch time,
    # then patched in below via set_tokens once the upstream
    # stream completes and the usage tracker extracts them.
    usage_event_id = _usage_events_store.record(
        conn,
        model_name=requested_model_name,
        base_name=target.base_model_name,
        adapter_name=target.adapter_name,
        deployment_id=active.id,
        api_key_id=key.id if key is not None else None,
        cold_loaded=cold_loaded,
    )

    # Rewrite the upstream payload's `model` field to the adapter name
    # when an adapter is in play - vLLM/SGLang both treat the OpenAI
    # `model` field as the LoRA slot name when --enable-lora is on.
    upstream_model_name = target.adapter_name or target.base_model_name
    if upstream_model_name != requested_model_name or routed_model_name != requested_model_name:
        try:
            parsed["model"] = upstream_model_name
            body = json.dumps(parsed).encode()
        except (TypeError, json.JSONDecodeError):
            pass  # body already validated as JSON above; should not happen

    _HOP_BY_HOP = {"host", "content-length", "transfer-encoding", "connection"}
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    # Strip the user's Authorization (don't leak the API key to the engine).
    headers.pop("authorization", None)
    headers.pop("Authorization", None)

    tracer.update(trace, dispatched_at=time.monotonic())

    # Decide between direct-HTTP and tunneled-WS dispatch by node_id.
    # Local node → existing direct httpx path (lowest overhead).
    # Remote node → stream chunks through the AgentLink's WS.
    from serve_engine.store import nodes as _nodes_store
    local_node = _nodes_store.find_by_label(conn, "local")
    local_node_id = local_node.id if local_node else 0
    is_remote = (
        active.node_id != 0
        and active.node_id != local_node_id
    )

    if is_remote:
        registry: AgentRegistry = request.app.state.agent_registry
        link = registry.get(active.node_id)
        if link is None or not link.is_ready:
            tracer.finalize(
                trace, status_code=503,
                error=f"node {active.node_id} not connected",
            )
            raise HTTPException(
                503, detail=f"node {active.node_id} agent not connected",
            )
        if active.container_id is None:
            tracer.finalize(trace, status_code=503, error="no container id")
            raise HTTPException(503, detail="deployment has no container yet")

        engine_path = backend.openai_base + openai_subpath
        usage_tracker = _UsageTracker(is_sse=True)  # tunnelled-stream assume SSE

        async def remote_streamer():
            first_byte_seen = False
            status_code = 502
            try:
                async for ch in link.proxy_request(
                    container_id=active.container_id,
                    method="POST",
                    path=engine_path,
                    headers=headers,
                    body=body,
                ):
                    if ch.status is not None:
                        status_code = ch.status
                    if not first_byte_seen and ch.body:
                        first_byte_seen = True
                        tracer.update(trace, first_byte_at=time.monotonic())
                    if ch.body:
                        usage_tracker.feed(ch.body)
                        yield ch.body
                    if ch.eof:
                        break
            finally:
                tin, tout = usage_tracker.extract()
                if tin or tout:
                    _usage_events_store.set_tokens(
                        conn, usage_event_id, tokens_in=tin, tokens_out=tout,
                    )
                if key is not None and key.usage_event_id is not None:
                    _key_usage_store.set_tokens(
                        conn, key.usage_event_id,
                        tokens_in=tin, tokens_out=tout,
                    )
                tracer.finalize(
                    trace, status_code=status_code,
                    tokens_in=tin, tokens_out=tout,
                )
        return StreamingResponse(
            remote_streamer(),
            status_code=200,  # tunnelled — actual status surfaces in stream
            media_type="text/event-stream",
        )

    # ----------- Local dispatch (direct httpx) -----------------------
    base = f"http://{active.container_address}:{active.container_port}{backend.openai_base}"
    # Open the upstream stream BEFORE returning so we can read its status
    # and headers and forward them faithfully to the caller.
    client = make_engine_client(base)
    stream_cm = client.stream("POST", openai_subpath, content=body, headers=headers)
    resp = await stream_cm.__aenter__()

    # Forward upstream status + content-type + selected headers.
    upstream_ct = resp.headers.get("content-type", "application/octet-stream")
    forward_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in _HOP_BY_HOP | {"content-type"}
    }

    # Track only the last SSE event's `data:` payload, or the entire body if
    # it's a single JSON object. ~4 KB cap on the last-line buffer is plenty
    # for usage payloads which are tens of bytes.
    usage_tracker = _UsageTracker(is_sse="event-stream" in upstream_ct)

    async def streamer():
        first_byte_seen = False
        try:
            async for chunk in resp.aiter_raw():
                if not first_byte_seen:
                    first_byte_seen = True
                    tracer.update(trace, first_byte_at=time.monotonic())
                usage_tracker.feed(chunk)
                yield chunk
        finally:
            await stream_cm.__aexit__(None, None, None)
            await client.aclose()
            tin, tout = usage_tracker.extract()
            if tin or tout:
                _usage_events_store.set_tokens(
                    conn, usage_event_id, tokens_in=tin, tokens_out=tout,
                )
            if key is not None and key.usage_event_id is not None:
                _key_usage_store.set_tokens(
                    conn, key.usage_event_id, tokens_in=tin, tokens_out=tout,
                )
            tracer.finalize(
                trace,
                status_code=resp.status_code,
                tokens_in=tin,
                tokens_out=tout,
            )

    return StreamingResponse(
        streamer(),
        status_code=resp.status_code,
        headers=forward_headers,
        media_type=upstream_ct,
    )


class _UsageTracker:
    """Best-effort token-count extraction without buffering the full response.

    - SSE mode: keep the last complete `data: {...}` event; OpenAI/vLLM/SGLang
      emit usage in the final event when stream_options.include_usage=true.
    - JSON mode: buffer up to a small cap (single JSON response). Most non-
      streaming `/v1/chat/completions` bodies are well under 64 KB.
    """

    _MAX_JSON = 65_536

    def __init__(self, *, is_sse: bool):
        self._is_sse = is_sse
        self._last_event = bytearray()
        self._current = bytearray()
        self._json_buf = bytearray()
        self._json_overflow = False

    def feed(self, chunk: bytes) -> None:
        if self._is_sse:
            self._current.extend(chunk)
            # An event ends with a blank line (\n\n). When we see one,
            # the bytes before the blank line are the most recent event.
            # Keep only events that carry a usage payload - providers
            # like OpenAI/vLLM/SGLang emit the usage chunk BEFORE the
            # terminal `data: [DONE]` frame, so blindly tracking the
            # last event loses the tokens.
            while True:
                idx = self._current.find(b"\n\n")
                if idx < 0:
                    break
                event = bytes(self._current[:idx])
                del self._current[: idx + 2]
                if b"data:" in event and b'"usage"' in event:
                    self._last_event = bytearray(event)
        else:
            if not self._json_overflow:
                remaining = self._MAX_JSON - len(self._json_buf)
                if remaining <= 0:
                    self._json_overflow = True
                else:
                    self._json_buf.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        self._json_overflow = True

    def extract(self) -> tuple[int, int]:
        if self._is_sse:
            payload = self._last_event
            if not payload:
                return 0, 0
            # Strip lines that don't start with `data:`
            for line in payload.split(b"\n"):
                if line.startswith(b"data:"):
                    body = line[len(b"data:"):].strip()
                    if body == b"[DONE]" or not body:
                        continue
                    try:
                        obj = json.loads(body)
                        u = obj.get("usage") or {}
                        return int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
                    except (json.JSONDecodeError, AttributeError):
                        continue
            return 0, 0
        # JSON mode
        if self._json_overflow or not self._json_buf:
            return 0, 0
        try:
            obj = json.loads(bytes(self._json_buf))
            u = obj.get("usage") or {}
            return int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        except (json.JSONDecodeError, AttributeError):
            return 0, 0


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    key: _api_keys_store.ApiKey | None = Depends(require_auth_dep),
):
    return await _proxy(request, "/chat/completions", key=key)


@router.post("/v1/completions")
async def completions(
    request: Request,
    key: _api_keys_store.ApiKey | None = Depends(require_auth_dep),
):
    return await _proxy(request, "/completions", key=key)


@router.post("/v1/embeddings")
async def embeddings(
    request: Request,
    key: _api_keys_store.ApiKey | None = Depends(require_auth_dep),
):
    return await _proxy(request, "/embeddings", key=key)


@router.get("/v1/models")
def models(
    request: Request,
    _key: _api_keys_store.ApiKey | None = Depends(require_auth_dep),
):
    conn: sqlite3.Connection = request.app.state.conn
    ready_by_model: dict[int, dep_store.Deployment] = {}
    for d in dep_store.list_ready(conn):
        ready_by_model[d.model_id] = d
    rows = model_store.list_all(conn)
    base_entries = [
        {
            "id": m.name,
            "object": "model",
            "owned_by": "serve-engine",
            "loaded": m.id in ready_by_model,
            "pinned": ready_by_model[m.id].pinned if m.id in ready_by_model else False,
        }
        for m in rows
    ]
    # Adapters appear alongside base models - clients can `model=<adapter>`
    # directly. `base` field disambiguates for clients that want the parent.
    adapter_entries = [
        {
            "id": a.name,
            "object": "model",
            "owned_by": "serve-engine",
            "base": a.base_model.name,
            "loaded": bool(da_store.find_deployments_with_adapter(conn, a.id)),
            "downloaded": a.local_path is not None,
        }
        for a in ad_store.list_all(conn)
    ]
    return {"object": "list", "data": base_entries + adapter_entries}
