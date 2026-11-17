import hashlib
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from clipen.projects import (
    DirectoryAvailability,
    DirectoryRole,
    NativeDirectoryPicker,
    ProjectError,
    ProjectWorkspace,
    SelectionAuthority,
)
from clipen.projects.picker import MacOSDirectoryPicker, refresh_availability


class FakePicker(NativeDirectoryPicker):
    def __init__(self, *results: Path | None) -> None:
        self.results = list(results)
        self.calls: list[DirectoryRole] = []

    def choose(self, role: DirectoryRole) -> Path | None:
        self.calls.append(role)
        return self.results.pop(0)


def tree_fingerprint(root: Path) -> tuple[tuple[str, int, str], ...]:
    entries = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_file():
            entries.append((relative, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            entries.append((relative + "/", path.stat().st_mtime_ns, "directory"))
    return tuple(entries)


class ProjectPickerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects = self.root / "managed" / "projects"
        self.source = self.root / "external-volume" / "raw"
        self.reference = self.root / "reference"
        self.source.mkdir(parents=True)
        self.reference.mkdir()
        (self.source / "camera-a.mov").write_bytes(b"source-sentinel")
        (self.reference / "example.mov").write_bytes(b"reference-sentinel")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_workspace(self, picker: NativeDirectoryPicker) -> tuple[ProjectWorkspace, object]:
        workspace = ProjectWorkspace(self.projects, picker=picker)
        project = workspace.create_project("request-create-picker", "目錄測試")
        return workspace, project

    def test_selection_and_save_never_modify_source_or_reference(self) -> None:
        before_source = tree_fingerprint(self.source)
        before_reference = tree_fingerprint(self.reference)
        workspace, project = self.create_workspace(FakePicker(self.source, self.reference))

        source_selection = workspace.select_directory("request-pick-source", DirectoryRole.SOURCE)
        assert source_selection is not None
        project = workspace.set_directory(
            project.project_id,
            "request-save-source",
            project.revision,
            DirectoryRole.SOURCE,
            source_selection["selection_token"],
        )
        reference_selection = workspace.select_directory("request-pick-reference", DirectoryRole.REFERENCE)
        assert reference_selection is not None
        project = workspace.set_directory(
            project.project_id,
            "request-save-reference",
            project.revision,
            DirectoryRole.REFERENCE,
            reference_selection["selection_token"],
        )

        self.assertEqual(len(project.directories), 2)
        self.assertEqual(tree_fingerprint(self.source), before_source)
        self.assertEqual(tree_fingerprint(self.reference), before_reference)

    def test_cancel_is_not_an_error_and_request_can_be_retried(self) -> None:
        picker = FakePicker(None, self.source)
        workspace, _ = self.create_workspace(picker)
        self.assertIsNone(workspace.select_directory("request-pick-cancel", DirectoryRole.SOURCE))
        selected = workspace.select_directory("request-pick-cancel", DirectoryRole.SOURCE)
        self.assertIsNotNone(selected)
        self.assertEqual(picker.calls, [DirectoryRole.SOURCE, DirectoryRole.SOURCE])

    def test_picker_request_is_idempotent_and_role_scoped(self) -> None:
        picker = FakePicker(self.source)
        workspace, _ = self.create_workspace(picker)
        first = workspace.select_directory("request-pick-repeat", DirectoryRole.SOURCE)
        second = workspace.select_directory("request-pick-repeat", DirectoryRole.SOURCE)
        self.assertEqual(first, second)
        self.assertEqual(len(picker.calls), 1)
        with self.assertRaises(ProjectError) as context:
            workspace.select_directory("request-pick-repeat", DirectoryRole.REFERENCE)
        self.assertEqual(context.exception.code, "request_reused")

    def test_selection_expires_and_wrong_role_is_rejected(self) -> None:
        clock = [10.0]
        authority = SelectionAuthority(self.projects, now=lambda: clock[0])
        self.projects.mkdir(parents=True)
        selection = authority.register(self.source, DirectoryRole.SOURCE)
        with self.assertRaises(ProjectError):
            authority.resolve(selection.token, DirectoryRole.REFERENCE)
        clock[0] += 601
        with self.assertRaises(ProjectError) as context:
            authority.resolve(selection.token, DirectoryRole.SOURCE)
        self.assertEqual(context.exception.code, "selection_invalid")

    def test_rejects_managed_root_symlink_and_overlapping_roles(self) -> None:
        picker = FakePicker(self.projects, self.source, self.source / "nested")
        (self.source / "nested").mkdir()
        workspace, project = self.create_workspace(picker)
        with self.assertRaises(ProjectError) as context:
            workspace.select_directory("request-pick-managed", DirectoryRole.SOURCE)
        self.assertEqual(context.exception.code, "directory_managed_overlap")

        source_selection = workspace.select_directory("request-pick-overlap-source", DirectoryRole.SOURCE)
        assert source_selection is not None
        project = workspace.set_directory(
            project.project_id,
            "request-save-overlap-source",
            project.revision,
            DirectoryRole.SOURCE,
            source_selection["selection_token"],
        )
        nested_selection = workspace.select_directory("request-pick-overlap-reference", DirectoryRole.REFERENCE)
        assert nested_selection is not None
        with self.assertRaises(ProjectError) as context:
            workspace.set_directory(
                project.project_id,
                "request-save-overlap-reference",
                project.revision,
                DirectoryRole.REFERENCE,
                nested_selection["selection_token"],
            )
        self.assertEqual(context.exception.code, "directory_overlap")

    def test_rejects_selected_symbolic_link(self) -> None:
        link = self.root / "linked-source"
        link.symlink_to(self.source, target_is_directory=True)
        workspace, _ = self.create_workspace(FakePicker(link))
        with self.assertRaises(ProjectError) as context:
            workspace.select_directory("request-pick-symlink", DirectoryRole.SOURCE)
        self.assertEqual(context.exception.code, "directory_symlink")

    def test_offline_and_changed_identity_are_reported_without_forgetting_path(self) -> None:
        workspace, project = self.create_workspace(FakePicker(self.source))
        selection = workspace.select_directory("request-pick-offline", DirectoryRole.SOURCE)
        assert selection is not None
        project = workspace.set_directory(
            project.project_id,
            "request-save-offline",
            project.revision,
            DirectoryRole.SOURCE,
            selection["selection_token"],
        )
        moved = self.root / "source-unplugged"
        self.source.rename(moved)
        offline = workspace.get_project(project.project_id)
        self.assertEqual(offline.directories[0].availability, DirectoryAvailability.OFFLINE)
        self.source.mkdir(parents=True)
        changed = workspace.get_project(project.project_id)
        self.assertEqual(changed.directories[0].availability, DirectoryAvailability.CHANGED)

    def test_reconnecting_the_same_directory_restores_availability(self) -> None:
        workspace, project = self.create_workspace(FakePicker(self.source))
        selection = workspace.select_directory("request-pick-reconnect", DirectoryRole.SOURCE)
        assert selection is not None
        project = workspace.set_directory(
            project.project_id,
            "request-save-reconnect",
            project.revision,
            DirectoryRole.SOURCE,
            selection["selection_token"],
        )

        detached = self.root / "detached-source"
        self.source.rename(detached)
        self.assertEqual(
            workspace.get_project(project.project_id).directories[0].availability,
            DirectoryAvailability.OFFLINE,
        )
        detached.rename(self.source)
        self.assertEqual(
            workspace.get_project(project.project_id).directories[0].availability,
            DirectoryAvailability.AVAILABLE,
        )

    def test_external_volume_device_number_may_change_after_remount(self) -> None:
        workspace, project = self.create_workspace(FakePicker(self.source))
        selection = workspace.select_directory("request-pick-device-drift", DirectoryRole.SOURCE)
        assert selection is not None
        project = workspace.set_directory(
            project.project_id,
            "request-save-device-drift",
            project.revision,
            DirectoryRole.SOURCE,
            selection["selection_token"],
        )
        reference = project.directories[0]

        self.assertEqual(
            refresh_availability(replace(reference, device=reference.device + 1)).availability,
            DirectoryAvailability.AVAILABLE,
        )

    def test_set_directory_replays_after_selection_authority_is_gone(self) -> None:
        workspace, project = self.create_workspace(FakePicker(self.source))
        selection = workspace.select_directory("request-pick-replay", DirectoryRole.SOURCE)
        assert selection is not None
        saved = workspace.set_directory(
            project.project_id,
            "request-save-replay",
            project.revision,
            DirectoryRole.SOURCE,
            selection["selection_token"],
        )
        restarted = ProjectWorkspace(self.projects, picker=FakePicker())
        replay = restarted.set_directory(
            project.project_id,
            "request-save-replay",
            project.revision,
            DirectoryRole.SOURCE,
            selection["selection_token"],
        )
        self.assertEqual(replay, saved)


class MacOSDirectoryPickerTests(unittest.TestCase):
    def test_uses_static_script_and_returns_absolute_path(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b"/Volumes/Camera/Raw/\n", b"")
        with mock.patch("clipen.projects.picker.sys.platform", "darwin"):
            with mock.patch("clipen.projects.picker.subprocess.run", return_value=completed) as run:
                selected = MacOSDirectoryPicker().choose(DirectoryRole.SOURCE)
        self.assertEqual(selected, Path("/Volumes/Camera/Raw"))
        command = run.call_args.args[0]
        self.assertEqual(command[0:2], ("/usr/bin/osascript", "-e"))
        self.assertIn("tell current application to activate", command[2])
        self.assertIn("選擇毛片資料夾", command[2])

    def test_cancel_and_failures_have_distinct_results(self) -> None:
        cancelled = subprocess.CompletedProcess([], 0, b"\n", b"")
        with mock.patch("clipen.projects.picker.sys.platform", "darwin"):
            with mock.patch("clipen.projects.picker.subprocess.run", return_value=cancelled):
                self.assertIsNone(MacOSDirectoryPicker().choose(DirectoryRole.REFERENCE))
            with mock.patch(
                "clipen.projects.picker.subprocess.run",
                side_effect=subprocess.TimeoutExpired("osascript", 300),
            ):
                with self.assertRaises(ProjectError) as context:
                    MacOSDirectoryPicker().choose(DirectoryRole.SOURCE)
        self.assertEqual(context.exception.code, "picker_timeout")


if __name__ == "__main__":
    unittest.main()
