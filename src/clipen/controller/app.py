"""Security-conscious Starlette application for the local Clipen workspace."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from http.cookies import SimpleCookie
import json
from pathlib import Path
import secrets
import time
from typing import Callable

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from clipen.media import InventoryCoordinator
from clipen.projects import DirectoryRole, ProjectError, ProjectWorkspace
from clipen.system_skills import SystemSkillSummary


_SESSION_COOKIE = "clipen_session"
_BOOTSTRAP_HEADER = "x-clipen-bootstrap"
_CSRF_HEADER = "x-clipen-csrf"
_SESSION_LIFETIME_SECONDS = 12 * 60 * 60
_BOOTSTRAP_LIFETIME_SECONDS = 5 * 60
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MAX_JSON_BODY_BYTES = 64 * 1024


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
    system_skills: tuple[SystemSkillSummary, ...] = ()
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
        identifiers = [summary.skill_id for summary in self.system_skills]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("system Skill catalog contains duplicate identifiers")


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
        credentials = None
        if path.startswith("/api/") and path not in {
            "/api/health",
            "/api/session/bootstrap",
        }:
            credentials = self.authority.authenticate(
                _cookie_value(headers, _SESSION_COOKIE)
            )
            if credentials is None:
                await self._reject(scope, receive, send, 401, "session_required")
                return
        if method in _MUTATING_METHODS:
            if supplied_origin != self.origin or headers.get("sec-fetch-site") != "same-origin":
                await self._reject(scope, receive, send, 403, "cross_site_request")
                return
            if path != "/api/session/bootstrap":
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
    workspace: ProjectWorkspace | None = None,
    inventories: InventoryCoordinator | None = None,
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

    async def read_json(request: Request) -> dict[str, object]:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ProjectError(
                "content_type_invalid",
                "Clipen 只接受 JSON 操作資料。",
                status_code=415,
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_JSON_BODY_BYTES:
                raise ProjectError(
                    "body_too_large", "這次操作的資料過大。", status_code=413
                )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProjectError(
                "json_invalid", "無法讀取這次操作的資料。", status_code=400
            ) from error
        if not isinstance(payload, dict):
            raise ProjectError(
                "json_invalid", "操作資料必須是 JSON object。", status_code=400
            )
        return payload

    def require_workspace() -> ProjectWorkspace:
        if workspace is None:
            raise ProjectError(
                "workspace_unavailable",
                "Clipen 作品工作台尚未準備完成。",
                status_code=503,
            )
        return workspace

    def require_inventories() -> InventoryCoordinator:
        if inventories is None:
            raise ProjectError(
                "inventory_unavailable",
                "Clipen 素材檢查尚未準備完成。",
                status_code=503,
            )
        return inventories

    def require_fields(
        payload: dict[str, object], required: set[str], optional: set[str] | None = None
    ) -> None:
        allowed = required | (optional or set())
        if not required.issubset(payload) or not set(payload).issubset(allowed):
            raise ProjectError(
                "command_invalid", "這次操作缺少必要資料。", status_code=422
            )

    def parse_role(value: object) -> DirectoryRole:
        try:
            return DirectoryRole(value)
        except (TypeError, ValueError) as error:
            raise ProjectError(
                "role_invalid", "資料夾用途無效。", status_code=422
            ) from error

    async def project_error_response(error: ProjectError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}},
            status_code=error.status_code,
        )

    async def list_projects(_: Request) -> Response:
        try:
            projects = await run_in_threadpool(require_workspace().list_projects)
            return JSONResponse(
                {"projects": [project.to_public() for project in projects]}
            )
        except ProjectError as error:
            return await project_error_response(error)

    async def get_project(request: Request) -> Response:
        try:
            project = await run_in_threadpool(
                require_workspace().get_project, request.path_params["project_id"]
            )
            return JSONResponse({"project": project.to_public()})
        except ProjectError as error:
            return await project_error_response(error)

    async def create_project(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, {"request_id", "name"})
            project = await run_in_threadpool(
                require_workspace().create_project,
                payload["request_id"],
                payload["name"],
            )
            return JSONResponse({"project": project.to_public()}, status_code=201)
        except ProjectError as error:
            return await project_error_response(error)

    async def update_details(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(
                payload, {"request_id", "expected_revision", "name", "brief"}
            )
            project = await run_in_threadpool(
                require_workspace().update_details,
                request.path_params["project_id"],
                payload["request_id"],
                payload["expected_revision"],
                payload["name"],
                payload["brief"],
            )
            return JSONResponse({"project": project.to_public()})
        except ProjectError as error:
            return await project_error_response(error)

    async def select_directory(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, {"request_id", "role"})
            selection = await run_in_threadpool(
                require_workspace().select_directory,
                payload["request_id"],
                parse_role(payload["role"]),
            )
            if selection is None:
                return Response(status_code=204)
            return JSONResponse({"selection": selection})
        except ProjectError as error:
            return await project_error_response(error)

    async def set_directory(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(
                payload,
                {
                    "request_id",
                    "expected_revision",
                    "role",
                    "selection_token",
                },
            )
            project = await run_in_threadpool(
                require_workspace().set_directory,
                request.path_params["project_id"],
                payload["request_id"],
                payload["expected_revision"],
                parse_role(payload["role"]),
                payload["selection_token"],
            )
            return JSONResponse({"project": project.to_public()})
        except ProjectError as error:
            return await project_error_response(error)

    async def remove_directory(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, {"request_id", "expected_revision", "role"})
            project = await run_in_threadpool(
                require_workspace().remove_directory,
                request.path_params["project_id"],
                payload["request_id"],
                payload["expected_revision"],
                parse_role(payload["role"]),
            )
            return JSONResponse({"project": project.to_public()})
        except ProjectError as error:
            return await project_error_response(error)

    async def inventory_status(request: Request) -> Response:
        try:
            coordinator = require_inventories()
            project_id = request.path_params["project_id"]
            latest, current = await run_in_threadpool(
                lambda: (
                    coordinator.repository.latest_run(project_id),
                    coordinator.repository.current_run(project_id),
                )
            )
            return JSONResponse(
                {
                    "latest": None if latest is None else latest.to_public(),
                    "current": None if current is None else current.to_public(),
                }
            )
        except ProjectError as error:
            return await project_error_response(error)

    async def inventory_items(request: Request) -> Response:
        try:
            coordinator = require_inventories()
            offset = _query_integer(request, "offset", 0, minimum=0, maximum=1_000_000)
            limit = _query_integer(request, "limit", 100, minimum=1, maximum=200)
            project_id = request.path_params["project_id"]
            run_id = request.path_params["run_id"]
            run, page = await run_in_threadpool(
                lambda: (
                    coordinator.repository.get_run(project_id, run_id),
                    coordinator.repository.list_items(
                        project_id, run_id, offset=offset, limit=limit
                    ),
                )
            )
            return JSONResponse({"inventory": run.to_public(), **page.to_public()})
        except ProjectError as error:
            return await project_error_response(error)

    async def start_inventory(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, {"request_id", "expected_revision"})
            run = await run_in_threadpool(
                require_inventories().start,
                request.path_params["project_id"],
                payload["request_id"],
                payload["expected_revision"],
            )
            return JSONResponse({"inventory": run.to_public()}, status_code=202)
        except ProjectError as error:
            return await project_error_response(error)

    async def cancel_inventory(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, set())
            run = await run_in_threadpool(
                require_inventories().cancel,
                request.path_params["project_id"],
                request.path_params["run_id"],
            )
            return JSONResponse({"inventory": run.to_public()})
        except ProjectError as error:
            return await project_error_response(error)

    async def system_skills(_: Request) -> Response:
        return JSONResponse(
            {"skills": [summary.as_public_dict() for summary in config.system_skills]}
        )

    app = Starlette(
        debug=False,
        routes=[
            Route("/api/health", health, methods=["GET"]),
            Route("/api/session/bootstrap", bootstrap, methods=["POST"]),
            Route("/api/status", status, methods=["GET"]),
            Route("/api/system-skills", system_skills, methods=["GET"]),
            Route("/api/projects", list_projects, methods=["GET"]),
            Route("/api/projects/{project_id}", get_project, methods=["GET"]),
            Route("/api/commands/create-project", create_project, methods=["POST"]),
            Route(
                "/api/system/commands/select-directory",
                select_directory,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/commands/update-details",
                update_details,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/commands/set-directory",
                set_directory,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/commands/remove-directory",
                remove_directory,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/inventory",
                inventory_status,
                methods=["GET"],
            ),
            Route(
                "/api/projects/{project_id}/inventories/{run_id}",
                inventory_items,
                methods=["GET"],
            ),
            Route(
                "/api/projects/{project_id}/commands/start-inventory",
                start_inventory,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/inventories/{run_id}/commands/cancel",
                cancel_inventory,
                methods=["POST"],
            ),
            Mount("/", app=StaticFiles(directory=static_root, html=True), name="web"),
        ],
    )
    return LocalRequestGuard(
        app,
        authority=authority,
        origin=config.origin,
        allowed_hosts=config.allowed_hosts,
    )


def _query_integer(
    request: Request,
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdigit() or len(raw) > 10:
        raise ProjectError("pagination_invalid", "素材清單頁碼無效。", status_code=422)
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ProjectError("pagination_invalid", "素材清單頁碼無效。", status_code=422)
    return value
