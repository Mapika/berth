"""Body-size cap for externally reachable listeners.

Without one, a single multi-GB POST to /v1/chat/completions OOMs a small
VPS — `await request.body()` in the proxy buffers the whole payload.
"""
from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodySizeLimitMiddleware:
    """413 if the request body exceeds `max_bytes`.

    Rejects oversized Content-Length before reading, and counts streamed
    bodies without Content-Length so chunked uploads cannot bypass the cap
    and reach a downstream `await request.body()` buffer.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header_map = {
            k.lower(): v for k, v in scope.get("headers", [])
        }
        cl = header_map.get(b"content-length")
        if cl is not None:
            try:
                declared = int(cl.decode("ascii"))
            except ValueError:
                declared = -1
            if declared > self._max_bytes:
                await self._reject(scope, receive, send, size=declared)
                return

        replay: list[Message] = []
        received = 0
        while True:
            message = await receive()
            replay.append(message)
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    await self._reject(scope, receive, send, size=received)
                    return
                if not message.get("more_body", False):
                    break
            else:
                break

        async def replay_receive() -> Message:
            if replay:
                return replay.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        size: int,
    ) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "detail": (
                    f"request body of {size} bytes exceeds the "
                    f"{self._max_bytes}-byte cap"
                ),
            },
        )
        await response(scope, receive, send)
