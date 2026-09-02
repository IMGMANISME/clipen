from pathlib import Path
import tempfile
import unittest
from unittest import mock

from clipen.analysis import ArtifactStore, ManagedAnalysisRuntime, PreparationRepository
from clipen.projects import ProjectRepository


class FakeSetup:
    def __init__(self) -> None:
        self.binding = None
        self.removed = False
        self.closed = False

    def public_status(self):
        return {"state": "ready" if self.binding else "setup_required"}

    def runtime_binding(self):
        return self.binding

    def start_install(self, accepted_notice):
        return accepted_notice

    def cancel_install(self):
        return None

    def remove(self, before_remove=None):
        if before_remove is not None:
            before_remove()
        self.removed = True
        self.binding = None
        return "removed"

    def close(self):
        self.closed = True


class ManagedAnalysisRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        projects = ProjectRepository(Path(self.temporary.name) / "projects")
        self.repository = PreparationRepository(projects)
        self.artifacts = ArtifactStore(projects)
        self.setup = FakeSetup()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_coordinator_is_created_lazily_and_reused(self) -> None:
        runtime = ManagedAnalysisRuntime(self.repository, self.artifacts, self.setup)
        self.assertIsNone(runtime.coordinator())
        self.setup.binding = (lambda: object(), {"id": "test"})
        with mock.patch("clipen.analysis.runtime.PreparationCoordinator") as coordinator:
            first = runtime.coordinator()
            second = runtime.coordinator()
        self.assertIs(first, second)
        coordinator.assert_called_once_with(
            self.repository,
            self.artifacts,
            transcriber_factory=self.setup.binding[0],
            model={"id": "test"},
        )

    def test_remove_stops_coordinator_before_deleting_runtime(self) -> None:
        self.setup.binding = (lambda: object(), {"id": "test"})
        runtime = ManagedAnalysisRuntime(self.repository, self.artifacts, self.setup)
        coordinator = mock.Mock()
        with mock.patch(
            "clipen.analysis.runtime.PreparationCoordinator", return_value=coordinator
        ):
            runtime.coordinator()
        self.assertEqual(runtime.remove(), "removed")
        coordinator.close.assert_called_once_with(timeout=15)
        self.assertTrue(self.setup.removed)

    def test_close_cancels_setup_and_coordinator(self) -> None:
        self.setup.binding = (lambda: object(), {"id": "test"})
        runtime = ManagedAnalysisRuntime(self.repository, self.artifacts, self.setup)
        coordinator = mock.Mock()
        with mock.patch(
            "clipen.analysis.runtime.PreparationCoordinator", return_value=coordinator
        ):
            runtime.coordinator()
        runtime.close()
        self.assertTrue(self.setup.closed)
        coordinator.close.assert_called_once_with(timeout=15)


if __name__ == "__main__":
    unittest.main()
