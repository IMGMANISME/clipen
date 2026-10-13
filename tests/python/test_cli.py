from pathlib import Path
import tempfile
import unittest
from unittest import mock

from clipen import cli
from clipen.system_skills import SystemSkillVerificationError
from clipen.workspace import WorkspaceInspection, WorkspacePaths


class CliTests(unittest.TestCase):
    def test_verifies_system_skills_before_provider_probe_and_server(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            release = mock.sentinel.release
            provider = mock.sentinel.provider
            calls: list[str] = []
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            managed = workspace_root / ".clipen"
            projects = managed / "projects"
            projects.mkdir(parents=True)
            workspace = WorkspacePaths(
                workspace_root,
                managed,
                projects,
                managed / "workspace.json",
                "wsp_" + "1" * 32,
                False,
            )

            def verify(path: Path):
                calls.append("verify")
                self.assertEqual(path, root / "resources" / "system-skills")
                return release

            def probe(path: Path):
                calls.append("probe")
                self.assertEqual(path, data / "provider-attempts")
                return provider

            def run(paths, received_provider, authority, received_release):
                calls.append("run")
                self.assertEqual(received_provider, provider)
                self.assertEqual(received_release, release)
                self.assertEqual(paths.projects_root, projects)
                self.assertEqual(
                    paths.instance_root,
                    data / "workspace-instances" / ("wsp_" + "1" * 32),
                )
                return 0

            with (
                mock.patch("clipen.cli._repo_root", return_value=root),
                mock.patch("clipen.cli._data_root", return_value=data),
                mock.patch("clipen.cli.prepare_workspace", return_value=workspace),
                mock.patch(
                    "clipen.cli.inspect_workspace_projects",
                    return_value=WorkspaceInspection(1),
                ) as inspect,
                mock.patch("clipen.cli.verify_system_skill_release", side_effect=verify),
                mock.patch("clipen.cli.probe_codex", side_effect=probe),
                mock.patch("clipen.cli.run_server", side_effect=run),
            ):
                self.assertEqual(cli.main(["codex"]), 0)

            self.assertEqual(calls, ["verify", "probe", "run"])
            inspect.assert_called_once_with(workspace)

    def test_invalid_system_skill_release_stops_before_provider_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = root / "workspace" / ".clipen"
            (managed / "projects").mkdir(parents=True)
            workspace = WorkspacePaths(
                root / "workspace",
                managed,
                managed / "projects",
                managed / "workspace.json",
                "wsp_" + "2" * 32,
                False,
            )
            with (
                mock.patch("clipen.cli._repo_root", return_value=root),
                mock.patch("clipen.cli._data_root", return_value=root / "data"),
                mock.patch("clipen.cli.prepare_workspace", return_value=workspace),
                mock.patch(
                    "clipen.cli.inspect_workspace_projects",
                    return_value=WorkspaceInspection(0),
                ),
                mock.patch(
                    "clipen.cli.verify_system_skill_release",
                    side_effect=SystemSkillVerificationError("invalid"),
                ),
                mock.patch("clipen.cli.probe_codex") as probe,
                mock.patch("clipen.cli.run_server") as run,
            ):
                self.assertEqual(cli.main(["codex"]), 1)

            probe.assert_not_called()
            run.assert_not_called()

    def test_invalid_workspace_project_stops_before_provider_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = root / ".clipen"
            projects = managed / "projects"
            projects.mkdir(parents=True)
            workspace = WorkspacePaths(
                root,
                managed,
                projects,
                managed / "workspace.json",
                "wsp_" + "4" * 32,
                False,
            )
            with (
                mock.patch("clipen.cli._repo_root", return_value=root),
                mock.patch("clipen.cli._data_root", return_value=root / "data"),
                mock.patch("clipen.cli.prepare_workspace", return_value=workspace),
                mock.patch(
                    "clipen.cli.inspect_workspace_projects",
                    side_effect=cli.WorkspaceError("作品損壞"),
                ),
                mock.patch("clipen.cli.verify_system_skill_release") as verify,
                mock.patch("clipen.cli.probe_codex") as probe,
                mock.patch("clipen.cli.run_server") as run,
            ):
                self.assertEqual(cli.main(["codex"]), 1)

            verify.assert_not_called()
            probe.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
