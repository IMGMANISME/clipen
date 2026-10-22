"""Installed Clipen command entry point."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Sequence

from clipen.controller import LocalSessionAuthority
from clipen.controller.server import ServerPaths, probe_claude, probe_codex, run_server
from clipen.provider_registry import PROVIDERS, implemented_providers
from clipen.system_skills import verify_system_skill_release
from clipen.workspace import (
    WorkspaceError,
    inspect_workspace_projects,
    prepare_workspace,
)


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
    available = implemented_providers()
    usage = "用法：clipen " + "|".join(available)
    if len(selected) != 1 or selected[0] not in PROVIDERS:
        print(usage, file=sys.stderr)
        return 2
    descriptor = PROVIDERS[selected[0]]
    if not descriptor.implemented:
        print(f"Clipen 目前還不支援 {descriptor.display_name}（{descriptor.provider_id}）。{usage}", file=sys.stderr)
        return 2
    # Resolved at call time so each probe stays a module attribute.
    probe = {"codex": probe_codex, "claude": probe_claude}[descriptor.provider_id]
    try:
        repo_root = _repo_root()
        data_root = _data_root()
        workspace = prepare_workspace(Path.cwd())
        print(f"工作區：{workspace.root}", flush=True)
        print("正在檢查目前目錄的 Clipen 作品…", flush=True)
        inspection = inspect_workspace_projects(workspace)
        if inspection.project_count:
            print(f"✓ 找到 {inspection.project_count} 個可用作品", flush=True)
        else:
            print("目前沒有作品，可在 UI 建立第一個作品。", flush=True)
        attempt_root = data_root / "provider-attempts"
        attempt_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        print("正在驗證系統剪輯規則…", flush=True)
        system_skills = verify_system_skill_release(
            repo_root / "resources" / "system-skills"
        )
        print(f"正在檢查 {descriptor.display_name}…", flush=True)
        provider = probe(attempt_root)
        return run_server(
            ServerPaths(
                static_root=repo_root / "apps" / "web" / "dist",
                attempt_root=attempt_root,
                projects_root=workspace.projects_root,
                instance_root=data_root / "workspace-instances" / workspace.workspace_id,
                asr_requirements=repo_root / "requirements" / "asr-runtime.txt",
                plan_schema=repo_root
                / "schemas"
                / "v2"
                / "plan-proposal.schema.json",
            ),
            provider,
            LocalSessionAuthority(),
            system_skills,
        )
    except KeyboardInterrupt:
        print("\nClipen 已停止", flush=True)
        return 130
    except (OSError, RuntimeError, ValueError, WorkspaceError) as error:
        print(f"Clipen 無法啟動：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
