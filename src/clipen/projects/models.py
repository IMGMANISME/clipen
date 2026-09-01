"""Validated project workspace values shared by storage and the Controller."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Mapping


_PROJECT_ID = re.compile(r"prj_[0-9a-f]{32}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")


class ProjectError(RuntimeError):
    """A project command failed with a stable product-facing error."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class DirectoryRole(str, Enum):
    SOURCE = "source"
    REFERENCE = "reference"


class DirectoryAvailability(str, Enum):
    AVAILABLE = "available"
    OFFLINE = "offline"
    CHANGED = "changed"
    PERMISSION_REQUIRED = "permission_required"


def validate_project_id(value: str) -> str:
    if not isinstance(value, str) or _PROJECT_ID.fullmatch(value) is None:
        raise ProjectError("project_not_found", "找不到這個作品。", status_code=404)
    return value


def validate_request_id(value: object) -> str:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise ProjectError("request_invalid", "這次操作的識別資訊無效。", status_code=422)
    return value


def validate_name(value: object) -> str:
    if not isinstance(value, str):
        raise ProjectError("name_invalid", "請輸入作品名稱。", status_code=422)
    name = value.strip()
    if not name or len(name) > 120 or any(ord(character) < 32 for character in name):
        raise ProjectError("name_invalid", "作品名稱需為 1 至 120 個字。", status_code=422)
    return name


def validate_brief(value: object) -> str:
    if not isinstance(value, str):
        raise ProjectError("brief_invalid", "剪輯需求格式無效。", status_code=422)
    brief = value.strip()
    if len(brief) > 8000 or "\x00" in brief:
        raise ProjectError("brief_invalid", "剪輯需求不能超過 8,000 個字。", status_code=422)
    return brief


def validate_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProjectError("revision_invalid", "作品版本資訊無效。", status_code=422)
    return value


@dataclass(frozen=True)
class DirectoryReference:
    role: DirectoryRole
    canonical_path: str
    display_path: str
    display_name: str
    device: int
    inode: int
    saved_at: str
    availability: DirectoryAvailability = DirectoryAvailability.AVAILABLE

    def __post_init__(self) -> None:
        if not self.canonical_path.startswith("/") or "\x00" in self.canonical_path:
            raise ValueError("directory canonical path must be an absolute POSIX path")
        if not self.display_path or len(self.display_path) > 4096:
            raise ValueError("directory display path must be non-empty and bounded")
        if not self.display_name or len(self.display_name) > 255:
            raise ValueError("directory display name must be non-empty and bounded")
        if self.device < 0 or self.inode < 0:
            raise ValueError("directory identity must be non-negative")

    def to_storage(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "canonical_path": self.canonical_path,
            "display_path": self.display_path,
            "display_name": self.display_name,
            "device": self.device,
            "inode": self.inode,
            "saved_at": self.saved_at,
            "availability": self.availability.value,
        }

    def to_public(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "display_path": self.display_path,
            "display_name": self.display_name,
            "availability": self.availability.value,
        }

    @classmethod
    def from_storage(cls, value: Mapping[str, Any]) -> "DirectoryReference":
        return cls(
            role=DirectoryRole(value["role"]),
            canonical_path=str(value["canonical_path"]),
            display_path=str(value["display_path"]),
            display_name=str(value["display_name"]),
            device=int(value["device"]),
            inode=int(value["inode"]),
            saved_at=str(value["saved_at"]),
            availability=DirectoryAvailability(value.get("availability", "available")),
        )


@dataclass(frozen=True)
class ProjectSnapshot:
    project_id: str
    name: str
    brief: str
    stage: str
    revision: int
    created_at: str
    updated_at: str
    directories: tuple[DirectoryReference, ...] = ()

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        validate_name(self.name)
        validate_brief(self.brief)
        validate_revision(self.revision)
        if self.stage != "materials":
            raise ValueError("unsupported project stage")
        roles = [reference.role for reference in self.directories]
        if len(roles) != len(set(roles)):
            raise ValueError("project directory roles must be unique")

    def to_storage(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "brief": self.brief,
            "stage": self.stage,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "directories": [reference.to_storage() for reference in self.directories],
        }

    def to_public(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "brief": self.brief,
            "stage": self.stage,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "directories": [reference.to_public() for reference in self.directories],
        }

    @classmethod
    def from_storage(cls, value: Mapping[str, Any]) -> "ProjectSnapshot":
        raw_directories = value.get("directories", [])
        if not isinstance(raw_directories, list):
            raise ValueError("stored project directories must be a list")
        return cls(
            project_id=str(value["project_id"]),
            name=str(value["name"]),
            brief=str(value["brief"]),
            stage=str(value["stage"]),
            revision=int(value["revision"]),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            directories=tuple(
                DirectoryReference.from_storage(item) for item in raw_directories
            ),
        )
