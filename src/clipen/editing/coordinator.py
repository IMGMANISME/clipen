"""Background orchestration for one-shot provider-driven editing sessions."""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

from clipen.projects import ProjectError
from clipen.providers.codex import ProviderCancelled

from .agent import AgentRun, EditAgentRepository
from .production_limits import timeline_stats


class EditAgentCoordinator:
    """Serialize provider jobs while the MCP server owns all timeline mutations."""

    def __init__(
        self,
        repository: EditAgentRepository,
        attempt_root: Path,
        *,
        adapter_factory=None,
        queue_capacity: int = 4,
        local_runtime: tuple[Path, Path] | None = None,
        provider_id: str = "codex",
    ) -> None:
        root = Path(attempt_root).resolve(strict=True)
        if not root.is_dir() or attempt_root.is_symlink():
            raise ValueError("editing attempt root must be a safe directory")
        if not 1 <= queue_capacity <= 16:
            raise ValueError("editing queue capacity is invalid")
        from clipen.provider_registry import get_provider

        self.provider = get_provider(provider_id)
        if not self.provider.implemented:
            raise ValueError(f"provider {provider_id} cannot edit yet")
        self.repository = repository
        self.local_runtime = local_runtime
        self.attempt_root = root
        if adapter_factory is None:
            # Import lazily so the provider can import the agent repository
            # without cycling back through clipen.editing.__init__.
            from clipen.provider_registry import editing_agent_factory

            adapter_factory = editing_agent_factory(provider_id)
        self.adapter_factory = adapter_factory
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue(queue_capacity)
        self._cancel: dict[tuple[str, str], threading.Event] = {}
        self._projects: set[str] = set()
        self._worker: threading.Thread | None = None
        self._closed = False
        self._lock = threading.RLock()
        self._user_previews = {}

    def recover(self) -> int:
        recovered = 0
        for project in self.repository.projects.list_projects():
            recovered += self.repository.interrupt_incomplete(project.project_id)
            with self.repository.projects.connect(project.project_id) as db:
                db.execute(
                    "UPDATE production_evidence_jobs SET state='interrupted',error_code='controller_stopped' WHERE state='running'"
                )
            latest = self.repository.latest(project.project_id)
            if latest is not None and latest.state == "succeeded" and latest.provider_attempt_id is None:
                # finish_edit is durable before the provider process reports its
                # usage. A Controller shutdown in that narrow window must not
                # leave the UI waiting forever for a process that no longer
                # exists. Preserve the successful revision and mark the usage
                # handoff as unavailable.
                self.repository.record_provider_outcome(
                    project.project_id,
                    latest.run_id,
                    "recovered-" + latest.run_id.removeprefix("editrun_"),
                    usage=None,
                )
                recovered += 1
        return recovered

    def status(self, project_id: str) -> dict[str, object]:
        from .production_requirements import production_contents

        latest = self.repository.latest(project_id)
        published = self.repository.latest_successful(project_id)
        result = None
        if published is not None:
            revision = self.repository.final_revision(project_id, published.run_id)
            stats = timeline_stats(revision.timeline)
            with self.repository.projects.connect(project_id) as db:
                rows = db.execute(
                    "SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='editorial_review' ORDER BY sequence DESC",
                    (published.run_id,),
                ).fetchall()
                completed_preview = db.execute(
                    "SELECT render_id FROM preview_renders WHERE timeline_id=? AND state='succeeded' ORDER BY created_at DESC LIMIT 1",
                    (revision.revision_id,),
                ).fetchone()
            assessment = next(
                (v for v in (json.loads(r["payload_json"]) for r in rows) if v["revision_id"] == revision.revision_id),
                None,
            )
            from .production_requirements import production_contents
            from .program_story import read_episode
            from .program_subtitles import subtitle_status

            result = {
                **revision.to_public(include_timeline=True),
                "run_id": published.run_id,
                "editorial_review": assessment,
                "creative_direction": self.repository.creative_direction(project_id, published.run_id),
                "episode": read_episode(self.repository.projects, project_id, published.run_id, revision.timeline),
                "subtitle_review": subtitle_status(self.repository.projects, project_id, revision.timeline),
                "production_contents": production_contents(self.repository.projects, project_id, revision.timeline),
                "export_url": self._export_url(project_id, published.run_id, revision.revision_id),
                "preview_url": f"/api/projects/{project_id}/preview/{completed_preview['render_id']}/media"
                if completed_preview
                else None,
                "proof_stats": {
                    "duration_seconds": stats.duration_seconds,
                    "clip_count": stats.clip_count,
                    "longest_clip_seconds": stats.longest_clip_seconds,
                    "title_count": stats.title_count,
                    "effect_count": stats.effect_count,
                },
            }
        production = None
        if latest is not None:
            session = self.repository.production_session(project_id, latest.run_id)
            if session is not None:
                draft = self.repository.current_revision(project_id, latest.run_id)
                from clipen.rendering import PreviewRenderRepository

                with self.repository.projects.connect(project_id) as db:
                    previews = db.execute(
                        "SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='production_preview' ORDER BY sequence DESC LIMIT 1",
                        (latest.run_id,),
                    ).fetchone()
                    successful_preview = db.execute(
                        "SELECT e.payload_json FROM edit_agent_events e JOIN preview_renders p ON p.render_id=json_extract(e.payload_json,'$.render_id') AND p.project_id=? WHERE e.run_id=? AND e.event_type='production_preview' AND p.state='succeeded' ORDER BY e.sequence DESC LIMIT 1",
                        (project_id, latest.run_id),
                    ).fetchone()
                    checks = db.execute(
                        "SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='sequence_validation' ORDER BY sequence DESC LIMIT 30",
                        (latest.run_id,),
                    ).fetchall()
                    jobs = db.execute(
                        "SELECT job_id,kind,state,error_code,result_json FROM production_evidence_jobs WHERE run_id=? ORDER BY rowid DESC LIMIT 10",
                        (latest.run_id,),
                    ).fetchall()
                    from .production_progress import production_activity

                    progress = production_activity(db, latest.run_id)
                validation = next(
                    (
                        value
                        for row in checks
                        if (value := json.loads(row["payload_json"]))["revision_id"] == draft.revision_id
                    ),
                    None,
                )
                from .versions import versions

                with self.repository.projects.connect(project_id) as db:
                    film_versions = versions(db, latest.run_id, latest.provider)
                    succeeded = db.execute(
                        "SELECT e.payload_json FROM edit_agent_events e JOIN preview_renders p ON p.render_id=json_extract(e.payload_json,'$.render_id') AND p.project_id=? WHERE e.run_id=? AND e.event_type='production_preview' AND p.state='succeeded' ORDER BY e.sequence DESC LIMIT 200",
                        (project_id, latest.run_id),
                    ).fetchall()
                # A finished film is a preview of a version people chose to keep (an AI request that
                # finished, a manual edit or a restore); a newer preview of anything else is a draft.
                complete = {v["revision_id"]: v for v in film_versions if v["kind"] != "ai_partial"}
                finished_row = next(
                    (row for row in succeeded if json.loads(row["payload_json"])["revision_id"] in complete), None
                )
                draft_row = succeeded[0] if succeeded and succeeded[0] is not finished_row else None
                local_jobs = []
                for row in jobs:
                    value = json.loads(row["result_json"]) if row["result_json"] else {}
                    local_jobs.append(
                        {k: row[k] for k in ("job_id", "kind", "state", "error_code")}
                        | {k: value[k] for k in ("phase", "completed_bytes", "total_bytes") if k in value}
                    )
                from .production_preview import render_recipe_id

                recipe_id = render_recipe_id()

                def preview_value(row):
                    if row is None:
                        return None
                    entry = json.loads(row["payload_json"])
                    return {
                        **PreviewRenderRepository(self.repository.projects).get(project_id, entry["render_id"]),
                        "revision_id": entry["revision_id"],
                        "stale": entry["revision_id"] != draft.revision_id or entry.get("recipe_id") != recipe_id,
                        "media_url": f"/api/projects/{project_id}/preview/{entry['render_id']}/media",
                    }

                preview = preview_value(previews)
                last_successful_preview = preview_value(successful_preview)
                finished_preview = preview_value(finished_row)
                draft_preview = preview_value(draft_row)
                by_revision = {v["revision_id"]: v for v in reversed(film_versions)}
                if finished_preview:
                    finished_preview["version"] = by_revision.get(finished_preview["revision_id"])
                if draft_preview:
                    draft_preview["version"] = by_revision.get(draft_preview["revision_id"])
                production = {
                    "intent": session["intent"],
                    "skill": {
                        k: session["skill"][k]
                        for k in (
                            "skill_id",
                            "name",
                            "version",
                            "content_hash",
                            "limitations",
                            "selected_visual_style_id",
                            "selected_font_style_id",
                        )
                        if k in session["skill"]
                    },
                    "draft": draft.to_public(include_timeline=True),
                    "preview": preview,
                    "last_successful_preview": last_successful_preview,
                    "finished_preview": finished_preview,
                    "draft_preview": draft_preview,
                    "validation": validation,
                    "local_jobs": local_jobs,
                    "production_contents": production_contents(self.repository.projects, project_id, draft.timeline),
                    "export_url": self._export_url(project_id, latest.run_id, draft.revision_id),
                    "progress": progress,
                }
        return {
            "latest_run": None if latest is None else latest.to_public(),
            "result": result,
            "production": production,
        }

    def _export_url(self, project_id, run_id, revision_id):
        try:
            self.export_path(project_id, run_id, revision_id)
        except ProjectError:
            return None
        return f"/api/projects/{project_id}/edit-agent/{run_id}/exports/{revision_id}"

    def export_path(self, project_id, run_id, revision_id):
        from .agent_models import REVISION_ID_PATTERN

        self.repository.get(project_id, run_id)
        if not isinstance(revision_id, str) or REVISION_ID_PATTERN.fullmatch(revision_id) is None:
            raise ProjectError("agent_export_not_found", "找不到匯出的剪輯版本。", status_code=404)
        with self.repository.projects.connect(project_id) as db:
            if (
                db.execute(
                    "SELECT 1 FROM edit_agent_revisions WHERE revision_id=? AND run_id=?", (revision_id, run_id)
                ).fetchone()
                is None
            ):
                raise ProjectError("agent_export_not_found", "匯出不屬於這次剪輯。", status_code=404)
        root = self.repository.projects.root / project_id / "agent-production"
        path = root / run_id / "exports" / (revision_id + ".json")
        if (
            any(p.is_symlink() for p in (root, root / run_id, root / run_id / "exports", path))
            or not path.is_file()
            or path.stat().st_size > 8 * 1024 * 1024
        ):
            raise ProjectError("agent_export_not_found", "匯出檔案遺失或不安全。", status_code=404)
        return path

    def start(
        self,
        project_id: str,
        request_id: object,
        consent: object,
        *,
        max_tool_calls: int = 96,
        max_preview_renders: int = 4,
        production: dict,
    ) -> AgentRun:
        if consent is not True:
            raise ProjectError(
                "edit_agent_consent_required",
                f"請先同意使用 {self.provider.display_name} 額度執行這次 AI 剪輯。",
                status_code=422,
            )
        with self._lock:
            if self._closed or self._queue.full():
                raise ProjectError("edit_agent_busy", "AI 剪輯佇列目前已滿。", status_code=409)
            run, created = self.repository.create_run(
                project_id,
                request_id,
                provider=self.provider.provider_id,
                max_tool_calls=max_tool_calls,
                max_preview_renders=max_preview_renders,
                production=production,
            )
            if not created:
                return run
            key = (project_id, run.run_id)
            self._projects.add(project_id)
            self._cancel[key] = threading.Event()
            self._queue.put_nowait(key)
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._loop,
                    name="clipen-edit-agent",
                    daemon=True,
                )
                self._worker.start()
        return run

    def refine(
        self,
        project_id,
        run_id,
        request_id,
        expected_revision_id,
        instruction,
        consent,
        music_asset_id=None,
        expected_references_version=None,
    ):
        if consent is not True:
            raise ProjectError(
                "edit_agent_consent_required",
                f"請同意使用 {self.provider.display_name} 額度修改影片。",
                status_code=422,
            )
        with self._lock:
            key = (project_id, run_id)
            if self._closed or self._queue.full() or key in self._cancel:
                raise ProjectError("edit_agent_busy", "AI 製作仍在執行，請稍後再試。", status_code=409)
            run, created = self.repository.request_refinement(
                project_id,
                run_id,
                request_id,
                expected_revision_id,
                instruction,
                music_asset_id,
                expected_references_version,
                provider=self.provider.provider_id,
            )
            if not created:
                return run
            self._projects.add(project_id)
            self._cancel[key] = threading.Event()
            self._queue.put_nowait(key)
            if self._worker is None:
                self._worker = threading.Thread(target=self._loop, name="clipen-edit-agent", daemon=True)
                self._worker.start()
            return run

    def workbench_edit(self, project_id, run_id, tool, arguments, expected_revision_id, mutation_id):
        from .workbench import edit

        with self._lock:
            return edit(self.repository, project_id, run_id, tool, arguments, expected_revision_id, mutation_id)

    def workbench_render(self, project_id, run_id, expected_revision_id):
        from .workbench import idle, tools_for

        with self._lock:
            run = idle(self.repository, project_id, run_id)
            if run.current_revision_id != expected_revision_id:
                raise ProjectError("revision_conflict", "影片已更新，請重新讀取。", status_code=409)
            key = (project_id, run_id)
            tools = self._user_previews.get(key)
            if tools is None:
                tools = tools_for(self.repository, project_id, run_id)
                self._user_previews[key] = tools
            if hasattr(tools, "_session_cache"):
                del tools._session_cache
            return tools.production_call("render_preview", {}).payload

    def resume(self, project_id: str, run_id: str, consent: object) -> AgentRun:
        if consent is not True:
            raise ProjectError(
                "edit_agent_consent_required", f"請同意接續使用 {self.provider.display_name} 額度。", status_code=422
            )
        with self._lock:
            key = (project_id, run_id)
            if self._closed or self._queue.full() or key in self._cancel:
                raise ProjectError("edit_agent_busy", "製作工作仍在結束或佇列已滿，請稍後再試。", status_code=409)
            run = self.repository.resume_stopped(project_id, run_id)
            self._projects.add(project_id)
            self._cancel[key] = threading.Event()
            self._queue.put_nowait(key)
            if self._worker is None:
                self._worker = threading.Thread(target=self._loop, name="clipen-edit-agent", daemon=True)
                self._worker.start()
            return run

    def cancel(self, project_id: str, run_id: str) -> AgentRun:
        with self._lock:
            run = self.repository.cancel(project_id, run_id)
            token = self._cancel.get((project_id, run_id))
            if token is not None:
                token.set()
            return run

    def close(self, timeout: float = 15) -> None:
        for tools in self._user_previews.values():
            tools.close_production()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = tuple(self._cancel)
            for token in self._cancel.values():
                token.set()
            while True:
                try:
                    queued = self._queue.get_nowait()
                except queue.Empty:
                    break
                if queued is not None:
                    self._queue.task_done()
            worker = self._worker
            if worker is not None:
                self._queue.put_nowait(None)
        if worker is not None:
            worker.join(max(0, timeout))
        for project_id, run_id in active:
            try:
                self.repository.interrupt_for_shutdown(project_id, run_id)
            except ProjectError:
                pass
        for project_id in self._projects:
            self.repository.interrupt_incomplete(project_id)

    def _end_attempt_renders(self, key) -> None:
        from clipen.rendering import PreviewRenderRepository

        try:
            PreviewRenderRepository(self.repository.projects).interrupt_attempt_renders(*key)
        except ProjectError:
            pass

    def _loop(self) -> None:
        while True:
            key = self._queue.get()
            try:
                if key is None:
                    return
                with self._lock:
                    cancel = self._cancel[key]
                if cancel.is_set():
                    continue
                if self.repository.get(*key).provider != self.provider.provider_id:
                    # Never spend another provider's quota on a run the user
                    # authorised for the provider it was created with.
                    self.repository.record_provider_outcome(
                        *key,
                        "agent-mismatch-" + key[1].removeprefix("editrun_"),
                        usage=None,
                        error_code="provider_mismatch",
                    )
                    continue
                adapter = self.adapter_factory(
                    self.repository.projects.root,
                    self.attempt_root,
                    self.repository,
                    **({"local_runtime": self.local_runtime} if self.local_runtime else {}),
                )
                self._end_attempt_renders(key)
                adapter.execute(*key, cancel=cancel)
            except (ProjectError, ProviderCancelled):
                pass
            except Exception:
                if key is not None:
                    try:
                        current = self.repository.get(*key)
                        if current.state in {"queued", "running"}:
                            self.repository.record_provider_outcome(
                                *key,
                                "agent-launch-" + key[1].removeprefix("editrun_"),
                                usage=None,
                                error_code="provider_launch_failed",
                            )
                    except ProjectError:
                        pass
            finally:
                if key is not None:
                    try:
                        with self.repository.projects.connect(key[0]) as db:
                            db.execute(
                                "UPDATE production_evidence_jobs SET state='interrupted',error_code='provider_ended' WHERE run_id=? AND state='running'",
                                (key[1],),
                            )
                    except ProjectError:
                        pass
                    self._end_attempt_renders(key)
                    with self._lock:
                        self._cancel.pop(key, None)
                self._queue.task_done()
