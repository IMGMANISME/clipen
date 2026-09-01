from pathlib import Path
import tempfile
import threading
import time
import unittest

from clipen.media import (
    AssetKind,
    InventoryCoordinator,
    InventoryRepository,
    InventoryState,
    MediaProbeError,
    MediaProbeResult,
    MediaStream,
)
from clipen.projects import DirectoryReference, DirectoryRole, ProjectRepository


class FakeProbe:
    def __init__(self) -> None:
        self.calls = 0

    def version(self) -> str:
        return "ffprobe version fake"

    def probe(self, root, asset, cancel=None) -> MediaProbeResult:
        self.calls += 1
        return MediaProbeResult(
            asset.kind,
            "a" * 64,
            asset.size_bytes,
            asset.mtime_ns,
            {"duration_seconds": "1.0", "format_names": ["mov"]},
            (MediaStream(0, "video", "h264", {"width": 320, "height": 180}),),
        )


class BlockingProbe:
    def __init__(self) -> None:
        self.started = threading.Event()

    def version(self) -> str:
        return "ffprobe version fake"

    def probe(self, root, asset, cancel=None) -> MediaProbeResult:
        self.started.set()
        while cancel is None or not cancel.wait(0.01):
            pass
        raise MediaProbeError("cancelled")


class MediaInventoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects = ProjectRepository(self.root / "projects")
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "clip.mov").write_bytes(b"synthetic-media")
        (self.source / "notes.txt").write_text("not media", encoding="utf-8")
        created = self.projects.create_project("request-service-create", "素材流程")
        identity = self.source.stat()
        self.project = self.projects.set_directory(
            created.project_id,
            "request-service-source",
            "b" * 64,
            created.revision,
            DirectoryReference(
                DirectoryRole.SOURCE,
                str(self.source.resolve()),
                str(self.source),
                "source",
                identity.st_dev,
                identity.st_ino,
                "2026-09-01T00:00:00+00:00",
            ),
        )
        self.repository = InventoryRepository(self.projects)
        self.coordinators: list[InventoryCoordinator] = []

    def tearDown(self) -> None:
        for coordinator in self.coordinators:
            coordinator.close()
        self.temporary.cleanup()

    def coordinator(self, probe) -> InventoryCoordinator:
        coordinator = InventoryCoordinator(
            self.repository,
            probe_factory=lambda: probe,
        )
        self.coordinators.append(coordinator)
        return coordinator

    def wait_terminal(self, run_id: str, timeout: float = 3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.repository.get_run(self.project.project_id, run_id)
            if run.state in {
                InventoryState.SUCCEEDED,
                InventoryState.FAILED,
                InventoryState.CANCELLED,
                InventoryState.INTERRUPTED,
            }:
                return run
            time.sleep(0.01)
        self.fail("inventory did not reach a terminal state")

    def test_background_inventory_publishes_only_after_all_items_are_staged(self) -> None:
        probe = FakeProbe()
        coordinator = self.coordinator(probe)
        before = (self.source / "clip.mov").read_bytes()
        run = coordinator.start(
            self.project.project_id,
            "request-service-success",
            self.project.revision,
        )

        finished = self.wait_terminal(run.run_id)
        self.assertEqual(finished.state, InventoryState.SUCCEEDED)
        self.assertEqual(
            finished.counts.to_public(),
            {"discovered": 2, "probed": 1, "usable": 1, "issues": 0, "ignored": 1},
        )
        self.assertEqual(self.repository.current_run(self.project.project_id), finished)
        self.assertEqual((self.source / "clip.mov").read_bytes(), before)
        self.assertEqual(probe.calls, 1)

        replay = coordinator.start(
            self.project.project_id,
            "request-service-success",
            self.project.revision,
        )
        self.assertEqual(replay, finished)
        self.assertEqual(probe.calls, 1)

    def test_cancel_terminates_active_probe_without_publishing_partial_result(self) -> None:
        probe = BlockingProbe()
        coordinator = self.coordinator(probe)
        run = coordinator.start(
            self.project.project_id,
            "request-service-cancel",
            self.project.revision,
        )
        self.assertTrue(probe.started.wait(2))

        cancelling = coordinator.cancel(self.project.project_id, run.run_id)
        self.assertEqual(cancelling.state, InventoryState.CANCELLING)
        finished = self.wait_terminal(run.run_id)
        self.assertEqual(finished.state, InventoryState.CANCELLED)
        self.assertIsNone(self.repository.current_run(self.project.project_id))

    def test_probe_setup_failure_is_durable_and_does_not_publish(self) -> None:
        def unavailable():
            raise MediaProbeError("probe_unavailable")

        coordinator = InventoryCoordinator(self.repository, probe_factory=unavailable)
        self.coordinators.append(coordinator)
        run = coordinator.start(
            self.project.project_id,
            "request-service-unavailable",
            self.project.revision,
        )

        finished = self.wait_terminal(run.run_id)
        self.assertEqual(finished.state, InventoryState.FAILED)
        self.assertEqual(finished.error_code, "probe_unavailable")
        self.assertIsNone(self.repository.current_run(self.project.project_id))

    def test_close_interrupts_work_and_recovery_never_replays_it(self) -> None:
        probe = BlockingProbe()
        coordinator = self.coordinator(probe)
        run = coordinator.start(
            self.project.project_id,
            "request-service-close",
            self.project.revision,
        )
        self.assertTrue(probe.started.wait(2))

        coordinator.close()
        finished = self.wait_terminal(run.run_id)
        self.assertEqual(finished.state, InventoryState.INTERRUPTED)
        self.assertEqual(finished.error_code, "controller_stopped")
        restarted = InventoryCoordinator(self.repository, probe_factory=lambda: FakeProbe())
        self.coordinators.append(restarted)
        self.assertEqual(
            restarted.recover_incomplete([self.project.project_id]),
            0,
        )


if __name__ == "__main__":
    unittest.main()
