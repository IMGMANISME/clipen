"""Background orchestration for local rough-cut preview rendering."""

from __future__ import annotations

import os
import threading
import time
from typing import Protocol

from clipen.editing import TimelineRepository
from clipen.filesystem import restrict_permissions
from clipen.projects import ProjectError

from .renderer import PreviewRenderError
from .storage import PreviewRenderRepository


class PreviewRenderer(Protocol):
    def render(self, project_id, timeline, output, cancel, progress) -> None: ...


class TimelineStatus(Protocol):
    def status(self, project_id: str) -> dict[str, object]: ...


class PreviewRenderService:
    def __init__(
        self,
        renders: PreviewRenderRepository,
        timelines: TimelineRepository,
        renderer: PreviewRenderer,
        timeline_status: TimelineStatus | None = None,
    ) -> None:
        self.renders = renders
        self.timelines = timelines
        self.renderer = renderer
        self.timeline_status = timeline_status
        self._lock = threading.Lock()
        self._cancels: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}

    def recover(self, project_ids) -> None:
        for project_id in project_ids:
            self.renders.interrupt_incomplete(project_id)
            self.renders.cleanup_partial_outputs(project_id)

    def status(self, project_id: str) -> dict[str, object]:
        render = self.renders.current(project_id)
        timeline = self.timelines.current(project_id)
        can_preview = timeline is not None and self._can_preview(project_id)
        if render is None:
            return {"render": None, "can_start": can_preview}
        return {
            "render": render,
            "can_start": can_preview
            and render["state"] not in {"queued", "running", "cancelling"},
            "stale": timeline is None
            or render["timeline_id"] != timeline.timeline.timeline_id,
        }

    def start(
        self, project_id: str, request_id: object, timeline_id: object
    ) -> dict[str, object]:
        timeline = self.timelines.current(project_id)
        if (
            timeline is None
            or not isinstance(timeline_id, str)
            or timeline.timeline.timeline_id != timeline_id
        ):
            raise ProjectError(
                "timeline_revision_conflict",
                "剪輯版本已更新，請重新整理後再輸出。",
                status_code=409,
            )
        if not self._can_preview(project_id):
            raise ProjectError(
                "preview_validation_blocked",
                "請先處理剪輯版本的檢查項目，再產生初剪。",
                status_code=409,
            )
        with self._lock:
            existing = self.renders.by_request(project_id, request_id)
            if existing is not None:
                if existing["timeline_id"] != timeline_id:
                    raise ProjectError(
                        "request_conflict",
                        "這次初剪輸出與既有操作衝突。",
                        status_code=409,
                    )
                return self.status(project_id)
            current = self.renders.current(project_id)
            if current and current["state"] in {"queued", "running", "cancelling"}:
                raise ProjectError(
                    "preview_busy",
                    "初剪正在輸出，請等待完成或先取消。",
                    status_code=409,
                )
            render = self.renders.create(project_id, request_id, timeline_id)
            cancel = threading.Event()
            self._cancels[render["render_id"]] = cancel
            thread = threading.Thread(
                target=self._run,
                args=(project_id, render["render_id"], timeline.timeline, cancel),
                daemon=True,
                name=f"clipen-preview-{render['render_id'][-8:]}",
            )
            self._threads[render["render_id"]] = thread
            thread.start()
        return self.status(project_id)

    def close(self) -> None:
        with self._lock:
            active = tuple(
                (self._cancels[render_id], thread)
                for render_id, thread in self._threads.items()
            )
            for cancel, _ in active:
                cancel.set()
        for _, thread in active:
            thread.join(timeout=5)

    def cancel(self, project_id: str, render_id: str) -> dict[str, object]:
        render = self.renders.get(project_id, render_id)
        if render["state"] not in {"queued", "running", "cancelling"}:
            return self.status(project_id)
        self.renders.request_cancel(project_id, render_id)
        with self._lock:
            event = self._cancels.get(render_id)
        if event is not None:
            event.set()
        return self.status(project_id)

    def media_path(self, project_id: str, render_id: str):
        render = self.renders.get(project_id, render_id)
        if render["state"] != "succeeded":
            raise ProjectError("preview_not_ready", "初剪影片尚未完成。", status_code=409)
        path = self.renders.output_path(project_id, render_id, prepare=False)
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != render["size_bytes"]
        ):
            raise ProjectError(
                "preview_missing",
                "找不到已完成的初剪影片。",
                status_code=409,
            )
        return path

    def _run(
        self, project_id: str, render_id: str, timeline, cancel: threading.Event
    ) -> None:
        staging = self.renders.output_path(project_id, render_id, staging=True)
        final = self.renders.output_path(project_id, render_id)
        try:
            staging.unlink(missing_ok=True)
            running = self.renders.mark_running(project_id, render_id)
            if running["state"] != "running" or cancel.is_set():
                raise PreviewRenderError("cancelled")
            last_saved = 0.0
            last_saved_at = 0.0

            def save_progress(value: float) -> None:
                nonlocal last_saved, last_saved_at
                now = time.monotonic()
                normalized = max(0.0, min(1.0, value))
                if (
                    normalized < 1
                    and normalized - last_saved < 0.01
                    and now - last_saved_at < 1
                ):
                    return
                self.renders.update_progress(project_id, render_id, normalized)
                last_saved = normalized
                last_saved_at = now

            self.renderer.render(project_id, timeline, staging, cancel, save_progress)
            if cancel.is_set():
                raise PreviewRenderError("cancelled")
            os.replace(staging, final)
            restrict_permissions(final, 0o600)
            size = final.stat().st_size
            relative = f"previews/{final.name}"
            completed = self.renders.complete(project_id, render_id, relative, size)
            if completed["state"] != "succeeded":
                final.unlink(missing_ok=True)
                raise PreviewRenderError("cancelled")
        except PreviewRenderError as error:
            final.unlink(missing_ok=True)
            state = "cancelled" if error.code == "cancelled" else "failed"
            self.renders.finish_unsuccessful(
                project_id, render_id, state, error_code=error.code
            )
        except Exception:
            final.unlink(missing_ok=True)
            self.renders.finish_unsuccessful(
                project_id,
                render_id,
                "failed",
                error_code="preview_internal_error",
            )
        finally:
            staging.unlink(missing_ok=True)
            with self._lock:
                self._cancels.pop(render_id, None)
                self._threads.pop(render_id, None)

    def _can_preview(self, project_id: str) -> bool:
        if self.timeline_status is None:
            return True
        status = self.timeline_status.status(project_id)
        if status.get("state") == "ready":
            return True
        if status.get("state") != "blocked":
            return False
        validation = status.get("validation")
        report = validation.get("report") if isinstance(validation, dict) else None
        issues = report.get("issues") if isinstance(report, dict) else None
        preview_only_rules = {
            "timeline.capability.element_unverified",
            "timeline.content.audio_missing",
        }
        return bool(issues) and all(
            isinstance(issue, dict)
            and issue.get("rule_id") in preview_only_rules
            for issue in issues
        )
