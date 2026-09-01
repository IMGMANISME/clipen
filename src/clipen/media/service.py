"""Single-worker orchestration for recoverable, source-read-only inventories."""

from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
from typing import Callable, Iterable, Protocol

from clipen.projects.models import ProjectError

from .discovery import DiscoveredAsset, DiscoveryError, MediaDiscovery
from .models import (
    AssetCandidate,
    InventoryRun,
    InventoryState,
    ItemStatus,
    ProbeStatus,
)
from .probe import FFprobeAdapter, MediaProbeError, MediaProbeResult
from .storage import InventoryRepository


_ITEM_PROBE_ERRORS = {
    "unsupported": ProbeStatus.UNSUPPORTED,
    "unreadable": ProbeStatus.UNREADABLE,
    "entry_unreadable": ProbeStatus.UNREADABLE,
    "entry_not_regular": ProbeStatus.CHANGED,
    "timeout": ProbeStatus.TIMEOUT,
    "changed": ProbeStatus.CHANGED,
}


class _Discovery(Protocol):
    def scan(
        self, root: object, cancel: threading.Event | None = None
    ) -> Iterable[DiscoveredAsset]: ...


class _Probe(Protocol):
    def probe(
        self,
        root: object,
        asset: DiscoveredAsset,
        cancel: threading.Event | None = None,
    ) -> MediaProbeResult: ...


@dataclass(frozen=True)
class _Job:
    project_id: str
    run_id: str


