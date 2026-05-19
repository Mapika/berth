from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from berth.auth.middleware import require_auth_dep
from berth.backends.base import Backend
from berth.cluster.agent_link import ENGINE_TIMEOUT
from berth.cluster.agent_registry import AgentRegistry
from berth.daemon.dispatch import open_upstream_stream
from berth.daemon.dispatch_errors import NodeUnreachableError
from berth.daemon.retry_dispatcher import dispatch_with_retry
from berth.lifecycle.adapter_router import (
    UnknownModel,
    ensure_adapter_loaded,
    find_deployment_for,
    rank_deployments_for,
    resolve_target,
)
from berth.routing.scorer import NodeSignals, RoutingRequest
from berth.store import adapters as ad_store
from berth.store import api_keys as _api_keys_store
from berth.store import deployment_adapters as da_store
from berth.store import deployments as dep_store
from berth.store import key_usage as _key_usage_store
from berth.store import models as model_store
from berth.store import service_routes as _route_store
from berth.store import usage_events as _usage_events_store

router = APIRouter()


# Allowlist of upstream response headers to forward to the external
# client. Anything else (Set-Cookie, CORS, Link, X-Frame-Options, etc.)
# is dropped. content-type is forwarded separately via the
# StreamingResponse media_type — keep it out of this set.
_FORWARDABLE_RESPONSE_HEADERS = {
    "cache-control",
    "content-encoding",
    "content-language",
    "etag",
    "last-modified",
    "x-request-id",
    "x-trace-id",
    "x-served-by",
}


# Default bounded queue depth between the upstream reader and the client
# writer. Overridable per-app via `app.state.sse_queue_depth`. Sized at
# 64 chunks — large enough that fast clients never block, small enough
# that a slow consumer pauses the engine within a few hundred ms instead
# of buffering the whole generation.
SSE_QUEUE_DEPTH_DEFAULT = 64

# End-of-stream sentinel for the bounded pipe (cannot be a real chunk
# value since chunks are bytes).
_END_OF_STREAM = object()


