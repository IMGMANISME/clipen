from pathlib import Path
import tempfile
import unittest
from unittest import mock

from clipen.projects import ProjectRepository
from clipen.workspace import (
    WorkspaceError,
    inspect_workspace_projects,
    prepare_workspace,
)


class WorkspaceTests(unittest.TestCase):
    def test_prepare_workspace_is_scoped_to_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = prepare_workspace(root)
            second = prepare_workspace(root)

            self.assertTrue(first.created)
            self.assertFalse(second.created)
            resolved = root.resolve()
            self.assertEqual(first.projects_root, resolved / ".clipen" / "projects")
            self.assertRegex(first.workspace_id, r"wsp_[0-9a-f]{32}")
            self.assertEqual(first.workspace_id, second.workspace_id)
            self.assertTrue(first.manifest_path.is_file())

    def test_prepare_workspace_accepts_volume_without_posix_chmod(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch(
                "clipen.filesystem.os.chmod",
                side_effect=OSError("operation not supported"),
            ):
                workspace = prepare_workspace(root)
                project = ProjectRepository(workspace.projects_root).create_project(
                    "request-non-posix-volume",
                    "外接硬碟作品",
                )

            self.assertTrue(workspace.projects_root.is_dir())
            self.assertTrue(
                (workspace.projects_root / project.project_id / "project.sqlite3").is_file()
            )

    def test_inspection_accepts_empty_workspace_and_valid_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = prepare_workspace(Path(temporary))

            self.assertEqual(inspect_workspace_projects(workspace).project_count, 0)
            ProjectRepository(workspace.projects_root).create_project(
                "request-inspect-project",
                "合法作品",
            )

            self.assertEqual(inspect_workspace_projects(workspace).project_count, 1)

    def test_inspection_rejects_corrupt_project_without_modifying_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = prepare_workspace(Path(temporary))
            project_id = "prj_" + "a" * 32
            project = workspace.projects_root / project_id
            project.mkdir()
            database = project / "project.sqlite3"
            database.write_bytes(b"not sqlite")

            with self.assertRaisesRegex(WorkspaceError, project_id):
                inspect_workspace_projects(workspace)

            self.assertEqual(database.read_bytes(), b"not sqlite")

    def test_existing_unrecognized_clipen_directory_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = root / ".clipen"
            managed.mkdir()
            marker = managed / "unknown.txt"
            marker.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(WorkspaceError, "無法識別"):
                prepare_workspace(root)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
