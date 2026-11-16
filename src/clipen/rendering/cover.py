"""A still from a project's own footage, shown as its cover on the home page."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path

from clipen.media import MediaPreviewRepository
from clipen.projects import ProjectError, ProjectRepository
from clipen.projects.models import validate_project_id

from .storage import PreviewRenderRepository

# Far enough in to skip a black fade-in, then the very first frame for clips shorter than that.
_STILL_OFFSETS = (3.0, 0.0)
_SOURCE_CANDIDATES = 20


class ProjectCoverRepository:
    def __init__(
        self,
        projects: ProjectRepository,
        executable: str | None = None,
        *,
        timeout_seconds: float = 20,
    ) -> None:
        self.projects = projects
        self.executable = executable or shutil.which("ffmpeg")
        self.timeout_seconds = timeout_seconds

    def cover(self, project_id: str) -> Path:
        """The cover still, cut once from the latest finished film or else the first source video."""
        validate_project_id(project_id)
        source = self._source(project_id)
        if source is None:
            raise _unavailable()
        key, media = source
        directory = self._directory(project_id)
        cached = directory / f"{key}.jpg"
        if cached.is_file() and not cached.is_symlink():
            return cached
        if not self.executable or not os.path.isabs(self.executable):
            raise _unavailable()
        staging = directory / f".{secrets.token_hex(8)}.partial.jpg"
        try:
            for offset in _STILL_OFFSETS:
                if self._cut(media, offset, staging):
                    break
            else:
                raise _unavailable()
            # A newer film or source replaces the old still rather than piling up beside it.
            for stale in directory.glob("*.jpg"):
                if not stale.name.startswith("."):
                    stale.unlink(missing_ok=True)
            os.replace(staging, cached)
        finally:
            staging.unlink(missing_ok=True)
        return cached

    def _source(self, project_id: str) -> tuple[str, Path] | None:
        renders = PreviewRenderRepository(self.projects)
        with self.projects.connect(project_id) as database:
            finished = database.execute(
                "SELECT render_id FROM preview_renders WHERE project_id=? AND state='succeeded' "
                "ORDER BY completed_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            videos = database.execute(
                "SELECT versions.version_id FROM asset_versions AS versions "
                "JOIN assets ON assets.asset_id=versions.asset_id "
                "WHERE assets.project_id=? AND assets.role='source' AND versions.probe_status='ready' "
                "AND EXISTS(SELECT 1 FROM media_streams AS streams "
                "WHERE streams.version_id=versions.version_id AND streams.kind='video') "
                "ORDER BY assets.relative_path, versions.created_at DESC LIMIT ?",
                (project_id, _SOURCE_CANDIDATES),
            ).fetchall()
        if finished is not None:
            try:
                return finished["render_id"], renders.media_path(project_id, finished["render_id"])
            except ProjectError:
                pass
        previews = MediaPreviewRepository(self.projects)
        for row in videos:
            try:
                path, _ = previews.locate(project_id, row["version_id"])
            except ProjectError:
                continue
            return row["version_id"], path
        return None

    def _directory(self, project_id: str) -> Path:
        root = self.projects.root / project_id
        if root.is_symlink() or not root.is_dir():
            raise ProjectError("project_not_found", "找不到這個作品。", status_code=404)
        directory = root / "covers"
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise ProjectError("storage_corrupt", "Clipen 無法安全建立封面資料夾。", status_code=500)
        return directory

    def _cut(self, media: Path, offset: float, output: Path) -> bool:
        command = (
            self.executable,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{offset:.3f}",
            "-i",
            str(media),
            "-frames:v",
            "1",
            "-vf",
            "scale=640:-2",
            "-q:v",
            "4",
            "-y",
            str(output),
        )
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0 and output.is_file() and output.stat().st_size > 0


def _unavailable() -> ProjectError:
    return ProjectError("cover_unavailable", "這個作品還沒有可用的封面。", status_code=404)
