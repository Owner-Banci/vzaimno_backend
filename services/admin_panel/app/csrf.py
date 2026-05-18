from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from urllib.parse import parse_qs

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response


CSRF_SESSION_KEY = "admin_csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
STATIC_PATH_PREFIXES = ("/admin/static", "/admin/statics")


def ensure_csrf_token(request: Request) -> str:
    token = str(request.session.get(CSRF_SESSION_KEY) or "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


class AdminCSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        is_admin_static = any(path == prefix or path.startswith(f"{prefix}/") for prefix in STATIC_PATH_PREFIXES)
        protects_admin_form = path.startswith("/admin") and not path.startswith("/admin/api") and not is_admin_static
        if not protects_admin_form:
            return await call_next(request)

        expected = ensure_csrf_token(request)
        if request.method.upper() in SAFE_METHODS:
            return await call_next(request)

        supplied = request.headers.get("x-csrf-token", "")
        if not supplied:
            body = await request.body()
            parsed = parse_qs(body.decode("utf-8", errors="ignore"), keep_blank_values=True)
            supplied = str((parsed.get("csrf_token") or [""])[0])

            async def receive() -> dict[str, object]:
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = receive

        if not hmac.compare_digest(str(supplied), expected):
            return PlainTextResponse("CSRF token missing or invalid", status_code=403)

        return await call_next(request)