class InventoryCoordinator:
    """Runs one bounded inventory at a time without blocking HTTP requests."""

    def __init__(
        self,
        repository: InventoryRepository,
        *,
        discovery_factory: Callable[[], _Discovery] = MediaDiscovery,
        probe_factory: Callable[[], _Probe] = FFprobeAdapter,
    ) -> None:
        self.repository = repository
        self._discovery_factory = discovery_factory
        self._probe_factory = probe_factory
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._lock = threading.Lock()
        self._cancellations: dict[str, threading.Event] = {}
        self._projects: set[str] = set()
        self._worker: threading.Thread | None = None
        self._shutdown = threading.Event()

    def recover_incomplete(self, project_ids: Iterable[str]) -> int:
        """Mark work left by a dead controller as interrupted."""
        recovered = 0
        for project_id in project_ids:
            recovered += self.repository.interrupt_incomplete(project_id)
        return recovered

    def start(
        self, project_id: str, request_id: object, expected_revision: object
    ) -> InventoryRun:
        run, created = self.repository.create_run_with_status(
            project_id, request_id, expected_revision
        )
        if not created:
            return run
        with self._lock:
            if self._shutdown.is_set():
                self.repository.transition(
                    project_id,
                    run.run_id,
                    InventoryState.INTERRUPTED,
                    phase="interrupted",
                    error_code="controller_stopped",
                )
                return self.repository.get_run(project_id, run.run_id)
            self._projects.add(project_id)
            self._cancellations[run.run_id] = threading.Event()
            self._ensure_worker_locked()
            self._queue.put(_Job(project_id, run.run_id))
        return run

    def cancel(self, project_id: str, run_id: object) -> InventoryRun:
        run = self.repository.request_cancel(project_id, run_id)
        if run.state in {InventoryState.CANCELLING, InventoryState.CANCELLED}:
            with self._lock:
                event = self._cancellations.get(run.run_id)
                if event is not None:
                    event.set()
        return run

    def close(self, timeout: float = 3) -> None:
        with self._lock:
            if self._shutdown.is_set():
                return
            self._shutdown.set()
            for event in self._cancellations.values():
                event.set()
            worker = self._worker
            self._queue.put(None)
        if worker is not None:
            worker.join(max(0, timeout))
        for project_id in tuple(self._projects):
            self.repository.interrupt_incomplete(project_id)

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._work_loop,
            name="clipen-media-inventory",
            daemon=True,
        )
        self._worker.start()

    def _work_loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                self._execute(job)
            finally:
                self._queue.task_done()

    def _execute(self, job: _Job) -> None:
        with self._lock:
            cancel = self._cancellations.get(job.run_id)
        if cancel is None:
            return
        try:
            queued = self.repository.get_run(job.project_id, job.run_id)
            if queued.state is not InventoryState.QUEUED:
                return
            if self._shutdown.is_set():
                self._finish_stopped(job)
                return
            running = self.repository.transition(
                job.project_id,
                job.run_id,
                InventoryState.RUNNING,
                phase="inspecting",
            )
            discovery = self._discovery_factory()
            prober = self._probe_factory()
            for root in running.roots:
                for discovered in discovery.scan(root, cancel):
                    if cancel.is_set():
                        self._finish_cancel(job)
                        return
                    candidate = self._inspect(prober, root, discovered, cancel)
                    self.repository.stage_item(job.project_id, job.run_id, candidate)
            if cancel.is_set():
                self._finish_cancel(job)
                return
            self.repository.publish(job.project_id, job.run_id)
        except DiscoveryError as error:
            if error.code == "cancelled":
                self._finish_cancel(job)
            else:
                self._fail(job, error.code)
        except MediaProbeError as error:
            if error.code == "cancelled":
                self._finish_cancel(job)
            else:
                self._fail(job, error.code)
        except ProjectError:
            self._settle_conflict(job)
        except Exception:
            self._fail(job, "inventory_internal_error")
        finally:
            with self._lock:
                self._cancellations.pop(job.run_id, None)

    @staticmethod
    def _inspect(
        prober: _Probe,
        root: object,
        discovered: DiscoveredAsset,
        cancel: threading.Event,
    ) -> AssetCandidate:
        if not discovered.should_probe:
            return AssetCandidate(
                discovered.role,
                discovered.relative_path,
                discovered.kind,
                ItemStatus.IGNORED,
                discovered.issue_code or "non_media",
                None,
                None,
                None,
                None,
            )
        try:
            result = prober.probe(root, discovered, cancel)
        except MediaProbeError as error:
            if error.code == "cancelled" or error.code not in _ITEM_PROBE_ERRORS:
                raise
            has_version = error.has_version
            return AssetCandidate(
                discovered.role,
                discovered.relative_path,
                discovered.kind,
                ItemStatus.NEEDS_ATTENTION,
                f"probe_{error.code}",
                error.size_bytes if has_version else None,
                error.mtime_ns if has_version else None,
                error.fingerprint if has_version else None,
                _ITEM_PROBE_ERRORS[error.code] if has_version else None,
            )
        return AssetCandidate(
            discovered.role,
            discovered.relative_path,
            result.kind,
            ItemStatus.USABLE,
            None,
            result.size_bytes,
            result.mtime_ns,
            result.fingerprint,
            ProbeStatus.READY,
            result.summary,
            result.streams,
        )

    def _finish_cancel(self, job: _Job) -> None:
        if self._shutdown.is_set():
            self._finish_stopped(job)
            return
        current = self.repository.get_run(job.project_id, job.run_id)
        if current.state in {InventoryState.RUNNING, InventoryState.CANCELLING}:
            self.repository.transition(
                job.project_id,
                job.run_id,
                InventoryState.CANCELLED,
                phase="cancelled",
            )

    def _finish_stopped(self, job: _Job) -> None:
        current = self.repository.get_run(job.project_id, job.run_id)
        if current.state in {
            InventoryState.QUEUED,
            InventoryState.RUNNING,
            InventoryState.CANCELLING,
        }:
            self.repository.transition(
                job.project_id,
                job.run_id,
                InventoryState.INTERRUPTED,
                phase="interrupted",
                error_code="controller_stopped",
            )

    def _fail(self, job: _Job, code: str) -> None:
        try:
            current = self.repository.get_run(job.project_id, job.run_id)
            if current.state in {InventoryState.RUNNING, InventoryState.CANCELLING}:
                self.repository.transition(
                    job.project_id,
                    job.run_id,
                    InventoryState.FAILED,
                    phase="failed",
                    error_code=code,
                )
        except ProjectError:
            return

    def _settle_conflict(self, job: _Job) -> None:
        try:
            current = self.repository.get_run(job.project_id, job.run_id)
            if current.state is InventoryState.CANCELLING:
                self._finish_cancel(job)
        except ProjectError:
            return
