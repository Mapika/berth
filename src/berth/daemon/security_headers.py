from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "object-src 'none'"
    ),
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=()"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def _is_sensitive_path(path: str) -> bool:
    return (
        path == "/metrics"
        or path.startswith("/admin/")
        or path.startswith("/v1/")
    )


class SecurityHeadersMiddleware:
    """Attach conservative browser security headers to HTTP responses.

    The CSP keeps scripts and API connections on the same origin, permits the
    bundled UI's current inline styles and Google Fonts dependency, and blocks
    framing, forms, base-tag rewrites, and old browser plugin loads.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        sensitive = _is_sensitive_path(str(scope.get("path", "")))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name.lower() for name, _value in headers}
                for name, value in _SECURITY_HEADERS.items():
                    raw_name = name.lower().encode("latin-1")
                    if raw_name not in existing:
                        headers.append((raw_name, value.encode("latin-1")))
                if sensitive:
                    for name, value in _NO_STORE_HEADERS.items():
                        raw_name = name.lower().encode("latin-1")
                        if raw_name not in existing:
                            headers.append((raw_name, value.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
