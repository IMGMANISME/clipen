"""Bounded descriptor-based discovery below explicitly granted media roots."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import PurePosixPath
import stat
import threading
import time
from typing import Iterator

from clipen.projects.models import DirectoryRole

from .models import AssetKind, InventoryRoot, validate_relative_path


_VIDEO_EXTENSIONS = frozenset(
    {
        ".avi",
        ".braw",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mts",
        ".mxf",
        ".r3d",
        ".webm",
    }
)
_AUDIO_EXTENSIONS = frozenset(
    {".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
)
_IMAGE_EXTENSIONS = frozenset(
    {".bmp", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)
_SYSTEM_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
# Clipen writes Resolve packages here, beside the footage; they are not footage.
EXPORT_FOLDER = "Clipen 匯出"


class DiscoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DiscoveredAsset:
    role: DirectoryRole
    relative_path: str
    kind: AssetKind
    size_bytes: int | None
    mtime_ns: int | None
    issue_code: str | None = None

    def __post_init__(self) -> None:
        validate_relative_path(self.relative_path)
        if (self.size_bytes is None) != (self.mtime_ns is None):
            raise ValueError("discovered file stat must be complete")

    @property
    def should_probe(self) -> bool:
        return self.issue_code is None and self.kind is not AssetKind.UNKNOWN


class MediaDiscovery:
    def __init__(
        self,
        *,
        maximum_files: int = 100_000,
        maximum_depth: int = 32,
        maximum_seconds: float = 10 * 60,
    ) -> None:
        if maximum_files < 1 or maximum_depth < 1 or maximum_seconds <= 0:
            raise ValueError("discovery bounds must be positive")
        self.maximum_files = maximum_files
        self.maximum_depth = maximum_depth
        self.maximum_seconds = maximum_seconds

    def scan(
        self, root: InventoryRoot, cancel: threading.Event | None = None
    ) -> Iterator[DiscoveredAsset]:
        deadline = time.monotonic() + self.maximum_seconds
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            root_fd = os.open(root.canonical_path, flags)
        except OSError as error:
            raise DiscoveryError("root_unavailable") from error
        try:
            identity = os.fstat(root_fd)
        except OSError as error:
            os.close(root_fd)
            raise DiscoveryError("root_unavailable") from error
        if identity.st_ino != root.inode:
            os.close(root_fd)
            raise DiscoveryError("root_changed")
        discovered = [0]
        yield from self._scan_directory(
            root_fd,
            root.role,
            (),
            0,
            deadline,
            flags,
            discovered,
            cancel,
        )

    def _scan_directory(
        self,
        directory_fd: int,
        role: DirectoryRole,
        prefix: tuple[str, ...],
        depth: int,
        deadline: float,
        flags: int,
        discovered: list[int],
        cancel: threading.Event | None,
    ) -> Iterator[DiscoveredAsset]:
        """Depth-first traversal keeps descriptor use bounded by maximum depth."""
        try:
            if cancel is not None and cancel.is_set():
                raise DiscoveryError("cancelled")
            if time.monotonic() >= deadline:
                raise DiscoveryError("discovery_timeout")
            for entry in _entries(directory_fd):
                if cancel is not None and cancel.is_set():
                    raise DiscoveryError("cancelled")
                if time.monotonic() >= deadline:
                    raise DiscoveryError("discovery_timeout")
                name = entry.name
                if not name or name in _SYSTEM_NAMES:
                    continue
                relative_parts = prefix + (name,)
                relative = PurePosixPath(*relative_parts).as_posix()
                try:
                    validate_relative_path(relative)
                except ValueError as error:
                    raise DiscoveryError("path_too_long") from error
                try:
                    if entry.is_symlink():
                        discovered[0] += 1
                        self._check_file_limit(discovered[0])
                        yield DiscoveredAsset(
                            role,
                            relative,
                            AssetKind.UNKNOWN,
                            None,
                            None,
                            "symlink_ignored",
                        )
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if name.startswith(".") or (not prefix and name == EXPORT_FOLDER):
                            continue
                        if depth + 1 > self.maximum_depth:
                            raise DiscoveryError("tree_too_deep")
                        child_fd = os.open(name, flags, dir_fd=directory_fd)
                        yield from self._scan_directory(
                            child_fd,
                            role,
                            relative_parts,
                            depth + 1,
                            deadline,
                            flags,
                            discovered,
                            cancel,
                        )
                        continue
                    file_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    discovered[0] += 1
                    self._check_file_limit(discovered[0])
                    yield DiscoveredAsset(
                        role,
                        relative,
                        AssetKind.UNKNOWN,
                        None,
                        None,
                        "entry_unreadable",
                    )
                    continue
                discovered[0] += 1
                self._check_file_limit(discovered[0])
                if name.startswith(".") or not stat.S_ISREG(file_stat.st_mode):
                    yield DiscoveredAsset(
                        role,
                        relative,
                        AssetKind.UNKNOWN,
                        None,
                        None,
                        "non_media",
                    )
                    continue
                kind = _kind_for_name(name)
                yield DiscoveredAsset(
                    role,
                    relative,
                    kind,
                    file_stat.st_size,
                    file_stat.st_mtime_ns,
                    None if kind is not AssetKind.UNKNOWN else "non_media",
                )
        finally:
            os.close(directory_fd)

    def _check_file_limit(self, discovered: int) -> None:
        if discovered > self.maximum_files:
            raise DiscoveryError("too_many_files")


def _kind_for_name(name: str) -> AssetKind:
    extension = PurePosixPath(name).suffix.lower()
    if extension in _VIDEO_EXTENSIONS:
        return AssetKind.VIDEO
    if extension in _AUDIO_EXTENSIONS:
        return AssetKind.AUDIO
    if extension in _IMAGE_EXTENSIONS:
        return AssetKind.IMAGE
    return AssetKind.UNKNOWN


def _entries(directory_fd: int) -> Iterator[os.DirEntry[str]]:
    try:
        with os.scandir(directory_fd) as iterator:
            yield from iterator
    except OSError as error:
        raise DiscoveryError("directory_unreadable") from error


def open_file_beneath(root: InventoryRoot, relative_path: str) -> int:
    """Open one regular file without following any component symlink."""
    path = PurePosixPath(validate_relative_path(relative_path))
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        current = os.open(root.canonical_path, directory_flags)
    except OSError as error:
        raise DiscoveryError("root_unavailable") from error
    try:
        identity = os.fstat(current)
        if identity.st_ino != root.inode:
            raise DiscoveryError("root_changed")
        for component in path.parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current)
            os.close(current)
            current = next_fd
        file_fd = os.open(path.parts[-1], file_flags, dir_fd=current)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_fd)
            raise DiscoveryError("entry_not_regular")
        return file_fd
    except OSError as error:
        raise DiscoveryError("entry_unreadable") from error
    finally:
        os.close(current)
