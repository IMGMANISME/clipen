"""Background orchestration for one-shot provider-driven editing sessions."""

from __future__ import annotations

from pathlib import Path
import queue
import threading

from clipen.projects import ProjectError
from clipen.providers.codex import ProviderCancelled

from .agent import AgentRun, EditAgentRepository
from .style_proof import is_style_proof, style_proof_stats


class EditAgentCoordinator:
    """Serialize provider jobs while the MCP server owns all timeline mutations."""

    def __init__(
        self,
        repository: EditAgentRepository,
        attempt_root: Path,
        *,
        adapter_factory=None,
        queue_capacity: int = 4,
    ) -> None:
        root = Path(attempt_root).resolve(strict=True)
        if not root.is_dir() or attempt_root.is_symlink():
            raise ValueError("editing attempt root must be a safe directory")
        if not 1 <= queue_capacity <= 16:
            raise ValueError("editing queue capacity is invalid")
        self.repository = repository
        self.attempt_root = root
        if adapter_factory is None:
            # Import lazily so the provider can import the agent repository
            # without cycling back through clipen.editing.__init__.
            from clipen.providers.codex_editing import CodexEditingAgent

            adapter_factory = CodexEditingAgent
        self.adapter_factory = adapter_factory
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue(queue_capacity)
        self._cancel: dict[tuple[str, str], threading.Event] = {}
        self._projects: set[str] = set()
        self._worker: threading.Thread | None = None
        self._closed = False
        self._lock = threading.Lock()

    def recover(self) -> int:
        recovered = 0
        for project in self.repository.projects.list_projects():
            recovered += self.repository.interrupt_incomplete(project.project_id)
            latest = self.repository.latest(project.project_id)
            if (
                latest is not None
                and latest.state == "succeeded"
                and latest.provider_attempt_id is None
            ):
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
        latest = self.repository.latest(project_id)
        published = self.repository.latest_successful(project_id)
        result = None
        if published is not None:
            revision = self.repository.final_revision(project_id, published.run_id)
            stats = style_proof_stats(revision.timeline)
            result = {
                **revision.to_public(include_timeline=True),
                "is_style_proof": is_style_proof(revision.timeline),
                "proof_stats": {
                    "duration_seconds": stats.duration_seconds,
                    "clip_count": stats.clip_count,
                    "longest_clip_seconds": stats.longest_clip_seconds,
                    "title_count": stats.title_count,
                    "effect_count": stats.effect_count,
                },
            }
        return {
            "latest_run": None if latest is None else latest.to_public(),
            "result": result,
        }

    def start(
        self,
        project_id: str,
        request_id: object,
        consent: object,
        *,
        max_tool_calls: int = 48,
        max_preview_renders: int = 4,
    ) -> AgentRun:
        if consent is not True:
            raise ProjectError(
                "edit_agent_consent_required",
                "請先同意使用 Codex 額度執行這次 AI 剪輯。",
                status_code=422,
            )
        with self._lock:
            if self._closed or self._queue.full():
                raise ProjectError(
                    "edit_agent_busy", "AI 剪輯佇列目前已滿。", status_code=409
                )
            run, created = self.repository.create_run(
                project_id,
                request_id,
                provider="codex",
                max_tool_calls=max_tool_calls,
                max_preview_renders=max_preview_renders,
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

    def cancel(self, project_id: str, run_id: str) -> AgentRun:
        with self._lock:
            run = self.repository.cancel(project_id, run_id)
            token = self._cancel.get((project_id, run_id))
            if token is not None:
                token.set()
            return run

    def close(self, timeout: float = 15) -> None:
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
                adapter = self.adapter_factory(
                    self.repository.projects.root,
                    self.attempt_root,
                    self.repository,
                )
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
                    with self._lock:
                        self._cancel.pop(key, None)
                self._queue.task_done()
