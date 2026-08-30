"""Security-conscious Starlette application for the local Clipen workspace."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from http.cookies import SimpleCookie
from pathlib import Path
import secrets
import time
from typing import Callable

from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send


_SESSION_COOKIE = "clipen_session"
_BOOTSTRAP_HEADER = "x-clipen-bootstrap"
_CSRF_HEADER = "x-clipen-csrf"
_SESSION_LIFETIME_SECONDS = 12 * 60 * 60
_BOOTSTRAP_LIFETIME_SECONDS = 5 * 60
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ProviderState(str, Enum):
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    UNSUPPORTED = "unsupported"
    CONFINEMENT_FAILED = "confinement_failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    state: ProviderState
    message: str

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.name) > 80:
            raise ValueError("provider name must be non-empty and bounded")
        if not self.message.strip() or len(self.message) > 240:
            raise ValueError("provider message must be non-empty and bounded")


@dataclass(frozen=True)
class ControllerConfig:
    static_root: Path
    origin: str = "http://localhost:4200"
    allowed_hosts: tuple[str, ...] = ("localhost:4200", "127.0.0.1:4200")

    def __post_init__(self) -> None:
        root = self.static_root.resolve(strict=True)
        if not root.is_dir() or not (root / "index.html").is_file():
            raise ValueError("static_root must contain the Clipen production bundle")
        if not self.origin.startswith("http://localhost:"):
            raise ValueError("Clipen origin must use localhost over HTTP")
        if not self.allowed_hosts:
            raise ValueError("at least one local Host value is required")


@dataclass(frozen=True)
class SessionCredentials:
    session_token: str
    csrf_token: str


class LocalSessionAuthority:
    """One-process browser sessions bootstrapped by an unguessable fragment token."""

    def __init__(
        self,
        bootstrap_token: str | None = None,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bootstrap_token = bootstrap_token or secrets.token_urlsafe(32)
        if len(self._bootstrap_token) < 32:
            raise ValueError("bootstrap token is too short")
        self._now = now
        self._bootstrap_expires_at = now() + _BOOTSTRAP_LIFETIME_SECONDS
        self._credentials: SessionCredentials | None = None
        self._session_digest: bytes | None = None

    @property
    def bootstrap_token(self) -> str:
        return self._bootstrap_token

    def bootstrap(self, candidate: str) -> SessionCredentials | None:
        if self._now() > self._bootstrap_expires_at:
            return None
        if not secrets.compare_digest(candidate, self._bootstrap_token):
            return None
        if self._credentials is None:
            credentials = SessionCredentials(
                session_token=secrets.token_urlsafe(32),
                csrf_token=secrets.token_urlsafe(32),
            )
            self._credentials = credentials
            self._session_digest = self._digest(credentials.session_token)
        return self._credentials

    def authenticate(self, candidate: str | None) -> SessionCredentials | None:
        if not candidate or self._credentials is None or self._session_digest is None:
            return None
        if secrets.compare_digest(self._digest(candidate), self._session_digest):
            return self._credentials
        return None

    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()


def _cookie_value(headers: Headers, name: str) -> str | None:
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("cookie", ""))
    except Exception:
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel is not None else None


class LocalRequestGuard:
    """Reject DNS rebinding, cross-site mutations, and missing CSRF before routing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        authority: LocalSessionAuthority,
        origin: str,
        allowed_hosts: tuple[str, ...],
    ) -> None:
        self.app = app
        self.authority = authority
        self.origin = origin
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if headers.get("host", "").lower() not in self.allowed_hosts:
            await self._reject(scope, receive, send, 400, "invalid_host")
            return

        supplied_origin = headers.get("origin")
        if supplied_origin is not None and supplied_origin != self.origin:
            await self._reject(scope, receive, send, 403, "invalid_origin")
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if method in _MUTATING_METHODS:
            if supplied_origin != self.origin or headers.get("sec-fetch-site") != "same-origin":
                await self._reject(scope, receive, send, 403, "cross_site_request")
                return
            if path != "/api/session/bootstrap":
                credentials = self.authority.authenticate(
                    _cookie_value(headers, _SESSION_COOKIE)
                )
                csrf = headers.get(_CSRF_HEADER)
                if credentials is None or csrf is None or not secrets.compare_digest(
                    csrf, credentials.csrf_token
                ):
                    await self._reject(scope, receive, send, 403, "csrf_failed")
                    return

        await self.app(scope, receive, self._secure_send(scope, send))

    def _secure_send(self, scope: Scope, send: Send) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["content-security-policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data: blob:; media-src 'self' blob:; "
                    "connect-src 'self'; object-src 'none'; base-uri 'none'; "
                    "frame-ancestors 'none'; form-action 'self'"
                )
                headers["x-content-type-options"] = "nosniff"
                headers["x-frame-options"] = "DENY"
                headers["referrer-policy"] = "no-referrer"
                headers["cross-origin-opener-policy"] = "same-origin"
                headers["cross-origin-resource-policy"] = "same-origin"
                headers["permissions-policy"] = (
                    "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
                )
                path = str(scope.get("path", ""))
                if path.startswith("/api/") or path in {"", "/", "/index.html"}:
                    headers["cache-control"] = "no-store"
                elif path.startswith("/assets/"):
                    headers["cache-control"] = "public, max-age=31536000, immutable"
            await send(message)

        return wrapped

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        code: str,
    ) -> None:
        response = JSONResponse(
            {"error": {"code": code, "message": "無法驗證本機工作台連線。"}},
            status_code=status_code,
        )
        await response(scope, receive, self._secure_send(scope, send))


def create_app(
    config: ControllerConfig,
    provider: ProviderStatus,
    authority: LocalSessionAuthority,
) -> ASGIApp:
    static_root = config.static_root.resolve(strict=True)

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def bootstrap(request: Request) -> Response:
        headers = request.headers
        credentials = authority.bootstrap(headers.get(_BOOTSTRAP_HEADER, ""))
        if credentials is None:
            return JSONResponse(
                {
                    "error": {
                        "code": "bootstrap_invalid",
                        "message": "連線已失效，請從 Clipen Terminal 重新開啟。",
                    }
                },
                status_code=401,
            )
        response = JSONResponse({"connected": True})
        response.set_cookie(
            _SESSION_COOKIE,
            credentials.session_token,
            max_age=_SESSION_LIFETIME_SECONDS,
            httponly=True,
            secure=False,
            samesite="strict",
            path="/",
        )
        return response

    async def status(request: Request) -> Response:
        headers = request.headers
        credentials = authority.authenticate(_cookie_value(headers, _SESSION_COOKIE))
        if credentials is None:
            return JSONResponse(
                {
                    "error": {
                        "code": "session_required",
                        "message": "請從 Clipen Terminal 重新開啟工作台。",
                    }
                },
                status_code=401,
            )
        return JSONResponse(
            {
                "product": {"name": "Clipen", "workspace": "local"},
                "provider": {
                    "name": provider.name,
                    "state": provider.state.value,
                    "message": provider.message,
                },
                "session": {"csrf_token": credentials.csrf_token},
            }
        )

    app = Starlette(
        debug=False,
        routes=[
            Route("/api/health", health, methods=["GET"]),
            Route("/api/session/bootstrap", bootstrap, methods=["POST"]),
            Route("/api/status", status, methods=["GET"]),
            Mount("/", app=StaticFiles(directory=static_root, html=True), name="web"),
        ],
    )
    return LocalRequestGuard(
        app,
        authority=authority,
        origin=config.origin,
        allowed_hosts=config.allowed_hosts,
    )
