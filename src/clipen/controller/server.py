"""Single-instance Uvicorn lifecycle for the local Clipen workspace."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import errno
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import sys
import threading
import time
from typing import Callable
import webbrowser

try:
    import fcntl as _fcntl
except ImportError:  # Windows does not provide POSIX advisory file locks.
    _fcntl = None

import uvicorn

from clipen.analysis import (
    ASRSetupManager,
    ArtifactStore,
    ManagedAnalysisRuntime,
    PreparationRepository,
)
from clipen.media import InventoryCoordinator, InventoryRepository, MediaPreviewRepository
from clipen.domain import CapabilityProfile
from clipen.editing import (
    EditAgentCoordinator,
    EditAgentRepository,
    TimelineRepository,
    TimelineService,
)
from clipen.planning import PlanningRepository, PlanningService
from clipen.rendering import (
    FfmpegPreviewRenderer,
    PreviewRenderRepository,
    PreviewRenderService,
)
from clipen.providers import CodexExecAdapter, ProviderUnavailable
from clipen.analysis.evidence_service import EvidenceService
from clipen.annotations import AnnotationRepository
from clipen.projects import ProjectWorkspace, TimelineValidationRepository
from clipen.system_skills import VerifiedSystemSkillRelease

from .app import (
    ControllerConfig,
    LocalSessionAuthority,
    ProviderState,
    ProviderStatus,
    create_app,
)


HOST = "127.0.0.1"
PRIMARY_PORT = 4200
LAST_FALLBACK_PORT = 4299
_INSTANCE_SCHEMA = 1


class PortUnavailable(RuntimeError):
    """Every port reserved for the local Clipen UI is unavailable."""


@dataclass(frozen=True)
class ExistingController:
    public_url: str


class _InstanceLease:
    def __init__(self, lock_file, registry_path: Path, secret: str) -> None:
        self._lock_file = lock_file
        self.registry_path = registry_path
        self.secret = secret

    @classmethod
    def acquire(cls, root: Path) -> "_InstanceLease | None":
        if _fcntl is None:
            raise RuntimeError(
                "這個開發版本尚不支援 Windows；本機工作台需要 POSIX 檔案鎖"
            )
        lock_path = root / ".controller.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        lock_file = os.fdopen(descriptor, "r+b")
        try:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return None
        except BaseException:
            lock_file.close()
            raise
        return cls(lock_file, root / ".controller.json", secrets.token_urlsafe(32))

    def publish(self, port: int) -> None:
        payload = json.dumps(
            {"schema": _INSTANCE_SCHEMA, "port": port, "secret": self.secret},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = self.registry_path.with_name(
            f".{self.registry_path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.registry_path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def close(self) -> None:
        try:
            record = _read_instance_record(self.registry_path)
            if record is not None and record[1] == self.secret:
                self.registry_path.unlink(missing_ok=True)
        finally:
            try:
                assert _fcntl is not None
                _fcntl.flock(self._lock_file.fileno(), _fcntl.LOCK_UN)
            finally:
                self._lock_file.close()


@dataclass(frozen=True)
class ServerPaths:
    static_root: Path
    attempt_root: Path
    projects_root: Path | None = None
    instance_root: Path | None = None
    asr_requirements: Path | None = None
    plan_schema: Path | None = None

    def prepare(self) -> None:
        if not self.static_root.resolve(strict=True).is_dir():
            raise ValueError("Clipen UI production bundle is missing")
        data_root = self.attempt_root.parent
        data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if data_root.is_symlink():
            raise ValueError("Clipen data root cannot be a symbolic link")
        data_root.chmod(0o700)
        self.attempt_root.mkdir(mode=0o700, exist_ok=True)
        if self.attempt_root.is_symlink():
            raise ValueError("provider attempt root cannot be a symbolic link")
        self.attempt_root.chmod(0o700)
        if self.projects_root is not None and self.projects_root.is_symlink():
            raise ValueError("projects root cannot be a symbolic link")
        if self.instance_root is not None:
            self.instance_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.instance_root.is_symlink() or not self.instance_root.is_dir():
                raise ValueError("instance root cannot be a symbolic link")
            self.instance_root.chmod(0o700)
        if self.asr_requirements is not None and not self.asr_requirements.resolve(
            strict=True
        ).is_file():
            raise ValueError("ASR runtime requirements are missing")
        if self.plan_schema is not None and (
            self.plan_schema.is_symlink()
            or not self.plan_schema.resolve(strict=True).is_file()
        ):
            raise ValueError("planning proposal schema is missing")


def probe_codex(attempt_root: Path) -> ProviderStatus:
    try:
        adapter = CodexExecAdapter(attempt_root)
        result = adapter.probe()
    except ProviderUnavailable:
        return ProviderStatus("Codex", ProviderState.UNAVAILABLE, "找不到可用的 Codex CLI")
    except Exception:
        return ProviderStatus("Codex", ProviderState.UNAVAILABLE, "Codex 檢查無法完成")

    if not result.authenticated:
        return ProviderStatus("Codex", ProviderState.LOGIN_REQUIRED, "需要先完成 Codex 登入")
    if not result.capabilities.supports_bounded_attempts:
        return ProviderStatus("Codex", ProviderState.UNSUPPORTED, "Codex 版本需要更新")
    if not result.confinement_verified:
        return ProviderStatus(
            "Codex", ProviderState.CONFINEMENT_FAILED, "Codex 安全檢查未通過"
        )
    return ProviderStatus("Codex", ProviderState.READY, "AI 已連接，可以開始使用")


def _bind_socket(
    first_port: int = PRIMARY_PORT,
    last_port: int = LAST_FALLBACK_PORT,
) -> tuple[socket.socket, int]:
    for port in range(first_port, last_port + 1):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((HOST, port))
            listener.listen(2048)
            return listener, port
        except OSError as error:
            listener.close()
            if error.errno != errno.EADDRINUSE:
                raise PortUnavailable(f"localhost:{port} 無法使用") from error
    raise PortUnavailable(
        f"localhost:{first_port}–{last_port} 都已被其他程式使用"
    )


def _read_instance_record(path: Path) -> tuple[int, str] | None:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > 4096
            or info.st_mode & 0o077
        ):
            return None
        raw = os.read(descriptor, 4097)
        if len(raw) > 4096:
            return None
        value = json.loads(raw.decode("utf-8"))
        port = value.get("port") if isinstance(value, dict) else None
        secret = value.get("secret") if isinstance(value, dict) else None
        if (
            value.get("schema") != _INSTANCE_SCHEMA
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not PRIMARY_PORT <= port <= LAST_FALLBACK_PORT
            or not isinstance(secret, str)
            or not 32 <= len(secret) <= 256
        ):
            return None
        return port, secret
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _request_reopen(port: int, secret: str) -> ExistingController | None:
    connection = http.client.HTTPConnection(HOST, port, timeout=0.5)
    try:
        connection.request(
            "POST",
            "/api/instance/reopen",
            body=b"",
            headers={
                "Host": f"localhost:{port}",
                "X-Clipen-Instance": secret,
                "Content-Length": "0",
            },
        )
        response = connection.getresponse()
        body = response.read(4097)
        if response.status != 200 or len(body) > 4096:
            return None
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict) or value.get("running") is not True:
            return None
        public_url = f"http://localhost:{port}"
        return ExistingController(public_url)
    except (OSError, http.client.HTTPException, UnicodeError, json.JSONDecodeError):
        return None
    finally:
        connection.close()


def _find_existing_controller(registry_path: Path) -> ExistingController | None:
    for _ in range(20):
        record = _read_instance_record(registry_path)
        if record is not None:
            existing = _request_reopen(*record)
            if existing is not None:
                return existing
        time.sleep(0.05)
    return None


def _print_ui_url(url: str, *, first_connection: bool = False) -> None:
    label = "首次連線：" if first_connection else "UI："
    print(label, flush=True)
    if sys.stdout.isatty():
        print(f"\033]8;;{url}\033\\{url}\033]8;;\033\\", flush=True)
    else:
        print(url, flush=True)


def _terminal_reopen_loop(
    public_url: str,
    browser_open: Callable[[str], bool],
    should_stop: Callable[[], bool],
) -> None:
    while not should_stop():
        try:
            line = sys.stdin.readline()
        except (OSError, UnicodeError):
            return
        if line == "" or should_stop():
            return
        try:
            opened = browser_open(public_url)
        except Exception:
            opened = False
        if not opened:
            print("瀏覽器未自動開啟。", flush=True)
            _print_ui_url(public_url, first_connection=True)


async def _serve(
    paths: ServerPaths,
    provider: ProviderStatus,
    authority: LocalSessionAuthority,
    system_skills: VerifiedSystemSkillRelease,
    *,
    browser_open: Callable[[str], bool],
    lease: _InstanceLease,
) -> None:
    listener, port = _bind_socket()
    public_url = f"http://localhost:{port}"
    lease.publish(port)
    inventories = None
    preparation_repository = None
    analysis_runtime = None
    evidence_service = None
    planning_service = None
    timeline_service = None
    timeline_validations = None
    annotations = None
    media_previews = None
    preview_renders = None
    edit_agents = None
    try:
        workspace = (
            ProjectWorkspace(paths.projects_root)
            if paths.projects_root is not None
            else None
        )
        if workspace is not None:
            annotations = AnnotationRepository(workspace.repository)
            media_previews = MediaPreviewRepository(workspace.repository)
            planning_repository = PlanningRepository(workspace.repository)
            timeline_validations = TimelineValidationRepository(workspace.repository)
            timeline_repository = TimelineRepository(workspace.repository)
            timeline_service = TimelineService(
                timeline_repository,
                planning_repository,
                timeline_validations,
                CapabilityProfile("clipen-export-unverified", 1),
            )
            preview_renders = PreviewRenderService(
                PreviewRenderRepository(workspace.repository),
                timeline_repository,
                FfmpegPreviewRenderer(media_previews),
                timeline_service,
            )
            preview_renders.recover(
                project.project_id for project in workspace.repository.list_projects()
            )
            edit_agent_repository = EditAgentRepository(workspace.repository)
            if provider.state is ProviderState.READY:
                edit_agents = EditAgentCoordinator(
                    edit_agent_repository,
                    paths.attempt_root,
                )
                edit_agents.recover()
            else:
                for project in workspace.repository.list_projects():
                    edit_agent_repository.interrupt_incomplete(project.project_id)
            repository = InventoryRepository(workspace.repository)
            inventories = InventoryCoordinator(repository)
            inventories.recover_incomplete(
                project.project_id for project in workspace.repository.list_projects()
            )
            evidence_service = EvidenceService(workspace.repository, paths.attempt_root if provider.state is ProviderState.READY else None)
            evidence_service.recover()
            if paths.plan_schema is not None:
                planning_service = PlanningService(
                    planning_repository,
                    paths.attempt_root,
                    paths.plan_schema,
                    system_skills,
                )
                planning_service.recover()
            preparation_repository = PreparationRepository(workspace.repository)
            for project in workspace.repository.list_projects():
                preparation_repository.interrupt_incomplete(project.project_id)
            if paths.asr_requirements is not None:
                analysis_runtime = ManagedAnalysisRuntime(
                    preparation_repository,
                    ArtifactStore(workspace.repository),
                    ASRSetupManager(
                        paths.attempt_root.parent,
                        paths.asr_requirements,
                    ),
                )
        app = create_app(
            ControllerConfig(
                paths.static_root,
                system_skills.summaries,
                origin=public_url,
                allowed_hosts=(f"localhost:{port}", f"127.0.0.1:{port}"),
                instance_secret=lease.secret,
            ),
            provider,
            authority,
            workspace,
            inventories,
            preparation_repository=preparation_repository,
            analysis_runtime=analysis_runtime,
            evidence_service=evidence_service,
            timeline_validations=timeline_validations,
            planning_service=planning_service,
            timeline_service=timeline_service,
            annotations=annotations,
            media_previews=media_previews,
            preview_renders=preview_renders,
            edit_agents=edit_agents,
        )
        config = uvicorn.Config(
            app,
            host=HOST,
            port=port,
            log_level="critical",
            access_log=False,
            server_header=False,
            date_header=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        async def announce_when_started() -> None:
            while not server.started and not server.should_exit:
                await asyncio.sleep(0.02)
            if server.started:
                print("Clipen 已啟動", flush=True)
                if provider.state is ProviderState.READY:
                    print(f"AI：{provider.name} 可用", flush=True)
                else:
                    print(f"AI：{provider.name} 需要處理", flush=True)
                _print_ui_url(public_url)
                print("狀態：等待 UI 操作", flush=True)
                try:
                    opened = browser_open(public_url)
                except Exception:
                    opened = False
                if not opened:
                    print("瀏覽器未自動開啟。", flush=True)
                    _print_ui_url(public_url, first_connection=True)
                if sys.stdin.isatty():
                    print("按 Enter 重新開啟 UI · 按 Ctrl+C 關閉 Clipen", flush=True)
                    threading.Thread(
                        target=_terminal_reopen_loop,
                        args=(
                            public_url,
                            browser_open,
                            lambda: server.should_exit,
                        ),
                        name="clipen-terminal-reopen",
                        daemon=True,
                    ).start()
                else:
                    print("按 Ctrl+C 關閉 Clipen", flush=True)

        announcement = asyncio.create_task(announce_when_started())
        previous_hup = None
        if hasattr(signal, "SIGHUP"):
            previous_hup = signal.getsignal(signal.SIGHUP)
            signal.signal(signal.SIGHUP, lambda *_: setattr(server, "should_exit", True))
        try:
            await server.serve(sockets=[listener])
        finally:
            server.should_exit = True
            await announcement
            if hasattr(signal, "SIGHUP") and previous_hup is not None:
                signal.signal(signal.SIGHUP, previous_hup)
    finally:
        if edit_agents is not None:
            edit_agents.close()
        if preview_renders is not None:
            preview_renders.close()
        if planning_service is not None:
            planning_service.close()
        if evidence_service is not None:
            evidence_service.close()
        if analysis_runtime is not None:
            analysis_runtime.close()
        if inventories is not None:
            inventories.close()
        listener.close()


def run_server(
    paths: ServerPaths,
    provider: ProviderStatus,
    authority: LocalSessionAuthority,
    system_skills: VerifiedSystemSkillRelease,
    *,
    browser_open: Callable[[str], bool] = webbrowser.open,
) -> int:
    paths.prepare()
    instance_root = paths.instance_root or paths.attempt_root.parent
    lease = _InstanceLease.acquire(instance_root)
    if lease is None:
        existing = _find_existing_controller(instance_root / ".controller.json")
        if existing is None:
            print("Clipen 無法連接正在啟動的本機工作台。請稍後再試。")
            return 1
        print("Clipen 已在執行", flush=True)
        _print_ui_url(existing.public_url)
        try:
            opened = browser_open(existing.public_url)
        except Exception:
            opened = False
        if not opened:
            print("瀏覽器未自動開啟。", flush=True)
            _print_ui_url(existing.public_url, first_connection=True)
        return 0
    try:
        asyncio.run(
            _serve(
                paths,
                provider,
                authority,
                system_skills,
                browser_open=browser_open,
                lease=lease,
            )
        )
    except PortUnavailable as error:
        print(f"Clipen 無法啟動：{error}")
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        lease.close()
    print("Clipen 已停止", flush=True)
    return 0
