from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.cluster.protocol import (
    Heartbeat,
    Hello,
    Welcome,
    decode_frame,
    encode_frame,
)
from serve_engine.cluster.remote_agent import RemoteAgentLink
from serve_engine.store import nodes as nodes_store

log = logging.getLogger(__name__)


FingerprintResolver = Callable[[WebSocket], str | None]


def _default_fingerprint_resolver(ws: WebSocket) -> str | None:
    """Default policy: trust the operator's reverse proxy to verify the
    client certificate and forward its sha256 in `x-serve-client-fingerprint`.
    A follow-up plan will add direct uvicorn-TLS termination."""
    return ws.headers.get("x-serve-client-fingerprint")


class _WSAdapter:
    """Adapt FastAPI WebSocket to the duck type RemoteAgentLink expects."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send(self, msg: str) -> None:
        await self._ws.send_text(msg)

    async def recv(self) -> str | None:
        try:
            return await self._ws.receive_text()
        except WebSocketDisconnect:
            return None


class LeaderHub:
    """FastAPI WebSocket endpoint that accepts agent connections, verifies
    their cert fingerprint against the `nodes` table, completes a
    Hello/Welcome handshake, and registers a RemoteAgentLink with the
    AgentRegistry for the duration of the connection."""

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        registry: AgentRegistry,
        fingerprint_resolver: FingerprintResolver = _default_fingerprint_resolver,
    ) -> None:
        self._conn = conn
        self._registry = registry
        self._resolve_fp = fingerprint_resolver
        self.router = APIRouter()
        self.router.add_api_websocket_route("/cluster/agent", self._handle_agent)

    async def _handle_agent(self, ws: WebSocket) -> None:
        fp = self._resolve_fp(ws)
        if fp is None:
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        node = nodes_store.find_by_fingerprint(self._conn, fp)
        if node is None:
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await ws.accept()
        try:
            hello_text = await ws.receive_text()
            hello = decode_frame(hello_text)
        except (WebSocketDisconnect, ValueError):
            return

        if not isinstance(hello, Hello):
            await ws.close(code=status.WS_1003_UNSUPPORTED_DATA)
            return

        now = time.time()
        nodes_store.update_inventory(
            self._conn, node.id,
            agent_version=hello.agent_version,
            cpu_count=int(hello.host_info.get("cpu_count", 0)),
            total_ram_mb=int(hello.host_info.get("total_ram_mb", 0)),
            gpu_count=int(hello.host_info.get("gpu_count", 0)),
            total_vram_mb=int(hello.host_info.get("total_vram_mb", 0)),
        )
        nodes_store.set_status(self._conn, node.id, status="ready", last_seen=now)
        try:
            await ws.send_text(encode_frame(
                Welcome(node_id=node.id, server_time=now)
            ))
        except WebSocketDisconnect:
            return

        link = RemoteAgentLink(node_id=node.id, ws=_WSAdapter(ws))
        self._registry.register(link)
        try:
            while True:
                try:
                    raw = await ws.receive_text()
                except WebSocketDisconnect:
                    break
                try:
                    frame = decode_frame(raw)
                except ValueError:
                    log.warning("dropping malformed frame from node %s", node.id)
                    continue
                if isinstance(frame, Heartbeat):
                    nodes_store.set_status(
                        self._conn, node.id,
                        status="ready", last_seen=time.time(),
                    )
                    continue
                await link.inbound(frame)
        finally:
            link.shutdown()
            self._registry.unregister(node.id)
            nodes_store.set_status(
                self._conn, node.id,
                status="unreachable", last_seen=time.time(),
            )
