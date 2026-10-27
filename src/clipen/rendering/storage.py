"""Durable state for local rough-cut preview renders."""

from __future__ import annotations

from pathlib import Path
import secrets
import sqlite3

from clipen.filesystem import restrict_permissions
from clipen.projects import ProjectError, ProjectRepository
from clipen.projects.models import validate_project_id, validate_request_id
from clipen.projects.storage import utc_now


class PreviewRenderRepository:
    def __init__(self, projects: ProjectRepository) -> None:
        self.projects = projects

    def create(
        self, project_id: str, request_id: object, timeline_id: str
    ) -> dict[str, object]:
        validate_project_id(project_id)
        request = validate_request_id(request_id)
        now = utc_now()
        try:
            with self.projects.connect(project_id) as database:
                database.execute("BEGIN IMMEDIATE")
                existing = database.execute(
                    "SELECT * FROM preview_renders WHERE request_id=?", (request,)
                ).fetchone()
                if existing is not None:
                    if existing["timeline_id"] != timeline_id:
                        database.rollback()
                        raise ProjectError(
                            "request_conflict",
                            "這次初剪輸出與既有操作衝突。",
                            status_code=409,
                        )
                    database.rollback()
                    return self._public(existing)
                render_id = "render_" + secrets.token_hex(16)
                database.execute(
                    "INSERT INTO preview_renders "
                    "(render_id,project_id,timeline_id,request_id,state,progress,created_at) "
                    "VALUES(?,?,?,?, 'queued',0,?)",
                    (render_id, project_id, timeline_id, request, now),
                )
                database.execute(
                    "INSERT INTO current_preview_render(slot,render_id) VALUES(1,?) "
                    "ON CONFLICT(slot) DO UPDATE SET render_id=excluded.render_id",
                    (render_id,),
                )
                database.commit()
                return self.get(project_id, render_id)
        except ProjectError:
            raise
        except sqlite3.Error as error:
            raise ProjectError(
                "storage_failed",
                "Clipen 無法建立初剪輸出工作。",
                status_code=500,
            ) from error

    def current(self, project_id: str) -> dict[str, object] | None:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as database:
            row = database.execute(
                "SELECT renders.* FROM current_preview_render current "
                "JOIN preview_renders renders ON renders.render_id=current.render_id WHERE current.slot=1"
            ).fetchone()
        return None if row is None else self._public(row)

    def by_request(
        self, project_id: str, request_id: object
    ) -> dict[str, object] | None:
        validate_project_id(project_id)
        request = validate_request_id(request_id)
        with self.projects.connect(project_id) as database:
            row = database.execute(
                "SELECT * FROM preview_renders WHERE request_id=? AND project_id=?",
                (request, project_id),
            ).fetchone()
        return None if row is None else self._public(row)

    def get(self, project_id: str, render_id: str) -> dict[str, object]:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as database:
            row = database.execute(
                "SELECT * FROM preview_renders WHERE render_id=? AND project_id=?",
                (render_id, project_id),
            ).fetchone()
        if row is None:
            raise ProjectError("preview_not_found", "找不到這次初剪輸出。", status_code=404)
        return self._public(row)

    def finish_unsuccessful(
        self,
        project_id: str,
        render_id: str,
        state: str,
        *,
        error_code: str | None = None,
    ) -> dict[str, object]:
        if state not in {"failed", "cancelled"}:
            raise ValueError("unsuccessful preview state is invalid")
        now = utc_now()
        fields = ["state=?", "error_code=?"]
        values: list[object] = [state, error_code]
        fields.append("completed_at=?")
        values.append(now)
        values.extend([render_id, project_id])
        with self.projects.connect(project_id) as database:
            database.execute(
                f"UPDATE preview_renders SET {','.join(fields)} "
                "WHERE render_id=? AND project_id=?",
                values,
            )
        return self.get(project_id, render_id)

    def mark_running(self, project_id: str, render_id: str) -> dict[str, object]:
        now = utc_now()
        with self.projects.connect(project_id) as database:
            database.execute(
                "UPDATE preview_renders SET state='running',progress=0,"
                "started_at=COALESCE(started_at,?) "
                "WHERE render_id=? AND project_id=? AND state='queued'",
                (now, render_id, project_id),
            )
        return self.get(project_id, render_id)

    def update_progress(
        self, project_id: str, render_id: str, progress: float
    ) -> dict[str, object]:
        normalized = max(0.0, min(1.0, progress))
        with self.projects.connect(project_id) as database:
            database.execute(
                "UPDATE preview_renders SET progress=? "
                "WHERE render_id=? AND project_id=? AND state='running'",
                (normalized, render_id, project_id),
            )
        return self.get(project_id, render_id)

    def request_cancel(self, project_id: str, render_id: str) -> dict[str, object]:
        with self.projects.connect(project_id) as database:
            database.execute(
                "UPDATE preview_renders SET state='cancelling' "
                "WHERE render_id=? AND project_id=? AND state IN ('queued','running')",
                (render_id, project_id),
            )
        return self.get(project_id, render_id)

    def complete(
        self,
        project_id: str,
        render_id: str,
        output_relative_path: str,
        output_size_bytes: int,
    ) -> dict[str, object]:
        with self.projects.connect(project_id) as database:
            database.execute(
                "UPDATE preview_renders SET state='succeeded',progress=1,error_code=NULL,"
                "output_relative_path=?,output_size_bytes=?,completed_at=? "
                "WHERE render_id=? AND project_id=? AND state='running'",
                (
                    output_relative_path,
                    output_size_bytes,
                    utc_now(),
                    render_id,
                    project_id,
                ),
            )
        return self.get(project_id, render_id)

    def interrupt_incomplete(self, project_id: str) -> None:
        with self.projects.connect(project_id) as database:
            database.execute(
                "UPDATE preview_renders SET state='interrupted',error_code='controller_restarted',"
                "completed_at=? WHERE state IN ('queued','running','cancelling')",
                (utc_now(),),
            )

    def interrupt_attempt_renders(self, project_id: str, run_id: str) -> int:
        """End this run's AI previews still marked in progress.

        Agent previews render inside the AI's tool process, so they cannot
        outlive an attempt; a row left running (the Mac slept, the process was
        killed) would otherwise block every later preview as busy.
        """
        with self.projects.connect(project_id) as database:
            cursor = database.execute(
                "UPDATE preview_renders SET state='interrupted',error_code='attempt_ended',completed_at=? "
                "WHERE state IN ('queued','running','cancelling') AND request_id LIKE ? ESCAPE '\\'",
                (utc_now(), 'agent-preview-' + run_id.replace('_', '\\_') + '-%'),
            )
            database.commit()
            return cursor.rowcount

    def cleanup_partial_outputs(self, project_id: str) -> None:
        directory = self.projects.root / project_id / "previews"
        if not directory.exists():
            return
        if directory.is_symlink() or not directory.is_dir():
            raise ProjectError(
                "storage_corrupt", "Clipen 無法安全讀取初剪資料。", status_code=500
            )
        for candidate in directory.glob(".render_*.partial.*"):
            candidate.unlink(missing_ok=True)

    def output_path(
        self,
        project_id: str,
        render_id: str,
        *,
        staging: bool = False,
        prepare: bool = True,
    ) -> Path:
        validate_project_id(project_id)
        root = self.projects.root / project_id
        if root.is_symlink() or not root.is_dir():
            raise ProjectError("project_not_found", "找不到這個作品。", status_code=404)
        directory = root / "previews"
        if prepare:
            directory.mkdir(mode=0o700, exist_ok=True)
        elif not directory.exists():
            name = f".{render_id}.partial.mp4" if staging else f"{render_id}.mp4"
            return directory / name
        if directory.is_symlink() or not directory.is_dir():
            raise ProjectError(
                "storage_corrupt", "Clipen 無法安全建立初剪資料夾。", status_code=500
            )
        if prepare:
            restrict_permissions(directory, 0o700)
        name = f".{render_id}.partial.mp4" if staging else f"{render_id}.mp4"
        return directory / name

    @staticmethod
    def _public(row) -> dict[str, object]:
        return {
            "render_id": row["render_id"],
            "timeline_id": row["timeline_id"],
            "state": row["state"],
            "progress": row["progress"],
            "error_code": row["error_code"],
            "size_bytes": row["output_size_bytes"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "playback_url": (
                f"/api/projects/{row['project_id']}/preview/"
                f"{row['render_id']}/media"
                if row["state"] == "succeeded" and row["output_relative_path"]
                else None
            ),
        }
