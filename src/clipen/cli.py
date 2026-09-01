"""Installed Clipen command entry point."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Sequence

from clipen.controller import LocalSessionAuthority
from clipen.controller.server import ServerPaths, probe_codex, run_server


def _repo_root() -> Path:
    configured = os.environ.pop("CLIPEN_REPO_ROOT", None)
    root = Path(configured) if configured else Path(__file__).resolve().parents[2]
    resolved = root.resolve(strict=True)
    if not (resolved / "apps" / "web" / "dist" / "index.html").is_file():
        raise RuntimeError("找不到 Clipen UI production bundle")
    return resolved


def _data_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Clipen"
    xdg_data = os.environ.get("XDG_DATA_HOME")
    return Path(xdg_data) / "clipen" if xdg_data else Path.home() / ".local" / "share" / "clipen"


def main(arguments: Sequence[str] | None = None) -> int:
    selected = list(sys.argv[1:] if arguments is None else arguments)
    if selected != ["codex"]:
        print("用法：clipen codex", file=sys.stderr)
        return 2
    try:
        repo_root = _repo_root()
        data_root = _data_root()
        attempt_root = data_root / "provider-attempts"
        attempt_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        print("正在檢查 Codex…", flush=True)
        provider = probe_codex(attempt_root)
        return run_server(
            ServerPaths(
                static_root=repo_root / "apps" / "web" / "dist",
                attempt_root=attempt_root,
                projects_root=data_root / "projects",
            ),
            provider,
            LocalSessionAuthority(),
        )
    except KeyboardInterrupt:
        print("\nClipen 已停止", flush=True)
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Clipen 無法啟動：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
