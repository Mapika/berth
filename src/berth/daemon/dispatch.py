"""Open the upstream stream for one chosen deployment.

Carved out of openai_proxy._proxy so it can sit behind dispatch_with_retry.
The unit knows nothing about request context, usage tracking, the tracer,
or in-flight counters — those stay in the proxy and wrap the body
iterator the unit returns.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from berth.cluster.agent_registry import AgentRegistry
from berth.daemon.dispatch_errors import (
    NodeUnreachableError,
    UpstreamHttpError,
    classify_pre_first_byte_status,
)


@dataclass
class UpstreamOpen:
    """Result of open_upstream_stream — returned once status + headers
    are known, before any body byte has been consumed downstream.

    The caller is responsible for draining `body_iter` and calling
    `aclose()` in a finally block.
    """
    status: int
    headers: dict[str, str]
    body_iter: AsyncIterator[bytes]
    aclose: Callable[[], Awaitable[None]]


async def open_upstream_stream(
    *,
    deployment: Any,           # dep_store.Deployment
    local_node_id: int,
    registry: AgentRegistry,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    engine_client_factory: Callable[[str], httpx.AsyncClient] | None = None,
) -> UpstreamOpen:
    """Open the upstream stream for one deployment.

    Local deployment → direct httpx stream to the container.
    Remote deployment → AgentLink proxy_request through the WS tunnel.

    Raises NodeUnreachableError before any byte is yielded when the
    chosen node has no live, ready AgentLink — retry-safe.
    """
    is_remote = (
        deployment.node_id != 0 and deployment.node_id != local_node_id
    )
    if is_remote:
        link = registry.get(deployment.node_id)
        if link is None or not link.is_ready:
            raise NodeUnreachableError(node_id=deployment.node_id)
        if deployment.container_id is None:
            raise NodeUnreachableError(node_id=deployment.node_id)
        opened = await _open_remote(link, deployment, method, path, headers, body)
    else:
        opened = await _open_local(
            deployment, method, path, headers, body,
            engine_client_factory=engine_client_factory,
        )
    # Convert retryable 5xx into an exception so dispatch_with_retry sees it.
    # The body iterator hasn't been consumed yet — no bytes have left the
    # daemon. Close the upstream before raising to avoid leaking the
    # underlying client.
    if classify_pre_first_byte_status(opened.status) is not None:
        try:
            await opened.aclose()
        except Exception:
            pass  # nosec
        raise UpstreamHttpError(status=opened.status)
    return opened


async def _open_remote(
    link, deployment, method: str, path: str,
    headers: dict[str, str], body: bytes,
) -> UpstreamOpen:
    agen = link.proxy_request(
        container_id=deployment.container_id,
        method=method, path=path, headers=headers, body=body,
    )
    first_chunk = None
    status = 502
    upstream_headers: dict[str, str] = {}
    async for ch in agen:
        if ch.status is not None:
            status = ch.status
        if ch.headers is not None:
            upstream_headers = dict(ch.headers)
        first_chunk = ch
        break

    async def body_iter() -> AsyncIterator[bytes]:
        if first_chunk is not None and first_chunk.body:
            yield first_chunk.body
        if first_chunk is None or not first_chunk.eof:
            async for ch in agen:
                if ch.body:
                    yield ch.body
                if ch.eof:
                    break

    async def aclose() -> None:
        # The proxy_request iterator drops on the consumer side; nothing
        # explicit to close.
        return None

    return UpstreamOpen(
        status=status, headers=upstream_headers,
        body_iter=body_iter(), aclose=aclose,
    )


async def _open_local(
    deployment, method: str, path: str,
    headers: dict[str, str], body: bytes,
    *,
    engine_client_factory: Callable[[str], httpx.AsyncClient] | None = None,
) -> UpstreamOpen:
    base = (
        f"http://{deployment.container_address}:{deployment.container_port}"
    )
    if engine_client_factory is not None:
        client = engine_client_factory(base)
    else:
        # Inline import to avoid the circular dep with openai_proxy at
        # module load time.
        from berth.daemon.openai_proxy import make_engine_client
        client = make_engine_client(base)
    stream_cm = client.stream(method, path, content=body, headers=headers)
    resp = await stream_cm.__aenter__()

    async def body_iter() -> AsyncIterator[bytes]:
        async for chunk in resp.aiter_raw():
            yield chunk

    async def aclose() -> None:
        await stream_cm.__aexit__(None, None, None)
        await client.aclose()

    return UpstreamOpen(
        status=resp.status_code,
        headers=dict(resp.headers),
        body_iter=body_iter(),
        aclose=aclose,
    )
