from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

# Shared httpx timeout for engine-proxy traffic: long connect for cold
# starts, no read deadline (streaming responses can run for minutes),
# bounded write/pool. Used by every component that talks HTTP to an
# engine container — direct proxy, LocalAgentLink, and the agent's
# internal _HttpxAdapter.
ENGINE_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0)


@dataclass(frozen=True)
class StartedContainer:
    container_id: str
    address: str   # 'tunnel' sentinel for tunneled mode; LAN host for direct mode
    port: int      # for direct mode; 0 if tunneled


@dataclass(frozen=True)
class ProxyResponseChunk:
    """One chunk of an /v1/* response streamed back from an agent.

    The first chunk carries `status` and `headers`; subsequent chunks set
    those to None and carry only `body`. The last chunk has `eof=True` and
    typically empty `body`.
    """
    status: int | None
    headers: dict[str, str] | None
    body: bytes
    eof: bool


class AgentLink(Protocol):
    """Common interface for in-process and remote agents.

    LifecycleManager and openai_proxy depend only on this interface; the
    concrete implementations are LocalAgentLink (in-process, wraps
    DockerClient) and RemoteAgentLink (WS-backed).
    """

    @property
    def node_id(self) -> int: ...
    @property
    def is_ready(self) -> bool: ...

    async def start_deployment(self, plan: dict[str, Any]) -> StartedContainer: ...
    async def stop_deployment(
        self, container_id: str, *, remove: bool = True,
    ) -> None: ...
    def proxy_request(
        self,
        *,
        container_id: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> AsyncIterator[ProxyResponseChunk]: ...
    async def probe_container(
        self, *, container_id: str, path: str,
    ) -> int:
        """Do a single GET against the container's HTTP, return the status
        code. Used by HealthMonitor so remote engines can be probed
        through the tunnel (no leader-side reachability needed)."""
        ...
