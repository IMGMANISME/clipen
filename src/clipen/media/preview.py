"""Read-only, range-capable access to user-authorized source media."""

from __future__ import annotations

from dataclasses import dataclass
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
from typing import Iterator

from clipen.projects import DirectoryRole, ProjectError, ProjectRepository
from clipen.projects.models import validate_project_id

from .discovery import DiscoveryError, open_file_beneath
from .models import InventoryRoot


_VERSION_ID = re.compile(r"av_[0-9a-f]{32}\Z")
_VIDEO_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime"}
_AUDIO_TYPES = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".aac": "audio/aac"}


@dataclass
class MediaPreview:
    descriptor: int
    size: int
    media_type: str
    audio_stream_indices: tuple[int, ...] = ()
    video_stream_indices: tuple[int, ...] = ()

    def chunks(self, start: int, end: int, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        position = start
        try:
            while position <= end:
                chunk = os.pread(self.descriptor, min(chunk_size, end - position + 1), position)
                if not chunk:
                    break
                position += len(chunk)
                yield chunk
        finally:
            os.close(self.descriptor)

    def close(self) -> None:
        os.close(self.descriptor)


class MediaPreviewRepository:
    def __init__(self, projects: ProjectRepository) -> None:
        self.projects = projects

    def locate(self, project_id: str, version_id: str) -> tuple[Path, dict[int, dict]]:
        """The unchanged source file on disk and its probed streams, for handing to another editor."""
        self.open(project_id, version_id).close()
        with self.projects.connect(project_id) as database:
            row = database.execute(
                "SELECT assets.relative_path,directories.canonical_path FROM asset_versions AS versions "
                "JOIN assets ON assets.asset_id=versions.asset_id "
                "JOIN project_directories AS directories ON directories.role=assets.role "
                "WHERE versions.version_id=? AND assets.project_id=?",
                (version_id, project_id),
            ).fetchone()
            streams = database.execute(
                "SELECT stream_index,kind,details_json FROM media_streams WHERE version_id=?",
                (version_id,),
            ).fetchall()
        path = Path(row["canonical_path"]).joinpath(*PurePosixPath(row["relative_path"]).parts)
        return path, {
            stream["stream_index"]: {**json.loads(stream["details_json"]), "kind": stream["kind"]}
            for stream in streams
        }

    def open(self, project_id: str, version_id: str) -> MediaPreview:
        validate_project_id(project_id)
        if not isinstance(version_id, str) or _VERSION_ID.fullmatch(version_id) is None:
            raise ProjectError("media_not_found", "找不到這個素材。", status_code=404)
        try:
            with self.projects.connect(project_id) as database:
                row = database.execute(
                    "SELECT assets.role,assets.relative_path,versions.size_bytes,versions.mtime_ns,"
                    "directories.canonical_path,directories.device,directories.inode,directories.saved_at "
                    "FROM asset_versions AS versions "
                    "JOIN assets ON assets.asset_id=versions.asset_id "
                    "JOIN project_directories AS directories ON directories.role=assets.role "
                    "WHERE versions.version_id=? AND assets.project_id=?",
                    (version_id, project_id),
                ).fetchone()
                streams = database.execute(
                    "SELECT stream_index,kind,details_json FROM media_streams "
                    "WHERE version_id=? ORDER BY stream_index",
                    (version_id,),
                ).fetchall()
            if row is None:
                raise ProjectError("media_not_found", "找不到這個素材。", status_code=404)
            audio_stream_indices = tuple(
                stream["stream_index"] for stream in streams if stream["kind"] == "audio"
            )
            video_streams = [stream for stream in streams if stream["kind"] == "video"]
            primary_video_streams = tuple(
                stream["stream_index"]
                for stream in video_streams
                if not json.loads(stream["details_json"]).get("attached_pic", False)
            )
            video_stream_indices = primary_video_streams or tuple(
                stream["stream_index"] for stream in video_streams
            )
            root = InventoryRoot(
                DirectoryRole(row["role"]), row["canonical_path"], row["device"],
                row["inode"], row["saved_at"],
            )
            descriptor = open_file_beneath(root, row["relative_path"])
            details = os.fstat(descriptor)
            if details.st_size != row["size_bytes"] or details.st_mtime_ns != row["mtime_ns"]:
                os.close(descriptor)
                raise ProjectError(
                    "media_changed", "素材已變更，請重新檢查素材。", status_code=409
                )
            suffix = PurePosixPath(row["relative_path"]).suffix.lower()
            media_type = _VIDEO_TYPES.get(suffix) or _AUDIO_TYPES.get(suffix)
            if media_type is None:
                media_type = mimetypes.guess_type(row["relative_path"])[0]
            if media_type is None or not media_type.startswith(("video/", "audio/")):
                os.close(descriptor)
                raise ProjectError(
                    "media_preview_unsupported",
                    "這個素材無法在瀏覽器中直接播放。",
                    status_code=415,
                )
            return MediaPreview(
                descriptor,
                details.st_size,
                media_type,
                audio_stream_indices,
                video_stream_indices,
            )
        except ProjectError:
            raise
        except DiscoveryError as error:
            raise ProjectError(
                "media_offline", "目前無法讀取素材，請確認外接硬碟已連接。", status_code=409
            ) from error
        except (json.JSONDecodeError, OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise ProjectError(
                "media_unavailable", "Clipen 無法安全開啟這個素材。", status_code=500
            ) from error
