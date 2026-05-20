"""WebSocket protocol subclass that exposes the TLS peer cert to ASGI.

Uvicorn's default `WebSocketProtocol` doesn't put the SSL object into
the ASGI scope, so handlers cannot inspect the client cert. We inject
it into `scope["extensions"]["berth.tls"] = {"ssl_object": ...}` so
LeaderHub can do real mTLS verification instead of trusting a
forwarded header.

The injection must happen BEFORE the parent's
`self.loop.create_task(self.run_asgi())` schedules the ASGI handler —
if we mutate scope after `super().process_request` returns, the
handler has already read it. So we inline a copy of uvicorn 0.27+'s
`process_request` body and add the injection at the right spot. If
the parent signature changes in a future uvicorn version this will
break loudly — preferable to silently falling back to header-trust.

Pass this class via `uvicorn.Config(ws=TLSAwareWebSocketProtocol)`.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from uvicorn.protocols.websockets.websockets_impl import (
    WebSocketProtocol as _BaseWSProtocol,
)


class TLSAwareWebSocketProtocol(_BaseWSProtocol):
    async def process_request(self, path: str, request_headers: Any):
        import websockets.legacy.handshake as _hs

        path_portion, _, query_string = path.partition("?")
        _hs.check_request(request_headers)

        subprotocols: list[str] = []
        for header in request_headers.get_all("Sec-WebSocket-Protocol"):
            subprotocols.extend([t.strip() for t in header.split(",")])

        asgi_headers = [
            (name.encode("ascii"), value.encode("ascii", errors="surrogateescape"))
            for name, value in request_headers.raw_items()
        ]
        path_decoded = unquote(path_portion)
        full_path = self.root_path + path_decoded
        full_raw_path = self.root_path.encode("ascii") + path_portion.encode("ascii")

        # Pull the SSL object from the transport once, before scope is
        # exposed to the ASGI app. Captured at this exact point because
        # self.loop.create_task(...) below schedules the handler — by the
        # time we'd mutate from outside `super().process_request`, the
        # handler has already read scope.
        try:
            ssl_obj = self.transport.get_extra_info("ssl_object")
        except Exception:
            ssl_obj = None
        extensions: dict[str, Any] = {"websocket.http.response": {}}
        if ssl_obj is not None:
            extensions["berth.tls"] = {"ssl_object": ssl_obj}

        self.scope = {
            "type": "websocket",
            "asgi": {
                "version": self.config.asgi_version,
                "spec_version": "2.4",
            },
            "http_version": "1.1",
            "scheme": self.scheme,
            "server": self.server,
            "client": self.client,
            "root_path": self.root_path,
            "path": full_path,
            "raw_path": full_raw_path,
            "query_string": query_string.encode("ascii"),
            "headers": asgi_headers,
            "subprotocols": subprotocols,
            "state": self.app_state.copy(),
            "extensions": extensions,
        }
        task = self.loop.create_task(self.run_asgi())
        task.add_done_callback(self.on_task_complete)
        self.tasks.add(task)
        await self.handshake_started_event.wait()
        return self.initial_response
