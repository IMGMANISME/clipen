"""Uvicorn lifecycle for the fixed local Clipen workspace URL."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import signal
import socket
from typing import Callable
import webbrowser

import uvicorn

from clipen.providers import CodexExecAdapter, ProviderUnavailable

from .app import (
    ControllerConfig,
    LocalSessionAuthority,
    ProviderState,
    ProviderStatus,
    create_app,
)


HOST = "127.0.0.1"
PORT = 4200
PUBLIC_URL = "http://localhost:4200"


class PortUnavailable(RuntimeError):
    """The fixed Clipen port is already occupied."""


@dataclass(frozen=True)
class ServerPaths:
    static_root: Path
    attempt_root: Path

    def prepare(self) -> None:
        if not self.static_root.resolve(strict=True).is_dir():
            raise ValueError("Clipen UI production bundle is missing")
        self.attempt_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.attempt_root.is_symlink():
            raise ValueError("provider attempt root cannot be a symbolic link")


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


def _bind_socket() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HOST, PORT))
        listener.listen(2048)
    except OSError as error:
        listener.close()
        raise PortUnavailable(f"localhost:{PORT} 已被其他程式使用") from error
    return listener


async def _serve(
    paths: ServerPaths,
    provider: ProviderStatus,
    authority: LocalSessionAuthority,
    *,
    browser_open: Callable[[str], bool],
) -> None:
    listener = _bind_socket()
    try:
        app = create_app(ControllerConfig(paths.static_root), provider, authority)
        config = uvicorn.Config(
            app,
            host=HOST,
            port=PORT,
            log_level="critical",
            access_log=False,
            server_header=False,
            date_header=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        connect_url = f"{PUBLIC_URL}/#bootstrap={authority.bootstrap_token}"

        async def announce_when_started() -> None:
            while not server.started and not server.should_exit:
                await asyncio.sleep(0.02)
            if server.started:
                print("Clipen 已啟動", flush=True)
                print(f"AI：{provider.message}", flush=True)
                print(f"UI：{PUBLIC_URL}", flush=True)
                print("狀態：等待 UI 操作", flush=True)
                print("按 Ctrl+C 關閉 Clipen", flush=True)
                try:
                    opened = browser_open(connect_url)
                except Exception:
                    opened = False
                if not opened:
                    print(f"瀏覽器未自動開啟，首次連線：{connect_url}", flush=True)

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
        listener.close()


def run_server(
    paths: ServerPaths,
    provider: ProviderStatus,
    authority: LocalSessionAuthority,
    *,
    browser_open: Callable[[str], bool] = webbrowser.open,
) -> int:
    paths.prepare()
    try:
        asyncio.run(_serve(paths, provider, authority, browser_open=browser_open))
    except PortUnavailable as error:
        print(f"Clipen 無法啟動：{error}")
        return 1
    except KeyboardInterrupt:
        pass
    print("Clipen 已停止", flush=True)
    return 0
