from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import AsyncIterator
from typing import Any, Protocol

from serve_engine.cluster.agent_link import ProxyResponseChunk, StartedContainer
from serve_engine.cluster.protocol import (
    Frame,
    HttpCancel,
    HttpChunk,
    HttpRequest,
    OpResult,
    StartDeployment,
    StopDeployment,
    decode_frame,
    encode_frame,
)


class _WSProto(Protocol):
    """Minimal duck type covering FastAPI's WebSocket and the websockets
    client used by the agent process."""
    async def send(self, msg: str) -> None: ...
    async def recv(self) -> str | None: ...


_END_OF_STREAM = HttpChunk(stream_id="", body_b64="", eof=True)


class RemoteAgentLink:
    """AgentLink implementation backed by a long-lived WebSocket the agent
    opened to the leader. start_deployment/stop_deployment are RPC calls
    that await a matching OpResult; proxy_request is a streaming RPC that
    multiplexes per-request virtual streams over the same WS."""

    def __init__(self, *, node_id: int, ws: _WSProto) -> None:
        self._node_id = node_id
        self._ws = ws
        self._pending_ops: dict[str, asyncio.Future[OpResult]] = {}
        self._streams: dict[str, asyncio.Queue[HttpChunk]] = {}
        self._send_lock = asyncio.Lock()
        self._shutdown = False
        self._ready = True

    @property
    def node_id(self) -> int:
        return self._node_id

    @property
    def is_ready(self) -> bool:
        return self._ready and not self._shutdown

    def shutdown(self) -> None:
        self._shutdown = True
        self._ready = False
        for fut in self._pending_ops.values():
            if not fut.done():
                fut.set_exception(ConnectionError("agent disconnected"))
        self._pending_ops.clear()
        for q in self._streams.values():
            q.put_nowait(_END_OF_STREAM)

    async def _send(self, frame: Frame) -> None:
        async with self._send_lock:
            await self._ws.send(encode_frame(frame))

    async def inbound(self, frame: Frame) -> None:
        """Dispatch one inbound frame. Public so LeaderHub can intercept
        Heartbeats before routing the rest here."""
        if isinstance(frame, OpResult):
            fut = self._pending_ops.pop(frame.request_id, None)
            if fut is not None and not fut.done():
                fut.set_result(frame)
        elif isinstance(frame, HttpChunk):
            q = self._streams.get(frame.stream_id)
            if q is not None:
                await q.put(frame)

    async def run(self) -> None:
        """Consume frames from the WS forever (until shutdown or close).
        Used directly when the WS adapter is full duplex; LeaderHub uses
        `inbound()` directly to special-case heartbeats."""
        try:
            while not self._shutdown:
                raw = await self._ws.recv()
                if raw is None:
                    break
                try:
                    frame = decode_frame(raw)
                except ValueError:
                    continue
                await self.inbound(frame)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.shutdown()

    async def _request_op(self, frame: Frame, request_id: str) -> OpResult:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[OpResult] = loop.create_future()
        self._pending_ops[request_id] = fut
        await self._send(frame)
        return await fut

    async def start_deployment(self, plan: dict[str, Any]) -> StartedContainer:
        rid = secrets.token_hex(8)
        res = await self._request_op(
            StartDeployment(request_id=rid, plan=plan), rid,
        )
        if not res.ok or res.data is None:
            raise RuntimeError(res.error or "start_deployment failed")
        return StartedContainer(
            container_id=res.data["container_id"],
            address=res.data.get("address", "tunnel"),
            port=int(res.data.get("port", 0)),
        )

    async def stop_deployment(
        self, container_id: str, *, remove: bool = True,
    ) -> None:
        rid = secrets.token_hex(8)
        res = await self._request_op(
            StopDeployment(request_id=rid, container_id=container_id), rid,
        )
        if not res.ok:
            raise RuntimeError(res.error or "stop_deployment failed")

    async def proxy_request(
        self,
        *,
        container_id: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> AsyncIterator[ProxyResponseChunk]:
        stream_id = secrets.token_hex(8)
        q: asyncio.Queue[HttpChunk] = asyncio.Queue()
        self._streams[stream_id] = q
        try:
            await self._send(HttpRequest(
                stream_id=stream_id,
                method=method,
                path=path,
                headers={**headers, "x-serve-container-id": container_id},
                body_b64=base64.b64encode(body).decode("ascii"),
            ))
            first = True
            while True:
                chunk = await q.get()
                body_bytes = (
                    base64.b64decode(chunk.body_b64) if chunk.body_b64 else b""
                )
                yield ProxyResponseChunk(
                    status=chunk.status if first else None,
                    headers=chunk.headers if first else None,
                    body=body_bytes,
                    eof=chunk.eof,
                )
                first = False
                if chunk.eof:
                    break
        finally:
            self._streams.pop(stream_id, None)
            # Best-effort cancel notice; ignore failures (link may be down).
            if not self._shutdown:
                try:
                    await self._send(HttpCancel(stream_id=stream_id))
                except Exception:
                    pass
