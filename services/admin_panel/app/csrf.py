from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response


CSRF_SESSION_KEY = "admin_csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def ensure_csrf_token(request: Request) -> str:
    token = str(request.session.get(CSRF_SESSION_KEY) or "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


class AdminCSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        protects_admin_form = path.startswith("/admin") and not path.startswith("/admin/api")
        if not protects_admin_form:
            return await call_next(request)

        expected = ensure_csrf_token(request)
        if request.method.upper() in SAFE_METHODS:
            return await call_next(request)

        supplied = request.headers.get("x-csrf-token", "")
        if not supplied:
            form = await request.form()
            supplied = str(form.get("csrf_token") or "")

        if not hmac.compare_digest(str(supplied), expected):
            return PlainTextResponse("CSRF token missing or invalid", status_code=403)

        return await call_next(request)
