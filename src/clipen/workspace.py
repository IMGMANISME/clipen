"""Current-directory Clipen workspace creation and validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from .filesystem import restrict_permissions
from .projects.storage import PROJECT_SCHEMA_VERSION


_WORKSPACE_SCHEMA = 1
_PROJECT_DIRECTORY = re.compile(r"prj_[0-9a-f]{32}\Z")


class WorkspaceError(RuntimeError):
    """The selected working directory cannot safely host Clipen data."""


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    managed_root: Path
    projects_root: Path
    manifest_path: Path
    workspace_id: str
    created: bool


@dataclass(frozen=True)
class WorkspaceInspection:
    project_count: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(
                value,
                output,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_workspace(current_directory: Path) -> WorkspacePaths:
    """Create or reopen only the managed `.clipen` subtree below the cwd."""

    try:
        root = current_directory.resolve(strict=True)
    except OSError as error:
        raise WorkspaceError("Clipen 無法讀取目前的工作目錄。") from error
    if not root.is_dir():
        raise WorkspaceError("目前路徑不是可用的資料夾。")

    managed = root / ".clipen"
    created = not managed.exists()
    try:
        managed.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise WorkspaceError("Clipen 無法在目前目錄建立工作區。") from error
    if managed.is_symlink() or not managed.is_dir():
        raise WorkspaceError("目前目錄中的 .clipen 不是安全的工作區資料夾。")
    restrict_permissions(managed, 0o700)

    manifest = managed / "workspace.json"
    if manifest.exists():
        if manifest.is_symlink() or not manifest.is_file():
            raise WorkspaceError("Clipen 工作區識別檔不安全。")
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise WorkspaceError("Clipen 工作區識別檔已損壞。") from error
        if not isinstance(value, dict) or value.get("schema") != _WORKSPACE_SCHEMA:
            raise WorkspaceError("這個工作區需要其他版本的 Clipen。")
        workspace_id = value.get("workspace_id")
        if (
            not isinstance(workspace_id, str)
            or re.fullmatch(r"wsp_[0-9a-f]{32}", workspace_id) is None
        ):
            raise WorkspaceError("Clipen 工作區識別檔已損壞。")
        created = False
    else:
        try:
            existing_entries = tuple(managed.iterdir())
        except OSError as error:
            raise WorkspaceError("Clipen 無法檢查目前目錄的工作區。") from error
        if existing_entries:
            raise WorkspaceError(
                "目前目錄已有無法識別的 .clipen 資料，Clipen 沒有修改它。"
            )
        created = True
        workspace_id = "wsp_" + uuid.uuid4().hex
        _atomic_json(
            manifest,
            {
                "schema": _WORKSPACE_SCHEMA,
                "workspace_id": workspace_id,
                "created_at": _utc_now(),
            },
        )

    projects = managed / "projects"
    projects.mkdir(mode=0o700, exist_ok=True)
    if projects.is_symlink() or not projects.is_dir():
        raise WorkspaceError("Clipen 工作區中的 projects 不安全。")
    restrict_permissions(projects, 0o700)

    return WorkspacePaths(
        root,
        managed,
        projects,
        manifest,
        workspace_id,
        created,
    )


def inspect_workspace_projects(workspace: WorkspacePaths) -> WorkspaceInspection:
    """Verify every managed project before the provider or Controller starts."""

    try:
        entries = tuple(workspace.projects_root.iterdir())
    except OSError as error:
        raise WorkspaceError("Clipen 無法檢查目前目錄的作品。") from error

    project_count = 0
    for entry in entries:
        if (
            entry.name.startswith("prj_")
            and _PROJECT_DIRECTORY.fullmatch(entry.name) is None
        ):
            raise WorkspaceError(
                f"偵測到無法識別的作品資料夾：{entry.name}。Clipen 沒有修改它。"
            )
        if _PROJECT_DIRECTORY.fullmatch(entry.name) is None:
            continue
        if entry.is_symlink() or not entry.is_dir() or os.path.ismount(entry):
            raise WorkspaceError(
                f"作品 {entry.name} 的資料夾不安全，Clipen 沒有修改它。"
            )
        database = entry / "project.sqlite3"
        try:
            info = database.lstat()
        except OSError as error:
            raise WorkspaceError(
                f"作品 {entry.name} 缺少可讀取的資料庫。"
            ) from error
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceError(
                f"作品 {entry.name} 的資料庫不安全，Clipen 沒有修改它。"
            )

        try:
            uri = f"{database.resolve(strict=True).as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                connection.execute("PRAGMA query_only = ON")
                check = connection.execute("PRAGMA quick_check").fetchone()
                version = connection.execute("PRAGMA user_version").fetchone()
                row = connection.execute(
                    "SELECT project_id FROM project LIMIT 1"
                ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as error:
            raise WorkspaceError(
                f"作品 {entry.name} 的資料庫已損壞或無法讀取。"
            ) from error
        if check is None or check[0] != "ok" or row is None or row[0] != entry.name:
            raise WorkspaceError(
                f"作品 {entry.name} 的資料內容不完整，Clipen 沒有修改它。"
            )
        if (
            version is None
            or not isinstance(version[0], int)
            or not 1 <= version[0] <= PROJECT_SCHEMA_VERSION
        ):
            raise WorkspaceError(
                f"作品 {entry.name} 需要其他版本的 Clipen。"
            )
        project_count += 1

    return WorkspaceInspection(project_count)
