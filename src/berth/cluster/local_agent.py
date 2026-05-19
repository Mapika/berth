from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

from berth.cluster.agent_link import (
    ENGINE_TIMEOUT,
    ProxyResponseChunk,
    StartedContainer,
)
from berth.lifecycle.docker_client import DockerClient


class LocalAgentLink:
    """In-process AgentLink. Uses the existing DockerClient on the leader host
    and httpx for direct loopback proxying — preserves today's single-node
    behavior under the unified AgentLink interface."""

    def __init__(
        self,
        *,
        node_id: int,
        docker_client: DockerClient,
        transport_for_test: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._node_id = node_id
        self._docker = docker_client
        self._endpoints: dict[str, tuple[str, int]] = {}
        self._transport = transport_for_test

    @property
    def node_id(self) -> int:
        return self._node_id

    @property
    def is_ready(self) -> bool:
        return True

    async def start_deployment(self, plan: dict[str, Any]) -> StartedContainer:
        h = await asyncio.to_thread(
            self._docker.run,
            image=plan["image"],
            name=plan["name"],
            command=plan["command"],
            environment=plan["environment"],
            kwargs=plan["kwargs"],
            volumes=plan["volumes"],
            internal_port=plan["internal_port"],
        )
        self._endpoints[h.id] = (h.address, h.port)
        return StartedContainer(
            container_id=h.id, address=h.address, port=h.port,
        )

    async def stop_deployment(
        self, container_id: str, *, remove: bool = True,
    ) -> None:
        await asyncio.to_thread(self._docker.stop, container_id, remove=remove)
        self._endpoints.pop(container_id, None)

    async def proxy_request(
        self,
        *,
        container_id: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> AsyncIterator[ProxyResponseChunk]:
        endpoint = self._endpoints.get(container_id)
        if endpoint is None:
            raise KeyError(f"no endpoint for container {container_id!r}")
        addr, port = endpoint
        base = f"http://{addr}:{port}"
        client = httpx.AsyncClient(
            base_url=base,
            transport=self._transport,
            timeout=ENGINE_TIMEOUT,
        )
        try:
            async with client.stream(method, path, headers=headers, content=body) as resp:
                first = True
                async for chunk in resp.aiter_raw():
                    yield ProxyResponseChunk(
                        status=resp.status_code if first else None,
                        headers=dict(resp.headers) if first else None,
                        body=chunk,
                        eof=False,
                    )
                    first = False
                yield ProxyResponseChunk(
                    status=resp.status_code if first else None,
                    headers=dict(resp.headers) if first else None,
                    body=b"",
                    eof=True,
                )
        finally:
            await client.aclose()

    def register_endpoint(
        self, *, container_id: str, address: str, port: int,
    ) -> None:
        """Wire an endpoint back in for a deployment started before this
        link instance existed (e.g., after a daemon process restart that
        rediscovered a still-running container)."""
        self._endpoints[container_id] = (address, port)

    async def probe_container(
        self, *, container_id: str, path: str,
    ) -> int:
        endpoint = self._endpoints.get(container_id)
        if endpoint is None:
            return 0
        addr, port = endpoint
        async with httpx.AsyncClient(
            base_url=f"http://{addr}:{port}",
            transport=self._transport,
            timeout=httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=2.0),
        ) as c:
            try:
                r = await c.get(path)
                return r.status_code
            except httpx.HTTPError:
                return 0
