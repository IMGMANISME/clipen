import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import clipen.bootstrap as bootstrap


class TTYInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        (self.repo / "requirements").mkdir(parents=True)
        (self.repo / "pyproject.toml").write_text("[project]\nname='clipen'\n", encoding="utf-8")
        (self.repo / "requirements" / "runtime.txt").write_text(
            "starlette==1.6.0\n", encoding="utf-8"
        )
        launcher = self.repo / "clipen"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o755)
        self.data = self.root / "data"
        self.home = self.root / "home"
        self.home.mkdir()
        self.environment = {"SHELL": "/bin/zsh", "PATH": "/usr/bin:/bin"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fingerprint_changes_with_runtime_contract(self) -> None:
        first = bootstrap.runtime_fingerprint(self.repo)
        (self.repo / "requirements" / "runtime.txt").write_text(
            "starlette==1.5.1\n", encoding="utf-8"
        )
        second = bootstrap.runtime_fingerprint(self.repo)
        self.assertNotEqual(first, second)

    def test_declining_first_setup_leaves_no_runtime(self) -> None:
        output = io.StringIO()
        runtime = bootstrap.ensure_runtime(
            self.repo,
            data_root=self.data,
            stdin=TTYInput("n\n"),
            stdout=output,
        )
        self.assertIsNone(runtime)
        self.assertIn("已取消", output.getvalue())
        self.assertEqual(list((self.data / "runtimes").glob("[0-9a-f]*")), [])

    def test_noninteractive_first_setup_fails_closed(self) -> None:
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.ensure_runtime(
                self.repo,
                data_root=self.data,
                stdin=io.StringIO(),
                stdout=io.StringIO(),
            )

    def test_windows_fails_before_setup_creates_files_or_prompts(self) -> None:
        errors = io.StringIO()
        with (
            mock.patch("clipen.bootstrap._fcntl", None),
            mock.patch("clipen.bootstrap.ensure_installation") as install,
            mock.patch("sys.stderr", errors),
        ):
            result = bootstrap.main(self.repo, ["codex"])

        self.assertEqual(result, 1)
        self.assertIn("尚不支援 Windows", errors.getvalue())
        install.assert_not_called()

    def test_successful_runtime_is_reused_without_prompt_or_reinstall(self) -> None:
        installs = []

        def fake_install(staging: Path, repo: Path, fingerprint: str, log_path: Path) -> None:
            installs.append(staging)
            python = bootstrap.runtime_python(staging)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o700)
            receipt = {
                "schema": 1,
                "fingerprint": fingerprint,
                "repo_root": str(repo.resolve()),
            }
            (staging / "clipen-runtime.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            log_path.write_text("ok", encoding="utf-8")

        with mock.patch("clipen.bootstrap._install_runtime", side_effect=fake_install):
            first = bootstrap.ensure_runtime(
                self.repo,
                data_root=self.data,
                stdin=TTYInput("y\n"),
                stdout=io.StringIO(),
            )
            second = bootstrap.ensure_runtime(
                self.repo,
                data_root=self.data,
                stdin=io.StringIO(),
                stdout=io.StringIO(),
            )

        self.assertEqual(first, second)
        self.assertEqual(len(installs), 1)
        assert first is not None
        self.assertTrue(bootstrap.runtime_is_ready(
            first, bootstrap.runtime_fingerprint(self.repo), self.repo
        ))

    def test_first_install_uses_one_confirmation_for_runtime_and_command(self) -> None:
        installs = []

        def fake_install(staging: Path, repo: Path, fingerprint: str, log_path: Path) -> None:
            installs.append(staging)
            python = bootstrap.runtime_python(staging)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o700)
            receipt = {
                "schema": 1,
                "fingerprint": fingerprint,
                "repo_root": str(repo.resolve()),
            }
            (staging / "clipen-runtime.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            log_path.write_text("ok", encoding="utf-8")

        output = io.StringIO()
        with mock.patch("clipen.bootstrap._install_runtime", side_effect=fake_install):
            runtime = bootstrap.ensure_installation(
                self.repo,
                data_root=self.data,
                home=self.home,
                environment=self.environment,
                stdin=TTYInput("y\n"),
                stdout=output,
            )

        self.assertIsNotNone(runtime)
        self.assertEqual(len(installs), 1)
        command = self.data / "bin" / "clipen"
        self.assertTrue(command.is_file())
        self.assertTrue(command.stat().st_mode & 0o100)
        self.assertIn(str(self.repo / "clipen"), command.read_text(encoding="utf-8"))
        profile = (self.home / ".zshrc").read_text(encoding="utf-8")
        self.assertIn(str(self.data / "bin"), profile)
        self.assertEqual(output.getvalue().count("[Y/n]"), 1)
        self.assertIn("下次開啟 Terminal", output.getvalue())

    def test_command_setup_is_idempotent_and_preserves_shell_settings(self) -> None:
        profile = self.home / ".zshrc"
        profile.write_text("export EDITOR=vim\n", encoding="utf-8")

        bootstrap._install_command(
            self.repo, self.data, self.home, self.environment
        )
        first = profile.read_text(encoding="utf-8")
        bootstrap._install_command(
            self.repo, self.data, self.home, self.environment
        )
        second = profile.read_text(encoding="utf-8")

        self.assertEqual(first, second)
        self.assertIn("export EDITOR=vim", second)
        self.assertEqual(second.count("Clipen managed PATH"), 2)
        self.assertTrue(
            bootstrap._command_is_ready(
                self.repo, self.data, self.home, self.environment
            )
        )

    def test_profile_exposes_command_when_install_path_contains_spaces(self) -> None:
        spaced_data = self.root / "Application Support" / "Clipen"
        command, profile = bootstrap._install_command(
            self.repo, spaced_data, self.home, self.environment
        )

        completed = subprocess.run(
            [
                "/bin/sh",
                "-c",
                '. "$1"; command -v clipen',
                "sh",
                str(profile),
            ],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), str(command))

    def test_command_setup_refuses_a_symlinked_shell_profile(self) -> None:
        target = self.home / "real-profile"
        target.write_text("keep\n", encoding="utf-8")
        (self.home / ".zshrc").symlink_to(target)

        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap._install_command(
                self.repo, self.data, self.home, self.environment
            )

        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse((self.data / "bin" / "clipen").exists())


if __name__ == "__main__":
    unittest.main()
