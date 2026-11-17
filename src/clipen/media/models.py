"""Validated values for durable, source-read-only media inventories."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Mapping

from clipen.projects.models import DirectoryRole, ProjectError, validate_project_id

_RUN_ID = re.compile(r"inv_[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class InventoryState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class AssetKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    UNKNOWN = "unknown"


class ItemStatus(str, Enum):
    USABLE = "usable"
    NEEDS_ATTENTION = "needs_attention"
    IGNORED = "ignored"


class ProbeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    UNSUPPORTED = "unsupported"
    UNREADABLE = "unreadable"
    TIMEOUT = "timeout"
    CHANGED = "changed"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class InventoryCounts:
    discovered: int = 0
    probed: int = 0
    usable: int = 0
    issues: int = 0
    ignored: int = 0

    def __post_init__(self) -> None:
        values = (self.discovered, self.probed, self.usable, self.issues, self.ignored)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("inventory counts must be non-negative integers")
        if self.usable + self.issues + self.ignored > self.discovered:
            raise ValueError("inventory categorized counts exceed discovered count")
        if self.probed > self.discovered:
            raise ValueError("inventory probed count exceeds discovered count")

    def to_public(self) -> dict[str, int]:
        return {
            "discovered": self.discovered,
            "probed": self.probed,
            "usable": self.usable,
            "issues": self.issues,
            "ignored": self.ignored,
        }


@dataclass(frozen=True)
class InventoryRoot:
    role: DirectoryRole
    canonical_path: str
    device: int
    inode: int
    saved_at: str

    def __post_init__(self) -> None:
        if not self.canonical_path.startswith("/") or "\x00" in self.canonical_path:
            raise ValueError("inventory root path must be absolute")
        if self.device < 0 or self.inode < 0 or not self.saved_at:
            raise ValueError("inventory root identity is invalid")

    def to_storage(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "canonical_path": self.canonical_path,
            "device": self.device,
            "inode": self.inode,
            "saved_at": self.saved_at,
        }

    @classmethod
    def from_storage(cls, value: Mapping[str, Any]) -> "InventoryRoot":
        return cls(
            DirectoryRole(value["role"]),
            str(value["canonical_path"]),
            int(value["device"]),
            int(value["inode"]),
            str(value["saved_at"]),
        )


@dataclass(frozen=True)
class InventoryRun:
    run_id: str
    project_id: str
    project_revision: int
    input_fingerprint: str
    roots: tuple[InventoryRoot, ...]
    state: InventoryState
    phase: str
    counts: InventoryCounts
    error_code: str | None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        validate_project_id(self.project_id)
        if self.project_revision < 1 or not _SHA256.fullmatch(self.input_fingerprint):
            raise ValueError("inventory run identity is invalid")
        if not self.roots or len({root.role for root in self.roots}) != len(self.roots):
            raise ValueError("inventory roots must be non-empty and unique")
        if DirectoryRole.SOURCE not in {root.role for root in self.roots}:
            raise ValueError("inventory requires a source root")
        if not _CODE.fullmatch(self.phase) or not self.created_at:
            raise ValueError("inventory lifecycle metadata is invalid")
        if self.error_code is not None and not _CODE.fullmatch(self.error_code):
            raise ValueError("inventory error code is invalid")
        terminal = self.state in {
            InventoryState.SUCCEEDED,
            InventoryState.FAILED,
            InventoryState.CANCELLED,
            InventoryState.INTERRUPTED,
        }
        if terminal != (self.completed_at is not None):
            raise ValueError("inventory completion timestamp does not match state")
        if self.state is not InventoryState.QUEUED and self.started_at is None:
            raise ValueError("started inventory state requires started_at")

    def to_public(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "project_revision": self.project_revision,
            "roles": [root.role.value for root in self.roots],
            "state": self.state.value,
            "phase": self.phase,
            "counts": self.counts.to_public(),
            "error_code": self.error_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class MediaStream:
    stream_index: int
    kind: str
    codec: str
    details: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.stream_index < 0 or self.kind not in {"video", "audio"}:
            raise ValueError("media stream identity is invalid")
        if not self.codec or len(self.codec) > 80:
            raise ValueError("media stream codec is invalid")
        encoded = json.dumps(self.details, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 16_384:
            raise ValueError("media stream details are too large")


@dataclass(frozen=True)
class AssetCandidate:
    role: DirectoryRole
    relative_path: str
    kind: AssetKind
    status: ItemStatus
    issue_code: str | None
    size_bytes: int | None
    mtime_ns: int | None
    fingerprint: str | None
    probe_status: ProbeStatus | None
    probe: Mapping[str, object] | None = None
    streams: tuple[MediaStream, ...] = ()

    def __post_init__(self) -> None:
        validate_relative_path(self.relative_path)
        if self.issue_code is not None and not _CODE.fullmatch(self.issue_code):
            raise ValueError("asset issue code is invalid")
        version_values = (self.size_bytes, self.mtime_ns, self.fingerprint, self.probe_status)
        has_version = any(value is not None for value in version_values)
        if has_version and any(value is None for value in version_values):
            raise ValueError("asset version fields must be provided together")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("asset size must be non-negative")
        if self.mtime_ns is not None and self.mtime_ns < 0:
            raise ValueError("asset mtime must be non-negative")
        if self.fingerprint is not None and not _SHA256.fullmatch(self.fingerprint):
            raise ValueError("asset fingerprint is invalid")
        if self.probe is not None:
            if not has_version:
                raise ValueError("asset probe metadata requires a version")
            encoded = json.dumps(self.probe, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 256_000:
                raise ValueError("asset probe metadata is too large")
        if self.streams and self.probe_status is not ProbeStatus.READY:
            raise ValueError("streams require a ready probe")
        indices = [stream.stream_index for stream in self.streams]
        if len(indices) != len(set(indices)):
            raise ValueError("media stream indices must be unique")
        if self.status is ItemStatus.USABLE:
            if self.probe_status is not ProbeStatus.READY or self.issue_code is not None:
                raise ValueError("usable assets require a ready probe without an issue")
        elif self.issue_code is None:
            raise ValueError("non-usable assets require an issue code")

    @property
    def display_name(self) -> str:
        return PurePosixPath(self.relative_path).name

    def asset_id(self, project_id: str) -> str:
        validate_project_id(project_id)
        value = f"clipen-asset:v1:{project_id}:{self.role.value}:{self.relative_path}"
        return "ast_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    def version_id(self, project_id: str) -> str | None:
        if self.fingerprint is None:
            return None
        value = f"clipen-version:v1:{self.asset_id(project_id)}:{self.size_bytes}:{self.mtime_ns}:{self.fingerprint}"
        return "av_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class InventoryItem:
    asset_id: str
    role: DirectoryRole
    relative_path: str
    display_name: str
    kind: AssetKind
    status: ItemStatus
    issue_code: str | None
    size_bytes: int | None
    probe_status: ProbeStatus | None
    probe: Mapping[str, object] | None = None
    streams: tuple[MediaStream, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"ast_[0-9a-f]{32}", self.asset_id):
            raise ValueError("inventory item identity is invalid")
        validate_relative_path(self.relative_path)
        if self.display_name != PurePosixPath(self.relative_path).name:
            raise ValueError("inventory item display name is invalid")
        if self.issue_code is not None and not _CODE.fullmatch(self.issue_code):
            raise ValueError("inventory item issue code is invalid")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("inventory item size is invalid")
        if self.status is ItemStatus.USABLE and self.probe_status is not ProbeStatus.READY:
            raise ValueError("usable inventory item must have a ready probe")
        if self.status is not ItemStatus.USABLE and self.issue_code is None:
            raise ValueError("non-usable inventory item requires an issue")
        if self.probe is not None:
            encoded = json.dumps(self.probe, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 256_000:
                raise ValueError("inventory item probe metadata is too large")
        if self.streams and self.probe_status is not ProbeStatus.READY:
            raise ValueError("inventory item streams require a ready probe")

    def to_public(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "role": self.role.value,
            "relative_path": self.relative_path,
            "display_name": self.display_name,
            "kind": self.kind.value,
            "status": self.status.value,
            "issue_code": self.issue_code,
            "size_bytes": self.size_bytes,
            "probe_status": None if self.probe_status is None else self.probe_status.value,
            "probe": self.probe,
            "streams": [
                {
                    "stream_index": stream.stream_index,
                    "kind": stream.kind,
                    "codec": stream.codec,
                    "details": stream.details,
                }
                for stream in self.streams
            ],
        }


@dataclass(frozen=True)
class InventoryPage:
    items: tuple[InventoryItem, ...]
    total: int
    offset: int
    limit: int

    def __post_init__(self) -> None:
        if self.total < 0 or self.offset < 0 or not 1 <= self.limit <= 200:
            raise ValueError("inventory page bounds are invalid")
        if len(self.items) > self.limit:
            raise ValueError("inventory page contents are invalid")

    def to_public(self) -> dict[str, object]:
        next_offset = self.offset + len(self.items)
        return {
            "items": [item.to_public() for item in self.items],
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "next_offset": next_offset if next_offset < self.total else None,
        }


def validate_run_id(value: object) -> str:
    if not isinstance(value, str) or _RUN_ID.fullmatch(value) is None:
        raise ProjectError("inventory_not_found", "找不到這次素材檢查。", status_code=404)
    return value


def validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError("asset relative path is invalid")
    if "\x00" in value or "\\" in value:
        raise ValueError("asset relative path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("asset relative path is unsafe")
    return value


def make_run_id(project_id: str, request_id: str) -> str:
    validate_project_id(project_id)
    value = f"clipen-inventory:v1:{project_id}:{request_id}"
    return "inv_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
