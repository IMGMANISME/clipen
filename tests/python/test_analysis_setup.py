import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from clipen.analysis import ASRSetupError, ASRSetupManager, ASRSetupState
from clipen.analysis.setup import ASRSetupStatus
from clipen.analysis import setup_cli


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class ASRSetupManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text("example==1.0\n", encoding="utf-8")
        self.files = {"config.json": b"{}", "weights.npz": b"tiny-model"}
        self.model_files = tuple(
            (name, len(value), _sha256(value)) for name, value in self.files.items()
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manager(self, **overrides) -> ASRSetupManager:
        arguments = {
            "data_root": self.root / "data",
            "requirements": self.requirements,
            "system": "Darwin",
            "machine": "arm64",
            "model_repository": "test/model",
            "model_revision": "fixed-test-revision",
            "model_files": self.model_files,
            "runtime_installer": self._install_runtime,
            "downloader": self._download,
        }
        arguments.update(overrides)
        return ASRSetupManager(**arguments)

    @staticmethod
    def _install_runtime(destination: Path, _requirements: Path, cancel: threading.Event) -> None:
        if cancel.is_set():
            raise ASRSetupError("cancelled")
        executable = destination / "bin" / "python"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)

    def _download(
        self,
        url: str,
        destination: Path,
        expected_size: int,
        expected_sha256: str,
        cancel: threading.Event,
    ) -> None:
        if cancel.is_set():
            raise ASRSetupError("cancelled")
        value = self.files[url.rsplit("/", 1)[-1]]
        self.assertEqual(len(value), expected_size)
        self.assertEqual(_sha256(value), expected_sha256)
        destination.write_bytes(value)
        destination.chmod(0o600)

    @staticmethod
    def _wait(manager: ASRSetupManager, expected: ASRSetupState) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if manager.status().state is expected:
                return
            time.sleep(0.01)
        raise AssertionError(f"ASR setup did not reach {expected.value}: {manager.status()}")

    def test_install_requires_exact_notice_and_supported_platform(self) -> None:
        manager = self.manager()
        with self.assertRaisesRegex(ASRSetupError, "asr_notice_required"):
            manager.start_install(None)
        unsupported = self.manager(data_root=self.root / "other", machine="x86_64")
        with self.assertRaisesRegex(ASRSetupError, "asr_platform_unsupported"):
            unsupported.start_install("asr-mlx-whisper-small-v1")

    def test_atomic_install_is_verified_after_restart_and_removable(self) -> None:
        manager = self.manager()
        notice = manager.public_status()["notice"]
        self.assertEqual(notice["model"]["revision"], "fixed-test-revision")
        self.assertEqual(notice["model"]["model_bytes"], 12)
        manager.start_install("asr-mlx-whisper-small-v1")
        self._wait(manager, ASRSetupState.READY)
        release = manager.release
        self.assertTrue((release / "receipt.json").is_file())
        self.assertFalse(any(manager.root.glob(".staging-*")))

        restarted = self.manager()
        self.assertEqual(restarted.status().state, ASRSetupState.READY)
        self.assertIsNotNone(restarted.adapter_factory())
        self.assertEqual(restarted.model_snapshot()["revision"], "fixed-test-revision")

        self.requirements.write_text("example==2.0\n", encoding="utf-8")
        changed_contract = self.manager()
        self.assertEqual(changed_contract.status().state, ASRSetupState.FAILED)
        self.assertEqual(changed_contract.status().error_code, "asr_receipt_invalid")

        outside = self.root / "raw-footage.mov"
        outside.write_bytes(b"never-delete")
        observed = []
        self.assertEqual(
            changed_contract.remove(
                lambda: observed.append(changed_contract.status().state)
            ).state,
            ASRSetupState.SETUP_REQUIRED,
        )
        self.assertEqual(observed, [ASRSetupState.REMOVING])
        self.assertEqual(outside.read_bytes(), b"never-delete")
        self.assertFalse(release.exists())

    def test_cancel_removes_only_private_staging(self) -> None:
        entered = threading.Event()

        def blocked_installer(
            _destination: Path, _requirements: Path, cancel: threading.Event
        ) -> None:
            entered.set()
            while not cancel.wait(0.01):
                pass
            raise ASRSetupError("cancelled")

        manager = self.manager(runtime_installer=blocked_installer)
        manager.start_install("asr-mlx-whisper-small-v1")
        self.assertTrue(entered.wait(1))
        manager.cancel_install()
        self._wait(manager, ASRSetupState.SETUP_REQUIRED)
        self.assertEqual(manager.status().phase, "cancelled")
        self.assertFalse(manager.release.exists())
        self.assertFalse(any(manager.root.glob(".staging-*")))

    def test_corrupt_download_never_publishes_release(self) -> None:
        def corrupt(
            _url: str,
            destination: Path,
            _size: int,
            _digest: str,
            _cancel: threading.Event,
        ) -> None:
            destination.write_bytes(b"corrupt")

        manager = self.manager(downloader=corrupt)
        manager.start_install("asr-mlx-whisper-small-v1")
        self._wait(manager, ASRSetupState.FAILED)
        self.assertEqual(manager.status().error_code, "asr_model_invalid")
        self.assertFalse(manager.release.exists())
        self.assertFalse(any(manager.root.glob(".staging-*")))

    def test_restart_cleans_only_well_formed_staging_directories(self) -> None:
        root = self.root / "data" / "asr"
        stale = root / (".staging-" + "a" * 32)
        unrelated = root / ".staging-not-owned"
        stale.mkdir(parents=True)
        unrelated.mkdir()
        (stale / "partial").write_text("x", encoding="utf-8")
        self.manager()
        self.assertFalse(stale.exists())
        self.assertTrue(unrelated.exists())

    def test_restart_rejects_symlinked_runtime_without_deleting_target(self) -> None:
        manager = self.manager()
        manager.start_install("asr-mlx-whisper-small-v1")
        self._wait(manager, ASRSetupState.READY)
        runtime = manager.release / "runtime"
        outside = self.root / "outside-runtime"
        runtime.rename(outside)
        runtime.symlink_to(outside, target_is_directory=True)

        restarted = self.manager()
        self.assertEqual(restarted.status().state, ASRSetupState.FAILED)
        self.assertEqual(restarted.status().error_code, "asr_runtime_invalid")
        restarted.remove()
        self.assertTrue(outside.is_dir())

    def test_runtime_environment_copies_python_and_installs_only_pinned_binaries(self) -> None:
        manager = self.manager()
        destination = self.root / "runtime"

        def create_runtime(path: Path) -> None:
            executable = path / "bin" / "python"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"python")

        builder = mock.Mock()
        builder.create.side_effect = create_runtime
        with (
            mock.patch("clipen.analysis.setup.venv.EnvBuilder", return_value=builder) as factory,
            mock.patch.object(manager, "_run") as run,
        ):
            manager._install_runtime(destination, self.requirements, threading.Event())

        factory.assert_called_once_with(with_pip=True, clear=False, symlinks=False)
        command, cancel = run.call_args.args
        self.assertIsInstance(cancel, threading.Event)
        self.assertIn("--only-binary=:all:", command)
        self.assertIn("--no-cache-dir", command)
        self.assertEqual(command[-2:], ("-r", str(self.requirements)))

    def test_blocking_setup_entry_reports_phases_and_closes_manager(self) -> None:
        manager = mock.Mock()
        manager.status.side_effect = (
            ASRSetupStatus(ASRSetupState.SETUP_REQUIRED, "not_installed", "尚未安裝"),
            ASRSetupStatus(ASRSetupState.INSTALLING, "creating_runtime", "建立環境"),
            ASRSetupStatus(ASRSetupState.READY, "ready", "安裝完成", completed_units=3),
        )
        output = mock.Mock()
        with (
            mock.patch("clipen.analysis.setup_cli.ASRSetupManager", return_value=manager),
            mock.patch("clipen.analysis.setup_cli.time.sleep"),
            mock.patch("sys.stdout", output),
        ):
            result = setup_cli.main(
                [
                    "--data-root",
                    str(self.root / "data"),
                    "--requirements",
                    str(self.requirements),
                ]
            )
        self.assertEqual(result, 0)
        manager.start_install.assert_called_once_with("asr-mlx-whisper-small-v1")
        manager.close.assert_called_once_with(timeout=10)


if __name__ == "__main__":
    unittest.main()
