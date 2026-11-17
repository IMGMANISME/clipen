"""Lazy lifecycle binding between managed ASR setup and media preparation."""

from __future__ import annotations

import threading

from .artifacts import ArtifactStore
from .service import PreparationCoordinator
from .setup import ASRSetupManager, ASRSetupStatus
from .storage import PreparationRepository


class ManagedAnalysisRuntime:
    """Creates the heavy coordinator only after the verified runtime is ready."""

    def __init__(
        self,
        repository: PreparationRepository,
        artifacts: ArtifactStore,
        setup: ASRSetupManager,
    ) -> None:
        self.repository = repository
        self._artifacts = artifacts
        self._setup = setup
        self._lock = threading.Lock()
        self._coordinator: PreparationCoordinator | None = None

    def public_status(self) -> dict[str, object]:
        return self._setup.public_status()

    def coordinator(self) -> PreparationCoordinator | None:
        with self._lock:
            if self._coordinator is not None:
                return self._coordinator
            binding = self._setup.runtime_binding()
            if binding is None:
                return None
            factory, model = binding
            self._coordinator = PreparationCoordinator(
                self.repository,
                self._artifacts,
                transcriber_factory=factory,
                model=model,
            )
            return self._coordinator

    def remove(self) -> ASRSetupStatus:
        with self._lock:
            coordinator = self._coordinator
            self._coordinator = None
        return self._setup.remove(None if coordinator is None else lambda: coordinator.close(timeout=15))

    def close(self) -> None:
        self._setup.close()
        with self._lock:
            coordinator = self._coordinator
            self._coordinator = None
        if coordinator is not None:
            coordinator.close(timeout=15)


__all__ = ["ManagedAnalysisRuntime"]
