"""Narrow native folder selection without exposing an arbitrary path API."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import threading
import time
from typing import Callable

from .models import (
    DirectoryAvailability,
    DirectoryReference,
    DirectoryRole,
    ProjectError,
)
from .storage import utc_now


_SELECTION_LIFETIME_SECONDS = 10 * 60
_MAX_PICKER_OUTPUT_BYTES = 8192
_PICKER_TIMEOUT_SECONDS = 5 * 60


class NativeDirectoryPicker:
    """Select exactly one directory through a user-visible native dialog."""

    def choose(self, role: DirectoryRole) -> Path | None:
        raise NotImplementedError


class MacOSDirectoryPicker(NativeDirectoryPicker):
    _PROMPTS = {
        DirectoryRole.SOURCE: "選擇毛片資料夾",
        DirectoryRole.REFERENCE: "選擇參考作品資料夾",
    }

    def choose(self, role: DirectoryRole) -> Path | None:
        if sys.platform != "darwin":
            raise ProjectError(
                "picker_unsupported",
                "這個版本目前只支援 macOS 資料夾選擇器。",
                status_code=501,
            )
        script = (
            "use scripting additions\n"
            "try\n"
            "  tell current application to activate\n"
            f'  set selectedFolder to choose folder with prompt "{self._PROMPTS[role]}"\n'
            "  return POSIX path of selectedFolder\n"
            "on error number -128\n"
            '  return ""\n'
            "end try"
        )
        try:
            completed = subprocess.run(
                ("/usr/bin/osascript", "-e", script),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=_PICKER_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise ProjectError(
                "picker_timeout", "資料夾選擇已逾時，請再試一次。", status_code=408
            ) from error
        except OSError as error:
            raise ProjectError(
                "picker_unavailable", "無法開啟 macOS 資料夾選擇器。", status_code=500
            ) from error
        if completed.returncode != 0:
            raise ProjectError(
                "picker_failed", "無法完成資料夾選擇。", status_code=500
            )
        if len(completed.stdout) > _MAX_PICKER_OUTPUT_BYTES:
            raise ProjectError(
                "picker_failed", "資料夾選擇器回傳的路徑過長。", status_code=500
            )
        try:
            output = completed.stdout.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise ProjectError(
                "picker_failed", "資料夾選擇器回傳無法辨識的路徑。", status_code=500
            ) from error
        if not output:
            return None
        if "\x00" in output or not output.startswith("/"):
            raise ProjectError(
                "picker_failed", "資料夾選擇器回傳無效的路徑。", status_code=500
            )
        return Path(output)


@dataclass(frozen=True)
class DirectorySelection:
    token: str
    reference: DirectoryReference
    expires_at: float

    def to_public(self) -> dict[str, object]:
        return {
            "selection_token": self.token,
            "role": self.reference.role.value,
            "display_path": self.reference.display_path,
            "display_name": self.reference.display_name,
            "expires_in_seconds": _SELECTION_LIFETIME_SECONDS,
        }


def inspect_directory(
    selected_path: Path,
    role: DirectoryRole,
    managed_root: Path,
) -> DirectoryReference:
    if not selected_path.is_absolute() or "\x00" in str(selected_path):
        raise ProjectError("directory_invalid", "選取的資料夾路徑無效。", status_code=422)
    try:
        selected_info = selected_path.lstat()
        if stat.S_ISLNK(selected_info.st_mode):
            raise ProjectError(
                "directory_symlink",
                "請選擇實際資料夾，不要選擇 symbolic link。",
                status_code=422,
            )
        canonical = selected_path.resolve(strict=True)
        info = canonical.stat()
    except FileNotFoundError as error:
        raise ProjectError(
            "directory_offline", "找不到選取的資料夾或外接磁碟。", status_code=422
        ) from error
    except PermissionError as error:
        raise ProjectError(
            "directory_permission",
            "Clipen 沒有讀取這個資料夾的權限。",
            status_code=422,
        ) from error
    if not stat.S_ISDIR(info.st_mode):
        raise ProjectError("directory_invalid", "請選擇一個資料夾。", status_code=422)
    canonical_managed = managed_root.resolve(strict=True)
    if (
        canonical == canonical_managed
        or canonical.is_relative_to(canonical_managed)
        or canonical_managed.is_relative_to(canonical)
    ):
        raise ProjectError(
            "directory_managed_overlap",
            "素材資料夾不能包含 Clipen 的管理資料。",
            status_code=422,
        )
    if not os.access(canonical, os.R_OK | os.X_OK):
        raise ProjectError(
            "directory_permission",
            "Clipen 沒有讀取這個資料夾的權限。",
            status_code=422,
        )
    display_path = str(canonical)
    try:
        relative_home = canonical.relative_to(Path.home())
        display_path = "~" if not relative_home.parts else f"~/{relative_home}"
    except ValueError:
        pass
    return DirectoryReference(
        role=role,
        canonical_path=str(canonical),
        display_path=display_path,
        display_name=canonical.name or str(canonical),
        device=int(info.st_dev),
        inode=int(info.st_ino),
        saved_at=utc_now(),
    )


def refresh_availability(reference: DirectoryReference) -> DirectoryReference:
    path = Path(reference.canonical_path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return replace(reference, availability=DirectoryAvailability.OFFLINE)
    except PermissionError:
        return replace(reference, availability=DirectoryAvailability.PERMISSION_REQUIRED)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return replace(reference, availability=DirectoryAvailability.CHANGED)
    if not os.access(path, os.R_OK | os.X_OK):
        return replace(reference, availability=DirectoryAvailability.PERMISSION_REQUIRED)
    if int(info.st_dev) != reference.device or int(info.st_ino) != reference.inode:
        return replace(reference, availability=DirectoryAvailability.CHANGED)
    return replace(reference, availability=DirectoryAvailability.AVAILABLE)


class SelectionAuthority:
    """Keep selected paths in Controller memory until a bounded save command uses them."""

    def __init__(
        self,
        managed_root: Path,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._managed_root = managed_root
        self._now = now
        self._selections: dict[str, DirectorySelection] = {}
        self._lock = threading.Lock()

    def register(self, path: Path, role: DirectoryRole) -> DirectorySelection:
        reference = inspect_directory(path, role, self._managed_root)
        token = secrets.token_urlsafe(32)
        selection = DirectorySelection(
            token=token,
            reference=reference,
            expires_at=self._now() + _SELECTION_LIFETIME_SECONDS,
        )
        with self._lock:
            self._prune()
            self._selections[token] = selection
        return selection

    def resolve(self, token: object, role: DirectoryRole) -> DirectoryReference:
        if not isinstance(token, str) or len(token) < 32 or len(token) > 256:
            raise ProjectError(
                "selection_invalid", "資料夾選擇已失效，請重新選擇。", status_code=422
            )
        with self._lock:
            self._prune()
            selection = self._selections.get(token)
        if selection is None or selection.reference.role is not role:
            raise ProjectError(
                "selection_invalid", "資料夾選擇已失效，請重新選擇。", status_code=422
            )
        return inspect_directory(
            Path(selection.reference.canonical_path), role, self._managed_root
        )

    def _prune(self) -> None:
        current = self._now()
        expired = [
            token for token, selection in self._selections.items()
            if selection.expires_at < current
        ]
        for token in expired:
            self._selections.pop(token, None)
