"""Body-size cap for the public listener.

Without one, a single multi-GB POST to /v1/chat/completions OOMs a small
VPS — `await request.body()` in the proxy buffers the whole payload.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """413 if Content-Length exceeds `max_bytes`.

    Honours the declared Content-Length only; chunked uploads without a
    length aren't actively counted here (Starlette buffers them in
    `await request.body()` further down the stack). For our threat model
    — a public OpenAI-compatible endpoint where clients send well-formed
    bodies — Content-Length enforcement is the operational lever that
    catches the foot-gun POSTs.
    """

    def __init__(self, app, *, max_bytes: int) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                declared = int(cl)
            except ValueError:
                declared = -1
            if declared > self._max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"request body of {declared} bytes exceeds the "
                            f"{self._max_bytes}-byte cap"
                        ),
                    },
                )
        return await call_next(request)
