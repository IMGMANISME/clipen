"""Security-conscious Starlette application for the local Clipen workspace."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import asyncio
import hashlib
from http.cookies import SimpleCookie
import json
from pathlib import Path
import secrets
import threading

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from clipen.analysis import (
    ASRSetupError,
    ManagedAnalysisRuntime,
    PreparationCoordinator,
    PreparationRepository,
)
from clipen.analysis.evidence_service import EvidenceService
from clipen.annotations import AnnotationRepository
from clipen.editing import EditAgentCoordinator, TimelineService
from clipen.media import InventoryCoordinator, MediaPreviewRepository
from clipen.planning import PlanningService
from clipen.rendering import PreviewRenderService
from clipen.finishing import FinishingService
from clipen.projects import (
    DirectoryRole,
    ProjectError,
    ProjectWorkspace,
    TimelineValidationRepository,
)
from clipen.system_skills import SystemSkillSummary


_SESSION_COOKIE = "clipen_session"
_CSRF_HEADER = "x-clipen-csrf"
_INSTANCE_HEADER = "x-clipen-instance"
_SESSION_LIFETIME_SECONDS = 12 * 60 * 60
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
    instance_secret: str | None = None

    def __post_init__(self) -> None:
        root = self.static_root.resolve(strict=True)
        if not root.is_dir() or not (root / "index.html").is_file():
            raise ValueError("static_root must contain the Clipen production bundle")
        if not self.origin.startswith("http://localhost:"):
            raise ValueError("Clipen origin must use localhost over HTTP")
        if not self.allowed_hosts:
            raise ValueError("at least one local Host value is required")
        if self.instance_secret is not None and len(self.instance_secret) < 32:
            raise ValueError("controller instance secret is too short")
        identifiers = [summary.skill_id for summary in self.system_skills]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("system Skill catalog contains duplicate identifiers")


@dataclass(frozen=True)
class SessionCredentials:
    session_token: str
    csrf_token: str


class LocalSessionAuthority:
    """One-process browser sessions created only by the local same-origin UI."""

    def __init__(self) -> None:
        self._credentials: SessionCredentials | None = None
        self._session_digest: bytes | None = None
        self._lock = threading.Lock()

    def connect_local_browser(self) -> SessionCredentials:
        """Authorize a same-origin browser page served by this loopback process."""
        with self._lock:
            return self._credentials_locked()

    def authenticate(self, candidate: str | None) -> SessionCredentials | None:
        with self._lock:
            if not candidate or self._credentials is None or self._session_digest is None:
                return None
            if secrets.compare_digest(self._digest(candidate), self._session_digest):
                return self._credentials
            return None

    def _credentials_locked(self) -> SessionCredentials:
        if self._credentials is None:
            credentials = SessionCredentials(
                session_token=secrets.token_urlsafe(32),
                csrf_token=secrets.token_urlsafe(32),
            )
            self._credentials = credentials
            self._session_digest = self._digest(credentials.session_token)
        return self._credentials

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


def _media_range(value: str | None, size: int) -> tuple[int, int]:
    if size <= 0:
        raise ValueError("empty media cannot be ranged")
    if value is None:
        return 0, size - 1
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("only one byte range is supported")
    raw_start, separator, raw_end = value[6:].partition("-")
    if not separator:
        raise ValueError("invalid byte range")
    try:
        if raw_start:
            start = int(raw_start)
            end = size - 1 if not raw_end else int(raw_end)
        else:
            suffix = int(raw_end)
            if suffix <= 0:
                raise ValueError("invalid suffix range")
            start = max(0, size - suffix)
            end = size - 1
    except (TypeError, ValueError) as error:
        raise ValueError("invalid byte range") from error
    if start < 0 or end < start or start >= size:
        raise ValueError("unsatisfiable byte range")
    return start, min(end, size - 1)


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
            "/api/session/connect",
            "/api/instance/reopen",
        }:
            credentials = self.authority.authenticate(
                _cookie_value(headers, _SESSION_COOKIE)
            )
            if credentials is None:
                await self._reject(scope, receive, send, 401, "session_required")
                return
        if method in _MUTATING_METHODS and path != "/api/instance/reopen":
            if supplied_origin != self.origin or headers.get("sec-fetch-site") != "same-origin":
                await self._reject(scope, receive, send, 403, "cross_site_request")
                return
            if path != "/api/session/connect":
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
    preparations: PreparationCoordinator | None = None,
    preparation_repository: PreparationRepository | None = None,
    analysis_runtime: ManagedAnalysisRuntime | None = None,
    evidence_service: EvidenceService | None = None,
    timeline_validations: TimelineValidationRepository | None = None,
    planning_service: PlanningService | None = None,
    timeline_service: TimelineService | None = None,
    annotations: AnnotationRepository | None = None,
    media_previews: MediaPreviewRepository | None = None,
    preview_renders: PreviewRenderService | None = None,
    edit_agents: EditAgentCoordinator | None = None,
    finishing: FinishingService | None = None,
) -> ASGIApp:
    static_root = config.static_root.resolve(strict=True)
    if timeline_validations is None and workspace is not None:
        timeline_validations = TimelineValidationRepository(workspace.repository)

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def connect_local_browser(_: Request) -> Response:
        return session_response(authority.connect_local_browser())

    def session_response(credentials: SessionCredentials) -> Response:
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

    async def reopen_instance(request: Request) -> Response:
        supplied = request.headers.get(_INSTANCE_HEADER)
        expected = config.instance_secret
        if (
            expected is None
            or supplied is None
            or not secrets.compare_digest(supplied, expected)
        ):
            return JSONResponse(
                {"error": {"code": "instance_invalid", "message": "找不到 Clipen instance。"}},
                status_code=404,
            )
        return JSONResponse({"running": True})

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

    def require_preparation_repository() -> PreparationRepository:
        repository = (
            analysis_runtime.repository
            if analysis_runtime is not None
            else preparations.repository
            if preparations is not None
            else preparation_repository
        )
        if repository is None:
            raise ProjectError(
                "analysis_unavailable",
                "Clipen 素材分析尚未準備完成。",
                status_code=503,
            )
        return repository

    def require_preparations() -> PreparationCoordinator:
        coordinator = (
            analysis_runtime.coordinator()
            if analysis_runtime is not None
            else preparations
        )
        if coordinator is None:
            raise ProjectError(
                "asr_setup_required",
                "本機語音辨識尚未安裝。",
                status_code=409,
            )
        return coordinator

    def require_analysis_runtime() -> ManagedAnalysisRuntime:
        if analysis_runtime is None:
            raise ProjectError(
                "asr_setup_unavailable",
                "這個 Clipen 工作台無法管理本機語音辨識。",
                status_code=503,
            )
        return analysis_runtime

    def require_timeline_validations() -> TimelineValidationRepository:
        if timeline_validations is None:
            raise ProjectError(
                "timeline_validation_unavailable",
                "Clipen 剪輯檢查尚未準備完成。",
                status_code=503,
            )
        return timeline_validations

    def require_planning() -> PlanningService:
        if planning_service is None:
            raise ProjectError(
                "planning_unavailable",
                "Clipen 剪輯計畫尚未準備完成。",
                status_code=503,
            )
        return planning_service

    def require_timeline() -> TimelineService:
        if timeline_service is None:
            raise ProjectError(
                "timeline_unavailable",
                "Clipen 剪輯版本尚未準備完成。",
                status_code=503,
            )
        return timeline_service

    def require_annotations() -> AnnotationRepository:
        if annotations is None:
            raise ProjectError(
                "annotations_unavailable",
                "Clipen 素材標註尚未準備完成。",
                status_code=503,
            )
        return annotations

    def require_media_previews() -> MediaPreviewRepository:
        if media_previews is None:
            raise ProjectError(
                "media_preview_unavailable",
                "Clipen 素材播放器尚未準備完成。",
                status_code=503,
            )
        return media_previews

    def require_preview_renders() -> PreviewRenderService:
        if preview_renders is None:
            raise ProjectError(
                "preview_unavailable",
                "Clipen 初剪輸出尚未準備完成。",
                status_code=503,
            )
        return preview_renders

    def require_edit_agents() -> EditAgentCoordinator:
        if edit_agents is None:
            raise ProjectError(
                "edit_agent_unavailable",
                "AI 剪輯工具尚未準備完成。",
                status_code=503,
            )
        return edit_agents

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

    async def asr_setup_error_response(error: ASRSetupError) -> JSONResponse:
        messages = {
            "asr_setup_busy": "本機語音辨識正在安裝或移除，請稍候。",
            "asr_release_unsafe": "語音辨識安裝位置不安全，Clipen 沒有進行刪除。",
            "asr_remove_failed": "無法移除本機語音辨識，毛片與分析成果沒有變動。",
            "asr_pre_remove_failed": "無法先安全停止素材分析，語音辨識沒有被移除。",
        }
        return JSONResponse(
            {
                "error": {
                    "code": error.code,
                    "message": messages.get(error.code, "Clipen 無法安全完成語音辨識設定。"),
                }
            },
            status_code=409,
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

    async def delete_project(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(
                payload,
                {
                    "request_id",
                    "expected_revision",
                    "confirmation_name",
                },
            )
            project_id = await run_in_threadpool(
                require_workspace().delete_project,
                request.path_params["project_id"],
                payload["request_id"],
                payload["expected_revision"],
                payload["confirmation_name"],
            )
            return JSONResponse({"deleted_project_id": project_id})
        except ProjectError as error:
            return await project_error_response(error)

    async def timeline_validation_status(request: Request) -> Response:
        try:
            state = await run_in_threadpool(
                require_timeline_validations().get_state,
                request.path_params["project_id"],
            )
            return JSONResponse(state.to_public())
        except ProjectError as error:
            return await project_error_response(error)

    async def accept_timeline_warnings(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, {"request_id", "report_id"})
            state = await run_in_threadpool(
                require_timeline_validations().accept_current_warnings,
                request.path_params["project_id"],
                payload["request_id"],
                payload["report_id"],
            )
            return JSONResponse(state.to_public())
        except ProjectError as error:
            return await project_error_response(error)

    async def timeline_status(request: Request) -> Response:
        try:
            result = await run_in_threadpool(
                require_timeline().status,
                request.path_params["project_id"],
            )
            return JSONResponse(result)
        except ProjectError as error:
            return await project_error_response(error)

    async def annotation_status(request: Request) -> Response:
        try:
            try:
                stream_index = int(request.path_params["stream_index"])
            except (TypeError, ValueError) as error:
                raise ProjectError(
                    "annotation_not_found", "找不到這個素材。", status_code=404
                ) from error
            result = await run_in_threadpool(
                require_annotations().get,
                request.path_params["project_id"],
                request.path_params["version_id"],
                stream_index,
            )
            return JSONResponse({"annotation": result})
        except ProjectError as error:
            return await project_error_response(error)

    async def save_annotation(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(
                payload,
                {
                    "request_id", "version_id", "stream_index",
                    "expected_material_revision", "expected_transcript_revision",
                    "material", "transcript",
                },
            )
            result = await run_in_threadpool(
                require_annotations().save,
                request.path_params["project_id"],
                payload["request_id"],
                payload["version_id"],
                payload["stream_index"],
                payload["expected_material_revision"],
                payload["expected_transcript_revision"],
                payload["material"],
                payload["transcript"],
            )
            return JSONResponse({"annotation": result})
        except ProjectError as error:
            return await project_error_response(error)

    async def preview_media(request: Request) -> Response:
        try:
            preview = await run_in_threadpool(
                require_media_previews().open,
                request.path_params["project_id"],
                request.path_params["version_id"],
            )
            range_header = request.headers.get("range")
            try:
                start, end = _media_range(range_header, preview.size)
            except ValueError:
                preview.close()
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{preview.size}"},
                )
            partial = range_header is not None
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Length": str(end - start + 1),
                "Cache-Control": "private, no-store",
            }
            if partial:
                headers["Content-Range"] = f"bytes {start}-{end}/{preview.size}"
            if request.method == "HEAD":
                preview.close()
                return Response(
                    status_code=206 if partial else 200,
                    media_type=preview.media_type,
                    headers=headers,
                )
            return StreamingResponse(
                preview.chunks(start, end),
                status_code=206 if partial else 200,
                media_type=preview.media_type,
                headers=headers,
            )
        except ProjectError as error:
            return await project_error_response(error)

    def require_finishing() -> FinishingService:
        if finishing is None:
            raise ProjectError("finishing_unavailable", "目前無法使用完成度製作。", status_code=503)
        return finishing

    async def finishing_status(request: Request) -> Response:
        try:
            return JSONResponse(await run_in_threadpool(require_finishing().status, request.path_params["project_id"]))
        except ProjectError as error:
            return await project_error_response(error)

    async def finishing_command(request: Request) -> Response:
        try:
            payload = await read_json(request)
            service = require_finishing()
            project_id = request.path_params["project_id"]
            command = request.path_params["command"]
            if command != "produce":
                raise ProjectError("finishing_workflow_changed", "請使用新版「AI 自動製作樣片」。", status_code=409)
            require_fields(payload, {"request_id", "consent", "direction_confirmed", "context_hash"}, {"feedback"})
            result = await run_in_threadpool(
                service.start, project_id, payload["request_id"], command,
                consent=payload["consent"], direction_confirmed=payload["direction_confirmed"],
                context_hash=payload["context_hash"], feedback=payload.get("feedback", ""),
            )
            return JSONResponse(result, status_code=202)
        except ProjectError as error:
            return await project_error_response(error)

    async def cancel_finishing(request: Request) -> Response:
        try:
            require_fields(await read_json(request), set())
            return JSONResponse(await run_in_threadpool(require_finishing().cancel, request.path_params["project_id"], request.path_params["run_id"]))
        except ProjectError as error:
            return await project_error_response(error)

    async def resume_finishing(request: Request) -> Response:
        try:
            require_fields(await read_json(request), set())
            return JSONResponse(await run_in_threadpool(require_finishing().resume, request.path_params["project_id"], request.path_params["run_id"]), status_code=202)
        except ProjectError as error:
            return await project_error_response(error)

    async def finishing_media(request: Request) -> Response:
        try:
            path = await run_in_threadpool(require_finishing().media_path, request.path_params["project_id"], request.path_params["run_id"])
            return FileResponse(path, media_type="video/mp4", headers={"Cache-Control":"private, no-store"})
        except ProjectError as error:
            return await project_error_response(error)

    async def preview_render_status(request: Request) -> Response:
        try:
            result = await run_in_threadpool(
                require_preview_renders().status,
                request.path_params["project_id"],
            )
            return JSONResponse(result)
        except ProjectError as error:
            return await project_error_response(error)

    async def start_preview_render(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, {"request_id", "timeline_id"})
            result = await run_in_threadpool(
                require_preview_renders().start,
                request.path_params["project_id"],
                payload["request_id"],
                payload["timeline_id"],
            )
            return JSONResponse(result, status_code=202)
        except ProjectError as error:
            return await project_error_response(error)

    async def cancel_preview_render(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, set())
            result = await run_in_threadpool(
                require_preview_renders().cancel,
                request.path_params["project_id"],
                request.path_params["render_id"],
            )
            return JSONResponse(result)
        except ProjectError as error:
            return await project_error_response(error)

    async def preview_render_media(request: Request) -> Response:
        try:
            path = await run_in_threadpool(
                require_preview_renders().media_path,
                request.path_params["project_id"],
                request.path_params["render_id"],
            )
            return FileResponse(
                path,
                media_type="video/mp4",
                headers={"Cache-Control": "private, no-store"},
            )
        except ProjectError as error:
            return await project_error_response(error)

    async def build_timeline(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(
                payload,
                {
                    "request_id",
                    "plan_id",
                    "plan_revision",
                    "output_preset",
                    "frame_rate",
                },
            )
            result = await run_in_threadpool(
                require_timeline().build,
                request.path_params["project_id"],
                payload["request_id"],
                payload["plan_id"],
                payload["plan_revision"],
                payload["output_preset"],
                payload["frame_rate"],
            )
            return JSONResponse(result, status_code=201)
        except ProjectError as error:
            return await project_error_response(error)

    async def edit_agent_status(request: Request) -> Response:
        try:
            result = await run_in_threadpool(
                require_edit_agents().status,
                request.path_params["project_id"],
            )
            return JSONResponse(result)
        except ProjectError as error:
            return await project_error_response(error)

    async def edit_agent_export(request: Request) -> Response:
        try:
            path=await run_in_threadpool(require_edit_agents().export_path,request.path_params['project_id'],request.path_params['run_id'],request.path_params['revision_id'])
            return FileResponse(path,media_type='application/json',filename=path.name)
        except ProjectError as error:
            return await project_error_response(error)

    async def accept_program_reference(request: Request) -> Response:
        try:
            payload=await read_json(request)
            require_fields(payload,{'run_id','revision_id'})
            reference=await run_in_threadpool(require_edit_agents().accept_program_reference,
                request.path_params['project_id'],payload['run_id'],payload['revision_id'])
            return JSONResponse({'program_reference':reference})
        except ProjectError as error:
            return await project_error_response(error)

    async def start_edit_agent(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(
                payload,
                {"request_id", "consent"},
                {"max_tool_calls", "max_preview_renders", "creative_direction"},
            )
            run = await run_in_threadpool(
                require_edit_agents().start,
                request.path_params["project_id"],
                payload["request_id"],
                payload["consent"],
                max_tool_calls=payload.get("max_tool_calls", 96),
                max_preview_renders=payload.get("max_preview_renders", 4),
                creative_direction=payload.get("creative_direction"),
            )
            return JSONResponse({"run": run.to_public()}, status_code=202)
        except (TypeError, ValueError) as error:
            return await project_error_response(
                ProjectError("command_invalid", str(error), status_code=422)
            )
        except ProjectError as error:
            return await project_error_response(error)

    async def cancel_edit_agent(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, set())
            run = await run_in_threadpool(
                require_edit_agents().cancel,
                request.path_params["project_id"],
                request.path_params["run_id"],
            )
            return JSONResponse({"run": run.to_public()})
        except ProjectError as error:
            return await project_error_response(error)

    async def planning_status(request: Request) -> Response:
        try:
            result = await run_in_threadpool(
                require_planning().status,
                request.path_params["project_id"],
            )
            return JSONResponse(result)
        except ProjectError as error:
            return await project_error_response(error)

    async def start_planning(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(
                payload,
                {
                    "request_id",
                    "expected_revision",
                    "editing_skill_id",
                    "consent",
                    "expected_context_hash",
                },
            )
            run = await run_in_threadpool(
                require_planning().start,
                request.path_params["project_id"],
                payload["request_id"],
                payload["expected_revision"],
                payload["editing_skill_id"],
                payload["consent"],
                payload["expected_context_hash"],
            )
            return JSONResponse({"run": run.to_public()}, status_code=202)
        except ProjectError as error:
            return await project_error_response(error)

    async def cancel_planning(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, set())
            run = await run_in_threadpool(
                require_planning().cancel,
                request.path_params["project_id"],
                request.path_params["run_id"],
            )
            return JSONResponse({"run": run.to_public()})
        except ProjectError as error:
            return await project_error_response(error)

    async def update_plan(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(
                payload,
                {"request_id", "plan_id", "expected_revision", "plan"},
            )
            if not isinstance(payload["plan"], dict):
                raise ProjectError(
                    "plan_content_invalid",
                    "剪輯計畫內容格式無效。",
                    status_code=422,
                )
            result = await run_in_threadpool(
                require_planning().update_plan,
                request.path_params["project_id"],
                payload["request_id"],
                payload["plan_id"],
                payload["expected_revision"],
                payload["plan"],
            )
            return JSONResponse(result)
        except ProjectError as error:
            return await project_error_response(error)

    async def confirm_plan(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(
                payload,
                {"request_id", "plan_id", "expected_revision"},
            )
            result = await run_in_threadpool(
                require_planning().confirm_plan,
                request.path_params["project_id"],
                payload["request_id"],
                payload["plan_id"],
                payload["expected_revision"],
            )
            return JSONResponse(result)
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

    def require_evidence() -> EvidenceService:
        if evidence_service is None:
            raise ProjectError("evidence_unavailable", "請重新啟動 Clipen 以使用證據分析。", status_code=503)
        return evidence_service

    async def evidence_status(request: Request) -> Response:
        try:
            raw_offset = request.query_params.get("offset", "0")
            if not raw_offset.isascii() or not raw_offset.isdigit() or len(raw_offset) > 4:
                raise ProjectError("invalid_page", "頁碼不正確。", status_code=400)
            return JSONResponse(await run_in_threadpool(require_evidence().status, request.path_params["project_id"], int(raw_offset)))
        except ProjectError as error:
            return await project_error_response(error)

    async def evidence_command(request: Request) -> Response:
        try:
            service = require_evidence()
            project_id = request.path_params["project_id"]
            action = request.path_params["action"]
            payload = await read_json(request)
            if action == "start":
                require_fields(payload, {"request_id", "expected_revision", "scope_id"})
                result = await run_in_threadpool(service.start, project_id, payload["request_id"], payload["expected_revision"], payload["scope_id"])
            elif action in {"cancel", "resume"}:
                require_fields(payload, {"run_id"})
                result = await run_in_threadpool(getattr(service, action), project_id, payload["run_id"])
            elif action == "observe":
                require_fields(payload, {"run_id", "request_id", "item_key", "consent"})
                result = await run_in_threadpool(service.observe, project_id, payload["run_id"], payload["request_id"], payload["item_key"], payload["consent"])
            else:
                raise ProjectError("command_unknown", "找不到這個操作。", status_code=404)
            return JSONResponse(result, status_code=202)
        except ProjectError as error:
            return await project_error_response(error)

    async def evidence_image(request: Request) -> Response:
        try:
            def read_image():
                return require_evidence().image(request.path_params["project_id"], request.path_params["run_id"], request.path_params["artifact_id"]).read_bytes()
            return Response(await run_in_threadpool(read_image), media_type="image/png", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
        except ProjectError as error:
            return await project_error_response(error)

    async def analysis_status(request: Request) -> Response:
        try:
            repository = require_preparation_repository()
            project_id = request.path_params["project_id"]

            def read_status():
                latest = repository.latest_run(project_id)
                current = repository.current_run(project_id)
                try:
                    scope = repository.preflight(project_id).to_public()
                    blocked_reason = None
                except ProjectError as error:
                    if error.code not in {
                        "inventory_required",
                        "preparation_empty",
                        "root_unavailable",
                        "root_changed",
                    }:
                        raise
                    scope = None
                    blocked_reason = error.code
                return latest, current, scope, blocked_reason

            latest, current, scope, blocked_reason = await run_in_threadpool(read_status)
            runtime_status = (
                analysis_runtime.public_status()
                if analysis_runtime is not None
                else {
                    "state": "ready" if preparations is not None else "setup_required",
                    "phase": "ready" if preparations is not None else "not_installed",
                    "message": (
                        "本機語音辨識可用"
                        if preparations is not None
                        else "需要約 1.6 GB 空間安裝隔離的本機語音辨識環境與模型"
                    ),
                    "error_code": None,
                    "completed_units": 3 if preparations is not None else 0,
                    "total_units": 3,
                }
            )
            return JSONResponse(
                {
                    "runtime": runtime_status,
                    "scope": scope,
                    "blocked_reason": blocked_reason,
                    "latest": None if latest is None else latest.to_public(),
                    "current": None if current is None else current.to_public(),
                }
            )
        except ProjectError as error:
            return await project_error_response(error)

    async def analysis_results(request: Request) -> Response:
        try:
            trip_day = (
                None
                if request.query_params.get("trip_day") is None
                else _query_integer(request, "trip_day", 1, minimum=1, maximum=365)
            )
            results = await run_in_threadpool(
                require_preparation_repository().result_items,
                request.path_params["project_id"],
                request.path_params["run_id"],
                offset=_query_integer(
                    request, "offset", 0, minimum=0, maximum=1_000_000
                ),
                limit=_query_integer(request, "limit", 25, minimum=1, maximum=100),
                trip_day=trip_day,
                review_state=request.query_params.get("review_state", "all"),
            )
            return JSONResponse({"results": results})
        except ProjectError as error:
            return await project_error_response(error)

    async def transcript_segments(request: Request) -> Response:
        try:
            transcript = await run_in_threadpool(
                require_preparation_repository().transcript_segments,
                request.path_params["project_id"],
                request.path_params["run_id"],
                request.path_params["version_id"],
                request.path_params["stream_index"],
                offset=_query_integer(
                    request, "offset", 0, minimum=0, maximum=1_000_000
                ),
                limit=_query_integer(request, "limit", 100, minimum=1, maximum=100),
            )
            return JSONResponse({"transcript": transcript})
        except ProjectError as error:
            return await project_error_response(error)

    async def remove_asr(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, set())
            runtime = require_analysis_runtime()
            await run_in_threadpool(runtime.remove)
            return JSONResponse({"runtime": runtime.public_status()})
        except ProjectError as error:
            return await project_error_response(error)
        except ASRSetupError as error:
            return await asr_setup_error_response(error)

    async def start_analysis(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(
                payload,
                {"request_id", "expected_revision", "scope_id"},
                {"language", "vocabulary"},
            )
            run = await run_in_threadpool(
                require_preparations().start,
                request.path_params["project_id"],
                payload["request_id"],
                payload["expected_revision"],
                payload["scope_id"],
                payload.get("language", "zh"),
                payload.get("vocabulary", []),
            )
            return JSONResponse({"analysis": run.to_public()}, status_code=202)
        except ProjectError as error:
            return await project_error_response(error)

    async def cancel_analysis(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, set())
            run = await run_in_threadpool(
                require_preparations().cancel,
                request.path_params["project_id"],
                request.path_params["run_id"],
            )
            return JSONResponse({"analysis": run.to_public()})
        except ProjectError as error:
            return await project_error_response(error)

    async def resume_analysis(request: Request) -> Response:
        try:
            payload = await read_json(request)
            require_fields(payload, {"request_id"})
            run = await run_in_threadpool(
                require_preparations().resume,
                request.path_params["project_id"],
                request.path_params["run_id"],
                payload["request_id"],
            )
            return JSONResponse({"analysis": run.to_public()}, status_code=202)
        except ProjectError as error:
            return await project_error_response(error)

    async def analysis_events(request: Request) -> Response:
        try:
            repository = require_preparation_repository()
            project_id = request.path_params["project_id"]
            run_id = request.path_params["run_id"]
            raw_last = request.headers.get("last-event-id", "0")
            if not raw_last.isascii() or not raw_last.isdigit() or len(raw_last) > 20:
                raise ProjectError(
                    "event_cursor_invalid", "分析進度位置無效。", status_code=422
                )
            cursor = int(raw_last)
            await run_in_threadpool(repository.get_run, project_id, run_id)

            async def stream():
                nonlocal cursor
                idle = 0
                while idle < 50 and not await request.is_disconnected():
                    events = await run_in_threadpool(
                        lambda: repository.list_events(
                            project_id, run_id, after_sequence=cursor
                        )
                    )
                    if events:
                        idle = 0
                        for event in events:
                            cursor = int(event["sequence"])
                            data = json.dumps(
                                event,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            yield f"id: {cursor}\ndata: {data}\n\n".encode("utf-8")
                    else:
                        idle += 1
                        if idle % 20 == 0:
                            yield b": keepalive\n\n"
                    run = await run_in_threadpool(repository.get_run, project_id, run_id)
                    if run.state.value in {
                        "succeeded",
                        "failed",
                        "cancelled",
                        "interrupted",
                    } and not events:
                        return
                    await asyncio.sleep(0.5)

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        except ProjectError as error:
            return await project_error_response(error)

    async def system_skills(_: Request) -> Response:
        return JSONResponse(
            {"skills": [summary.as_public_dict() for summary in config.system_skills]}
        )

    app = Starlette(
        debug=False,
        routes=[
            Route("/api/projects/{project_id}/finishing", finishing_status, methods=["GET"]),
            Route("/api/projects/{project_id}/finishing/commands/{command}", finishing_command, methods=["POST"]),
            Route("/api/projects/{project_id}/finishing/{run_id}/commands/cancel", cancel_finishing, methods=["POST"]),
            Route("/api/projects/{project_id}/finishing/{run_id}/commands/resume", resume_finishing, methods=["POST"]),
            Route("/api/projects/{project_id}/finishing/{run_id}/media", finishing_media, methods=["GET", "HEAD"]),
            Route("/api/health", health, methods=["GET"]),
            Route("/api/projects/{project_id}/edit-agent/{run_id}/exports/{revision_id}", edit_agent_export, methods=["GET"]),
            Route("/api/session/connect", connect_local_browser, methods=["POST"]),
            Route("/api/instance/reopen", reopen_instance, methods=["POST"]),
            Route("/api/status", status, methods=["GET"]),
            Route(
                "/api/system/commands/remove-asr",
                remove_asr,
                methods=["POST"],
            ),
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
                "/api/projects/{project_id}/commands/delete",
                delete_project,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/timeline",
                timeline_status,
                methods=["GET"],
            ),
            Route(
                "/api/projects/{project_id}/preview",
                preview_render_status,
                methods=["GET"],
            ),
            Route(
                "/api/projects/{project_id}/preview/commands/start",
                start_preview_render,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/preview/{render_id}/commands/cancel",
                cancel_preview_render,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/preview/{render_id}/media",
                preview_render_media,
                methods=["GET", "HEAD"],
            ),
            Route(
                "/api/projects/{project_id}/annotations/{version_id}/{stream_index}",
                annotation_status,
                methods=["GET"],
            ),
            Route(
                "/api/projects/{project_id}/annotation/commands/save",
                save_annotation,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/media/{version_id}",
                preview_media,
                methods=["GET", "HEAD"],
            ),
            Route(
                "/api/projects/{project_id}/timeline/commands/build",
                build_timeline,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/edit-agent",
                edit_agent_status,
                methods=["GET"],
            ),
            Route(
                "/api/projects/{project_id}/edit-agent/commands/start",
                start_edit_agent,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/edit-agent/commands/accept-reference",
                accept_program_reference,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/edit-agent/{run_id}/commands/cancel",
                cancel_edit_agent,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/timeline-validation",
                timeline_validation_status,
                methods=["GET"],
            ),
            Route(
                "/api/projects/{project_id}/timeline-validation/commands/accept-warnings",
                accept_timeline_warnings,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/planning",
                planning_status,
                methods=["GET"],
            ),
            Route(
                "/api/projects/{project_id}/planning/commands/start",
                start_planning,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/planning/{run_id}/commands/cancel",
                cancel_planning,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/plan/commands/update",
                update_plan,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/plan/commands/confirm",
                confirm_plan,
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
            Route("/api/projects/{project_id}/evidence", evidence_status, methods=["GET"]),
            Route("/api/projects/{project_id}/evidence/commands/{action}", evidence_command, methods=["POST"]),
            Route("/api/projects/{project_id}/evidence/{run_id}/images/{artifact_id}", evidence_image, methods=["GET"]),
            Route(
                "/api/projects/{project_id}/analysis",
                analysis_status,
                methods=["GET"],
            ),
            Route(
                "/api/projects/{project_id}/analyses/{run_id}/results",
                analysis_results,
                methods=["GET"],
            ),
            Route(
                "/api/projects/{project_id}/analyses/{run_id}/items/"
                "{version_id}/{stream_index:int}/transcript",
                transcript_segments,
                methods=["GET"],
            ),
            Route(
                "/api/projects/{project_id}/commands/start-analysis",
                start_analysis,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/analyses/{run_id}/commands/cancel",
                cancel_analysis,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/analyses/{run_id}/commands/resume",
                resume_analysis,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/analyses/{run_id}/events",
                analysis_events,
                methods=["GET"],
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
