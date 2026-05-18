"""WebSocket protocol subclass that exposes the TLS peer cert to ASGI.

Uvicorn's default `WebSocketProtocol` doesn't put the SSL object into the
ASGI scope, so handlers cannot inspect the client cert. We subclass it
and inject `scope["extensions"]["serve.tls"] = {"ssl_object": ...}` so
LeaderHub can do real mTLS verification instead of trusting a
forwarded header.

Pass this class via `uvicorn.Config(ws=TLSAwareWebSocketProtocol)`."""
from __future__ import annotations

from typing import Any

from uvicorn.protocols.websockets.websockets_impl import (
    WebSocketProtocol as _BaseWSProtocol,
)


class TLSAwareWebSocketProtocol(_BaseWSProtocol):
    async def process_request(self, path: str, request_headers: Any):
        # Parent builds self.scope. Call it first.
        response = await super().process_request(path, request_headers)
        try:
            ssl_obj = self.transport.get_extra_info("ssl_object")
        except Exception:
            ssl_obj = None
        if ssl_obj is not None:
            exts = self.scope.setdefault("extensions", {})
            exts["serve.tls"] = {"ssl_object": ssl_obj}
        return response