async def _bounded_pipe(body_iter, *, queue_depth: int):
    """Decouple an upstream body iterator from a slow downstream consumer
    via a bounded asyncio.Queue.

    The reader task pulls from `body_iter` and puts each chunk on a
    queue with `maxsize=queue_depth`. The generator yields chunks as
    fast as the caller awaits. When the queue is full the reader blocks
    on `put()` — backpressure flows back to the upstream stream and,
    through it, to the engine.

    Exceptions raised by `body_iter` are forwarded through the queue
    and re-raised in the generator so the proxy's existing finally
    block runs.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=queue_depth)

    async def reader():
        try:
            async for chunk in body_iter:
                await q.put(chunk)
        except BaseException as e:
            await q.put(e)
            return
        await q.put(_END_OF_STREAM)

    reader_task = asyncio.create_task(reader())
    try:
        while True:
            item = await q.get()
            if item is _END_OF_STREAM:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (Exception, asyncio.CancelledError):
            pass


def _build_signals_by_node(aggregator) -> dict[int, NodeSignals]:
    """Distill the aggregator's latest per-node samples into the
    NodeSignals shape the scorer consumes. p95 across a node is the
    max across its deployments — conservatively pessimistic, which is
    the right bias for routing."""
    out: dict[int, NodeSignals] = {}
    for node_id, sample in aggregator.snapshot().items():
        gpus = sample.get("gpus", [])
        mem_free = sum(
            max(0, int(g.get("mem_total_mb", 0)) - int(g.get("mem_used_mb", 0)))
            for g in gpus
        )
        in_flight = sum(
            int(d.get("in_flight", 0)) for d in sample.get("deployments", [])
        )
        p95 = max(
            (int(d.get("latency_p95_ms", 0))
             for d in sample.get("deployments", [])),
            default=0,
        )
        out[node_id] = NodeSignals(
            node_id=node_id, mem_free_mb=mem_free,
            in_flight=in_flight, latency_p95_ms=p95,
        )
    return out


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

    # Build per-node signals + affinity hint so the scorer picks the
    # best candidate (lower in-flight, warmer KV cache). Skipping the
    # routing setup when the aggregator/affinity aren't wired keeps
    # legacy single-app builds working with the find_deployment_for
    # legacy path.
    aggregator = getattr(request.app.state, "metrics_aggregator", None)
    affinity = getattr(request.app.state, "routing_affinity", None)
    signals_by_node: dict[int, NodeSignals] | None = None
    routing_request: RoutingRequest | None = None
    affinity_key: str | None = None
    if aggregator is not None:
        signals_by_node = _build_signals_by_node(aggregator)
        affinity_key = (
            request.headers.get("x-session-id")
            or request.headers.get("x-conversation-id")
            or (f"key:{key.id}" if key is not None else None)
        )
        affinity_node_id = None
        if affinity_key and affinity is not None:
            affinity_node_id = affinity.lookup(affinity_key)
        routing_request = RoutingRequest(
            affinity_key=affinity_key, affinity_node_id=affinity_node_id,
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
            signals_by_node=signals_by_node,
            request=routing_request,
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
        active = find_deployment_for(
            conn, target.base_model_name, target.adapter_name,
            signals_by_node=signals_by_node, request=routing_request,
        )

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

    # tracer.update with deployment_id happens AFTER dispatch_with_retry,
    # since retry may swap us to a different candidate. Backend is the
    # same for all candidates of the same model so we read it from the
    # head here.
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
                    models_dir=manager.models_dir,
                )
            except UnknownModel as e:
                tracer.finalize(trace, status_code=404, error=str(e))
                raise HTTPException(404, detail=str(e)) from e
            except RuntimeError as e:
                tracer.finalize(trace, status_code=502, error=f"adapter load failed: {e}")
                raise HTTPException(502, detail=f"adapter load failed: {e}") from e
    tracer.update(trace, cold_loaded=cold_loaded)

    request.app.state.request_count += 1
    _in_flight = getattr(request.app.state, "in_flight", None)
    _latency = getattr(request.app.state, "latency", None)

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

    # Determine the ranked candidate list and the dispatch budget.
    # Adapter requests stay single-shot: only the head deployment has
    # had the adapter loaded by ensure_adapter_loaded above, so falling
    # through to another candidate would dispatch to an engine without
    # the right LoRA slot. Bare-base requests use the full ranking and
    # the retry budget gets us through transient node-unreachable.
    if target.adapter_name:
        ranked_for_dispatch = [active]
        retry_budget = 0
    else:
        full_ranked = rank_deployments_for(
            conn, target.base_model_name, None,
            signals_by_node=signals_by_node or {},
            request=routing_request or RoutingRequest(affinity_key=None),
        )
        ranked_for_dispatch = full_ranked or [active]
        retry_budget = 2

    from berth.store import nodes as _nodes_store
    local_node = _nodes_store.find_by_label(conn, "local")
    local_node_id = local_node.id if local_node else 0
    registry: AgentRegistry = request.app.state.agent_registry
    engine_path = backend.openai_base + openai_subpath

    async def _open_for(candidate):
        upstream = await open_upstream_stream(
            deployment=candidate,
            local_node_id=local_node_id,
            registry=registry,
            method="POST",
            path=engine_path,
            headers=headers,
            body=body,
        )
        return (upstream, candidate)

    tracer.update(trace, dispatched_at=time.monotonic())
    try:
        upstream, landed = await dispatch_with_retry(
            ranked=ranked_for_dispatch,
            open_stream=_open_for,
            budget=retry_budget,
        )
    except NodeUnreachableError as e:
        tracer.finalize(trace, status_code=503, error=str(e))
        raise HTTPException(503, detail=str(e)) from e

    # Now we know which deployment we actually landed on. All bookkeeping
    # (in-flight, latency window, usage_event, affinity) is keyed off
    # `landed`, not the head of the ranking — important when the retry
    # path swapped to a fallback candidate.
    tracer.update(trace, deployment_id=landed.id, backend=landed.backend)
    dep_store.touch_last_request(conn, landed.id)
    if affinity_key and affinity is not None:
        affinity.set(affinity_key, node_id=landed.node_id)
    if _in_flight is not None:
        _in_flight.start(landed.id)
    _dispatch_started_at = time.monotonic()
    usage_event_id = _usage_events_store.record(
        conn,
        model_name=requested_model_name,
        base_name=target.base_model_name,
        adapter_name=target.adapter_name,
        deployment_id=landed.id,
        api_key_id=key.id if key is not None else None,
        cold_loaded=cold_loaded,
    )

    upstream_ct = upstream.headers.get(
        "content-type", "application/octet-stream",
    )
    usage_tracker = _UsageTracker(is_sse="event-stream" in upstream_ct)
    # Forward only an explicit allowlist of upstream response headers.
    # Blocklist semantics let a compromised/misconfigured engine inject
    # Set-Cookie / CORS / Link / X-Frame-Options into our public response.
    # content-type is forwarded separately as the StreamingResponse
    # media_type, so we drop it here.
    forward_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() in _FORWARDABLE_RESPONSE_HEADERS
    }

    queue_depth = getattr(
        request.app.state, "sse_queue_depth", SSE_QUEUE_DEPTH_DEFAULT,
    )

    async def streamer():
        first_byte_seen = False
        try:
            async for chunk in _bounded_pipe(
                upstream.body_iter, queue_depth=queue_depth,
            ):
                if not first_byte_seen:
                    first_byte_seen = True
                    tracer.update(trace, first_byte_at=time.monotonic())
                if chunk:
                    usage_tracker.feed(chunk)
                    yield chunk
        finally:
            await upstream.aclose()
            tin, tout = usage_tracker.extract()
            if tin or tout:
                _usage_events_store.set_tokens(
                    conn, usage_event_id, tokens_in=tin, tokens_out=tout,
                )
            if key is not None and key.usage_event_id is not None:
                _key_usage_store.set_tokens(
                    conn, key.usage_event_id, tokens_in=tin, tokens_out=tout,
                )
            if _in_flight is not None:
                _in_flight.finish(landed.id)
            if _latency is not None:
                _latency.record(
                    deployment_id=landed.id,
                    latency_ms=int(
                        (time.monotonic() - _dispatch_started_at) * 1000
                    ),
                    error=upstream.status >= 500,
                )
            tracer.finalize(
                trace, status_code=upstream.status,
                tokens_in=tin, tokens_out=tout,
            )

    return StreamingResponse(
        streamer(),
        status_code=upstream.status,
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
            "owned_by": "berth",
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
            "owned_by": "berth",
            "base": a.base_model.name,
            "loaded": bool(da_store.find_deployments_with_adapter(conn, a.id)),
            "downloaded": a.local_path is not None,
        }
        for a in ad_store.list_all(conn)
    ]
    return {"object": "list", "data": base_entries + adapter_entries}
