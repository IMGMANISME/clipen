import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import clipen.bootstrap as bootstrap
from clipen.asr_release import DOWNLOAD_BYTES, INSTALLED_BYTES, MODEL_NAME


class TTYInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class TTYOutput(io.StringIO):
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
        (self.repo / "requirements" / "asr-runtime.txt").write_text(
            "mlx-whisper==0.4.3\n", encoding="utf-8"
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

    def test_source_installer_help_does_not_start_setup(self) -> None:
        installer = Path(__file__).resolve().parents[2] / "install"
        completed = subprocess.run(
            [str(installer), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "用法：./install")

    def test_source_uninstaller_help_does_not_remove_anything(self) -> None:
        uninstaller = Path(__file__).resolve().parents[2] / "uninstall"
        completed = subprocess.run(
            [str(uninstaller), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "用法：./uninstall")

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
            result = bootstrap.main(self.repo, ["install"])

        self.assertEqual(result, 1)
        self.assertIn("尚不支援 Windows", errors.getvalue())
        install.assert_not_called()

    def test_unsupported_hardware_fails_before_creating_installation(self) -> None:
        with (
            mock.patch("clipen.bootstrap.platform.machine", return_value="x86_64"),
            self.assertRaisesRegex(bootstrap.BootstrapError, "Apple 晶片"),
        ):
            bootstrap.ensure_installation(
                self.repo,
                data_root=self.data,
                home=self.home,
                environment=self.environment,
                stdin=TTYInput("y\n"),
                stdout=io.StringIO(),
            )
        self.assertFalse(self.data.exists())

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
        with (
            mock.patch("clipen.bootstrap._install_runtime", side_effect=fake_install),
            mock.patch(
                "clipen.bootstrap.quick_release_is_ready",
                side_effect=(False, True),
            ),
            mock.patch("clipen.bootstrap._install_asr_bundle") as install_asr,
        ):
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
        install_asr.assert_called_once()
        command = self.data / "bin" / "clipen"
        self.assertTrue(command.is_file())
        self.assertTrue(command.stat().st_mode & 0o100)
        self.assertIn(str(self.repo / "clipen"), command.read_text(encoding="utf-8"))
        profile = (self.home / ".zshrc").read_text(encoding="utf-8")
        self.assertIn(str(self.data / "bin"), profile)
        self.assertEqual(output.getvalue().count("[Y/n]"), 1)
        self.assertIn(f"{DOWNLOAD_BYTES / 1_000_000_000:.1f} GB", output.getvalue())
        self.assertIn(f"{INSTALLED_BYTES / 1_000_000_000:.1f} GB", output.getvalue())
        self.assertIn(MODEL_NAME, output.getvalue())
        self.assertIn("MIT", output.getvalue())
        self.assertIn("下次開啟 Terminal", output.getvalue())

    def test_asr_failure_keeps_runtime_but_does_not_publish_command(self) -> None:
        def fake_install(
            staging: Path, repo: Path, fingerprint: str, log_path: Path
        ) -> None:
            python = bootstrap.runtime_python(staging)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o700)
            (staging / "clipen-runtime.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "fingerprint": fingerprint,
                        "repo_root": str(repo.resolve()),
                    }
                ),
                encoding="utf-8",
            )
            log_path.write_text("ok", encoding="utf-8")

        with (
            mock.patch("clipen.bootstrap._install_runtime", side_effect=fake_install),
            mock.patch("clipen.bootstrap.quick_release_is_ready", return_value=False),
            mock.patch(
                "clipen.bootstrap._install_asr_bundle",
                side_effect=bootstrap.BootstrapError("download failed"),
            ),
        ):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "download failed"):
                bootstrap.ensure_installation(
                    self.repo,
                    data_root=self.data,
                    home=self.home,
                    environment=self.environment,
                    stdin=TTYInput("y\n"),
                    stdout=io.StringIO(),
                )

        fingerprint = bootstrap.runtime_fingerprint(self.repo)
        self.assertTrue(
            bootstrap.runtime_is_ready(
                self.data / "runtimes" / fingerprint, fingerprint, self.repo
            )
        )
        self.assertFalse((self.data / "bin" / "clipen").exists())

    def test_retry_reuses_core_runtime_and_only_finishes_missing_asr(self) -> None:
        fingerprint = bootstrap.runtime_fingerprint(self.repo)
        runtime = self.data / "runtimes" / fingerprint
        python = bootstrap.runtime_python(runtime)
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n", encoding="utf-8")
        python.chmod(0o700)
        (runtime / "clipen-runtime.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "fingerprint": fingerprint,
                    "repo_root": str(self.repo.resolve()),
                }
            ),
            encoding="utf-8",
        )
        output = io.StringIO()
        with (
            mock.patch("clipen.bootstrap._install_runtime") as install_runtime,
            mock.patch(
                "clipen.bootstrap.quick_release_is_ready",
                side_effect=(False, True),
            ),
            mock.patch("clipen.bootstrap._install_asr_bundle") as install_asr,
        ):
            observed = bootstrap.ensure_installation(
                self.repo,
                data_root=self.data,
                home=self.home,
                environment=self.environment,
                stdin=TTYInput("y\n"),
                stdout=output,
            )

        self.assertEqual(observed, runtime)
        install_runtime.assert_not_called()
        install_asr.assert_called_once()
        self.assertIn("Clipen 需要完成設定", output.getvalue())
        self.assertNotIn("Clipen runtime：", output.getvalue())
        self.assertTrue((self.data / "bin" / "clipen").is_file())

    def test_asr_subprocess_progress_is_bounded_streamed_and_logged(self) -> None:
        runtime = self.root / "runtime"
        python = bootstrap.runtime_python(runtime)
        python.parent.mkdir(parents=True)
        python.write_text("python", encoding="utf-8")
        output = io.StringIO()
        log = self.root / "asr.log"
        process = mock.Mock()
        process.stdout = iter(("正在建立…\n", "下載完成\n"))
        process.wait.return_value = 0
        with mock.patch("clipen.bootstrap.subprocess.Popen", return_value=process) as popen:
            bootstrap._install_asr_bundle(runtime, self.repo, self.data, log, output)

        command = popen.call_args.args[0]
        self.assertIn("clipen.analysis.setup_cli", command)
        self.assertEqual(output.getvalue(), "正在建立…\n下載完成\n")
        self.assertEqual(log.read_text(encoding="utf-8"), output.getvalue())

    def test_asr_progress_refreshes_one_terminal_line_but_keeps_log_history(self) -> None:
        runtime = self.root / "runtime-progress"
        python = bootstrap.runtime_python(runtime)
        python.parent.mkdir(parents=True)
        python.write_text("python", encoding="utf-8")
        output = TTYOutput()
        log = self.root / "asr-progress.log"
        prefix = bootstrap.PROGRESS_RECORD_PREFIX
        process = mock.Mock()
        process.stdout = iter(
            (
                f"{prefix}[░░░] 1% 建立環境\n",
                f"{prefix}[█░░] 5% 下載套件\n",
                "✓ 安裝完成\n",
            )
        )
        process.wait.return_value = 0

        with mock.patch("clipen.bootstrap.subprocess.Popen", return_value=process):
            bootstrap._install_asr_bundle(runtime, self.repo, self.data, log, output)

        self.assertEqual(
            output.getvalue(),
            "\r[░░░] 1% 建立環境\r[█░░] 5% 下載套件\n✓ 安裝完成\n",
        )
        self.assertEqual(
            log.read_text(encoding="utf-8"),
            "[░░░] 1% 建立環境\n[█░░] 5% 下載套件\n✓ 安裝完成\n",
        )

    def test_keyboard_interrupt_during_setup_returns_130_without_exec(self) -> None:
        errors = io.StringIO()
        with (
            mock.patch(
                "clipen.bootstrap.ensure_installation", side_effect=KeyboardInterrupt
            ),
            mock.patch("clipen.bootstrap.os.execve") as execute,
            mock.patch("sys.stderr", errors),
        ):
            result = bootstrap.main(self.repo, ["install"])
        self.assertEqual(result, 130)
        self.assertIn("設定已取消", errors.getvalue())
        execute.assert_not_called()

    def test_install_command_only_sets_up_and_never_launches_ui(self) -> None:
        runtime = self.root / "runtime"
        output = io.StringIO()
        with (
            mock.patch("clipen.bootstrap.ensure_installation", return_value=runtime) as setup,
            mock.patch("clipen.bootstrap.os.execve") as execute,
            mock.patch("sys.stdout", output),
        ):
            result = bootstrap.main(self.repo, ["install"])

        self.assertEqual(result, 0)
        setup.assert_called_once_with(self.repo)
        execute.assert_not_called()
        self.assertIn("Clipen 已安裝完成", output.getvalue())
        self.assertIn("clipen codex", output.getvalue())

    def test_codex_command_requires_completed_install_without_running_setup(self) -> None:
        errors = io.StringIO()
        with (
            mock.patch(
                "clipen.bootstrap.completed_runtime",
                side_effect=bootstrap.BootstrapError("請先執行 ./install"),
            ),
            mock.patch("clipen.bootstrap.ensure_installation") as setup,
            mock.patch("clipen.bootstrap.os.execve") as execute,
            mock.patch("sys.stderr", errors),
        ):
            result = bootstrap.main(self.repo, ["codex"])

        self.assertEqual(result, 1)
        self.assertIn("./install", errors.getvalue())
        setup.assert_not_called()
        execute.assert_not_called()

    def test_codex_command_launches_from_completed_runtime(self) -> None:
        runtime = self.root / "runtime"
        python = bootstrap.runtime_python(runtime)
        with (
            mock.patch("clipen.bootstrap.completed_runtime", return_value=runtime),
            mock.patch("clipen.bootstrap.os.execve") as execute,
        ):
            result = bootstrap.main(self.repo, ["codex"])

        self.assertEqual(result, 1)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0], python)
        self.assertEqual(
            execute.call_args.args[1],
            [str(python), "-m", "clipen.cli", "codex"],
        )

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

    def test_uninstall_removes_only_managed_installation_and_preserves_work(self) -> None:
        command, profile = bootstrap._install_command(
            self.repo, self.data, self.home, self.environment
        )
        for name in bootstrap._REMOVABLE_DIRECTORIES:
            directory = self.data / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "managed.txt").write_text("remove", encoding="utf-8")
        for name in bootstrap._REMOVABLE_FILES:
            (self.data / name).write_text("remove", encoding="utf-8")
        project = self.data / "projects" / "project-1" / "state.sqlite3"
        project.parent.mkdir(parents=True)
        project.write_text("keep project", encoding="utf-8")
        attempts = self.data / "provider-attempts" / "attempt-1" / "result.json"
        attempts.parent.mkdir(parents=True)
        attempts.write_text("keep attempt", encoding="utf-8")
        raw = self.root / "external-drive" / "raw.mov"
        raw.parent.mkdir()
        raw.write_text("keep raw", encoding="utf-8")
        output = io.StringIO()

        removed = bootstrap.uninstall_installation(
            data_root=self.data,
            home=self.home,
            environment=self.environment,
            stdin=TTYInput("y\n"),
            stdout=output,
        )

        self.assertTrue(removed)
        self.assertFalse(command.exists())
        self.assertNotIn("Clipen managed PATH", profile.read_text(encoding="utf-8"))
        for name in bootstrap._REMOVABLE_DIRECTORIES:
            self.assertFalse((self.data / name).exists())
        for name in bootstrap._REMOVABLE_FILES:
            self.assertFalse((self.data / name).exists())
        self.assertEqual(project.read_text(encoding="utf-8"), "keep project")
        self.assertEqual(attempts.read_text(encoding="utf-8"), "keep attempt")
        self.assertEqual(raw.read_text(encoding="utf-8"), "keep raw")
        self.assertIn("作品資料與所有毛片均已保留", output.getvalue())

    def test_declining_uninstall_changes_nothing(self) -> None:
        command, profile = bootstrap._install_command(
            self.repo, self.data, self.home, self.environment
        )
        runtime = self.data / "runtimes" / "release"
        runtime.mkdir(parents=True)

        removed = bootstrap.uninstall_installation(
            data_root=self.data,
            home=self.home,
            environment=self.environment,
            stdin=TTYInput("n\n"),
            stdout=io.StringIO(),
        )

        self.assertFalse(removed)
        self.assertTrue(command.is_file())
        self.assertTrue(runtime.is_dir())
        self.assertIn("Clipen managed PATH", profile.read_text(encoding="utf-8"))

    def test_uninstall_refuses_symlinked_runtime_before_changing_command(self) -> None:
        command, profile = bootstrap._install_command(
            self.repo, self.data, self.home, self.environment
        )
        outside = self.root / "outside-runtime"
        outside.mkdir()
        (outside / "sentinel").write_text("keep", encoding="utf-8")
        (self.data / "runtimes").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(bootstrap.BootstrapError, "不安全"):
            bootstrap.uninstall_installation(
                data_root=self.data,
                home=self.home,
                environment=self.environment,
                stdin=TTYInput("y\n"),
                stdout=io.StringIO(),
            )

        self.assertTrue(command.is_file())
        self.assertIn("Clipen managed PATH", profile.read_text(encoding="utf-8"))
        self.assertEqual((outside / "sentinel").read_text(encoding="utf-8"), "keep")

    def test_uninstall_refuses_while_controller_is_running(self) -> None:
        self.data.mkdir()
        project = self.data / "projects" / "project-1" / "state.sqlite3"
        project.parent.mkdir(parents=True)
        project.write_text("keep", encoding="utf-8")
        with (
            mock.patch("clipen.bootstrap._controller_is_running", return_value=True),
            self.assertRaisesRegex(bootstrap.BootstrapError, "仍在執行"),
        ):
            bootstrap.uninstall_installation(
                data_root=self.data,
                home=self.home,
                environment=self.environment,
                stdin=TTYInput("y\n"),
                stdout=io.StringIO(),
            )
        self.assertEqual(project.read_text(encoding="utf-8"), "keep")

    def test_uninstall_never_removes_an_unmanaged_command(self) -> None:
        command = self.data / "bin" / "clipen"
        command.parent.mkdir(parents=True)
        command.write_text("#!/bin/sh\necho other-tool\n", encoding="utf-8")
        runtime = self.data / "runtimes" / "release"
        runtime.mkdir(parents=True)

        with self.assertRaisesRegex(bootstrap.BootstrapError, "不是由 Clipen 管理"):
            bootstrap.uninstall_installation(
                data_root=self.data,
                home=self.home,
                environment=self.environment,
                stdin=TTYInput("y\n"),
                stdout=io.StringIO(),
            )

        self.assertIn("other-tool", command.read_text(encoding="utf-8"))
        self.assertTrue(runtime.is_dir())


if __name__ == "__main__":
    unittest.main()
