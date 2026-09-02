"""Explicit, atomic installation lifecycle for the optional local ASR runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import venv

from clipen.asr_release import (
    CONVERSION_NOTICE,
    DOWNLOAD_BYTES,
    INSTALLED_BYTES,
    MODEL_FILES,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    NOTICE_VERSION,
    RELEASE_ID,
    WHISPER_LICENSE,
    WHISPER_LICENSE_URL,
)

from .transcription import LocalModelSnapshot, MLXWhisperAdapter


_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
_INSTALL_TIMEOUT_SECONDS = 60 * 60
_MAX_CAPTURE_BYTES = 4 * 1024 * 1024
_STAGING_NAME = re.compile(r"\.staging-[0-9a-f]{32}\Z")
_READ_SIZE = 1024 * 1024


class ASRSetupState(str, Enum):
    SETUP_REQUIRED = "setup_required"
    INSTALLING = "installing"
    READY = "ready"
    FAILED = "failed"
    REMOVING = "removing"


@dataclass(frozen=True)
class ASRSetupStatus:
    state: ASRSetupState
    phase: str
    message: str
    error_code: str | None = None
    completed_units: int = 0
    total_units: int = 3

    def to_public(
        self,
        install_root: Path,
        *,
        model_repository: str = MODEL_REPOSITORY,
        model_revision: str = MODEL_REVISION,
        model_files: tuple[tuple[str, int, str], ...] = MODEL_FILES,
    ) -> dict[str, object]:
        return {
            "state": self.state.value,
            "phase": self.phase,
            "message": self.message,
            "error_code": self.error_code,
            "completed_units": self.completed_units,
            "total_units": self.total_units,
            "notice": {
                "version": NOTICE_VERSION,
                "download_bytes": DOWNLOAD_BYTES,
                "installed_bytes": INSTALLED_BYTES,
                "install_location": str(install_root),
                "model": {
                    "name": "Whisper Small MLX",
                    "source": f"https://huggingface.co/{model_repository}/tree/{model_revision}",
                    "revision": model_revision,
                    "model_bytes": sum(item[1] for item in model_files),
                },
                "license": {
                    "whisper_software": WHISPER_LICENSE,
                    "whisper_license_url": WHISPER_LICENSE_URL,
                    "conversion_notice": CONVERSION_NOTICE,
                },
                "removable": True,
            },
        }


class ASRSetupError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ASRSetupManager:
    def __init__(
        self,
        data_root: Path,
        requirements: Path,
        *,
        downloader: Callable[[str, Path, int, str, threading.Event], None] | None = None,
        runtime_installer: Callable[[Path, Path, threading.Event], None] | None = None,
        system: str | None = None,
        machine: str | None = None,
        model_repository: str = MODEL_REPOSITORY,
        model_revision: str = MODEL_REVISION,
        model_files: tuple[tuple[str, int, str], ...] = MODEL_FILES,
    ) -> None:
        self.data_root = self._secure_directory(Path(data_root))
        self.root = self._secure_directory(self.data_root / "asr")
        self.release = self.root / RELEASE_ID
        self.requirements = Path(requirements).resolve(strict=True)
        self._downloader = downloader or self._download
        self._runtime_installer = runtime_installer or self._install_runtime
        self._system = system or platform.system()
        self._machine = machine or platform.machine()
        if (
            not model_repository
            or not model_revision
            or not model_files
            or any(
                not filename
                or Path(filename).name != filename
                or size < 1
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for filename, size, digest in model_files
            )
        ):
            raise ValueError("ASR model release metadata is invalid")
        self._model_repository = model_repository
        self._model_revision = model_revision
        self._model_files = model_files
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._cleanup_staging()
        self._status = self._initial_status()

    def status(self) -> ASRSetupStatus:
        with self._lock:
            return self._status

    def public_status(self) -> dict[str, object]:
        return self.status().to_public(
            self.release,
            model_repository=self._model_repository,
            model_revision=self._model_revision,
            model_files=self._model_files,
        )

    def start_install(self, accepted_notice: object) -> ASRSetupStatus:
        if accepted_notice != NOTICE_VERSION:
            raise ASRSetupError("asr_notice_required")
        if self._system != "Darwin" or self._machine != "arm64":
            raise ASRSetupError("asr_platform_unsupported")
        with self._lock:
            if self._status.state is ASRSetupState.READY:
                return self._status
            if self._worker is not None and self._worker.is_alive():
                raise ASRSetupError("asr_setup_busy")
            self._cancel = threading.Event()
            self._status = ASRSetupStatus(
                ASRSetupState.INSTALLING,
                "creating_runtime",
                "正在建立隔離的語音辨識環境…",
            )
            self._worker = threading.Thread(
                target=self._install_worker,
                name="clipen-asr-setup",
                daemon=True,
            )
            self._worker.start()
            return self._status

    def cancel_install(self) -> ASRSetupStatus:
        with self._lock:
            if self._status.state is not ASRSetupState.INSTALLING:
                return self._status
            self._cancel.set()
            return self._status

    def remove(self, before_remove: Callable[[], None] | None = None) -> ASRSetupStatus:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise ASRSetupError("asr_setup_busy")
            self._status = ASRSetupStatus(
                ASRSetupState.REMOVING,
                "removing",
                "正在移除 Clipen 管理的語音辨識環境…",
            )
        try:
            if before_remove is not None:
                before_remove()
            self._remove_release()
            status = ASRSetupStatus(
                ASRSetupState.SETUP_REQUIRED,
                "not_installed",
                "本機語音辨識尚未安裝。",
            )
        except ASRSetupError as error:
            status = ASRSetupStatus(
                ASRSetupState.FAILED,
                "remove_failed",
                "Clipen 無法安全移除語音辨識環境。",
                error.code,
            )
        except Exception:
            status = ASRSetupStatus(
                ASRSetupState.FAILED,
                "remove_failed",
                "Clipen 無法先安全停止素材分析。",
                "asr_pre_remove_failed",
            )
        with self._lock:
            self._status = status
            return status

    def adapter_factory(self) -> Callable[[], MLXWhisperAdapter] | None:
        binding = self.runtime_binding()
        return None if binding is None else binding[0]

    def model_snapshot(self) -> dict[str, object] | None:
        binding = self.runtime_binding()
        return None if binding is None else binding[1]

    def runtime_binding(
        self,
    ) -> tuple[Callable[[], MLXWhisperAdapter], dict[str, object]] | None:
        if self.status().state is not ASRSetupState.READY:
            return None
        runtime_python, snapshot = self._read_release()
        return (
            lambda: MLXWhisperAdapter(runtime_python, snapshot),
            {
                "id": snapshot.model_id,
                "revision": snapshot.revision,
                "manifest_sha256": snapshot.manifest_sha256,
            },
        )

    def close(self, timeout: float = 3) -> None:
        self.cancel_install()
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join(max(0, timeout))

    def _initial_status(self) -> ASRSetupStatus:
        if not self.release.exists():
            return ASRSetupStatus(
                ASRSetupState.SETUP_REQUIRED,
                "not_installed",
                "本機語音辨識尚未安裝。",
            )
        try:
            self._read_release()
            return ASRSetupStatus(
                ASRSetupState.READY,
                "ready",
                "本機語音辨識可用。",
                completed_units=3,
            )
        except ASRSetupError as error:
            return ASRSetupStatus(
                ASRSetupState.FAILED,
                "verification_failed",
                "本機語音辨識安裝不完整。",
                error.code,
            )

    def _install_worker(self) -> None:
        staging = self.root / f".staging-{secrets.token_hex(16)}"
        try:
            if self.release.exists():
                raise ASRSetupError("asr_release_conflict")
            staging.mkdir(mode=0o700)
            runtime = staging / "runtime"
            model = staging / "model"
            model.mkdir(mode=0o700)
            self._runtime_installer(runtime, self.requirements, self._cancel)
            self._set_progress("downloading_model", "正在下載固定版本的語音辨識模型…", 1)
            for filename, size, digest in self._model_files:
                url = (
                    f"https://huggingface.co/{self._model_repository}/resolve/"
                    f"{self._model_revision}/{filename}"
                )
                self._downloader(url, model / filename, size, digest, self._cancel)
            self._set_progress("verifying", "正在驗證安裝內容…", 2)
            manifest = self._hash_model(model)
            receipt = {
                "schema": 1,
                "release_id": RELEASE_ID,
                "notice_version": NOTICE_VERSION,
                "model_id": "mlx-whisper-small",
                "model_revision": self._model_revision,
                "model_manifest_sha256": manifest,
                "requirements_sha256": self._hash_file(self.requirements),
                "runtime_python": "runtime/bin/python",
                "model_path": "model",
            }
            self._write_receipt(staging / "receipt.json", receipt)
            os.replace(staging, self.release)
            self._read_release()
            status = ASRSetupStatus(
                ASRSetupState.READY,
                "ready",
                "本機語音辨識安裝完成。",
                completed_units=3,
            )
        except ASRSetupError as error:
            status = ASRSetupStatus(
                ASRSetupState.SETUP_REQUIRED if error.code == "cancelled" else ASRSetupState.FAILED,
                "cancelled" if error.code == "cancelled" else "install_failed",
                "安裝已取消。" if error.code == "cancelled" else "本機語音辨識安裝失敗。",
                None if error.code == "cancelled" else error.code,
            )
        except Exception:
            status = ASRSetupStatus(
                ASRSetupState.FAILED,
                "install_failed",
                "本機語音辨識安裝失敗。",
                "asr_setup_internal_error",
            )
        finally:
            try:
                self._remove_tree(staging, missing_ok=True)
            except ASRSetupError:
                status = ASRSetupStatus(
                    ASRSetupState.FAILED,
                    "cleanup_failed",
                    "本機語音辨識安裝失敗，且暫存內容無法安全清除。",
                    "asr_setup_cleanup_failed",
                )
        with self._lock:
            self._status = status

    def _set_progress(self, phase: str, message: str, completed: int) -> None:
        if self._cancel.is_set():
            raise ASRSetupError("cancelled")
        with self._lock:
            self._status = ASRSetupStatus(
                ASRSetupState.INSTALLING, phase, message, completed_units=completed
            )

    def _install_runtime(
        self, destination: Path, requirements: Path, cancel: threading.Event
    ) -> None:
        try:
            venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(destination)
        except Exception as error:
            raise ASRSetupError("asr_runtime_create_failed") from error
        self._set_progress(
            "installing_dependencies",
            "正在下載並安裝語音辨識套件…",
            0,
        )
        python = destination / "bin" / "python"
        command = (
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--only-binary=:all:",
            "--no-cache-dir",
            "-r",
            str(requirements),
        )
        self._run(command, cancel)

    @staticmethod
    def _download(
        url: str,
        destination: Path,
        expected_size: int,
        expected_sha256: str,
        cancel: threading.Event,
    ) -> None:
        digest = hashlib.sha256()
        total = 0
        request = Request(url, headers={"User-Agent": "Clipen/0.0"})
        try:
            with urlopen(request, timeout=30) as response, destination.open("xb") as output:
                while True:
                    if cancel.is_set():
                        raise ASRSetupError("cancelled")
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > min(_MAX_DOWNLOAD_BYTES, expected_size):
                        raise ASRSetupError("asr_download_too_large")
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
        except ASRSetupError:
            destination.unlink(missing_ok=True)
            raise
        except (OSError, HTTPError, URLError) as error:
            destination.unlink(missing_ok=True)
            raise ASRSetupError("asr_download_failed") from error
        if total != expected_size or digest.hexdigest() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise ASRSetupError("asr_download_verification_failed")
        os.chmod(destination, 0o600)

    def _run(self, command: tuple[str, ...], cancel: threading.Event) -> None:
        deadline = time.monotonic() + _INSTALL_TIMEOUT_SECONDS
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen(
                    command,
                    cwd="/",
                    env={
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                        "PIP_NO_INPUT": "1",
                        "PYTHONNOUSERSITE": "1",
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    start_new_session=os.name == "posix",
                )
            except OSError as error:
                raise ASRSetupError("asr_runtime_install_failed") from error
            while process.poll() is None:
                if cancel.is_set():
                    self._terminate(process)
                    raise ASRSetupError("cancelled")
                if time.monotonic() >= deadline:
                    self._terminate(process)
                    raise ASRSetupError("asr_runtime_install_timeout")
                if os.fstat(output.fileno()).st_size > _MAX_CAPTURE_BYTES:
                    self._terminate(process)
                    raise ASRSetupError("asr_runtime_install_output_too_large")
                time.sleep(0.1)
            if process.returncode != 0:
                raise ASRSetupError("asr_runtime_install_failed")

    def _read_release(self) -> tuple[Path, LocalModelSnapshot]:
        if self.release.is_symlink() or not self.release.is_dir():
            raise ASRSetupError("asr_release_invalid")
        receipt_path = self.release / "receipt.json"
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise ASRSetupError("asr_receipt_invalid")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ASRSetupError("asr_receipt_invalid") from error
        required = {
            "schema": 1,
            "release_id": RELEASE_ID,
            "notice_version": NOTICE_VERSION,
            "model_id": "mlx-whisper-small",
            "model_revision": self._model_revision,
            "runtime_python": "runtime/bin/python",
            "model_path": "model",
            "requirements_sha256": self._hash_file(self.requirements),
        }
        if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in required.items()):
            raise ASRSetupError("asr_receipt_invalid")
        manifest = receipt.get("model_manifest_sha256")
        if not isinstance(manifest, str) or len(manifest) != 64:
            raise ASRSetupError("asr_receipt_invalid")
        runtime_root = self.release / "runtime"
        runtime_bin = runtime_root / "bin"
        runtime_python = runtime_bin / "python"
        model = self.release / "model"
        if (
            runtime_root.is_symlink()
            or not runtime_root.is_dir()
            or runtime_bin.is_symlink()
            or not runtime_bin.is_dir()
            or runtime_python.is_symlink()
            or not runtime_python.is_file()
            or not os.access(runtime_python, os.X_OK)
        ):
            raise ASRSetupError("asr_runtime_invalid")
        observed = self._hash_model(model)
        if observed != manifest:
            raise ASRSetupError("asr_model_invalid")
        return runtime_python, LocalModelSnapshot(
            "mlx-whisper-small", self._model_revision, model, manifest
        )

    def _hash_model(self, model: Path) -> str:
        if model.is_symlink() or not model.is_dir():
            raise ASRSetupError("asr_model_invalid")
        digest = hashlib.sha256()
        for filename, expected_size, expected_hash in self._model_files:
            path = model / filename
            if path.is_symlink() or not path.is_file():
                raise ASRSetupError("asr_model_invalid")
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
                raise ASRSetupError("asr_model_invalid")
            file_digest = hashlib.sha256()
            with path.open("rb") as source:
                while block := source.read(_READ_SIZE):
                    file_digest.update(block)
            file_hash = file_digest.hexdigest()
            if file_hash != expected_hash:
                raise ASRSetupError("asr_model_invalid")
            digest.update(filename.encode())
            digest.update(b"\0")
            digest.update(str(expected_size).encode())
            digest.update(b"\0")
            digest.update(bytes.fromhex(file_hash))
        return digest.hexdigest()

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while block := source.read(_READ_SIZE):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _write_receipt(path: Path, value: dict[str, object]) -> None:
        with path.open("x", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(path, 0o600)

    def _remove_release(self) -> None:
        self._remove_tree(self.release, missing_ok=True)

    def _cleanup_staging(self) -> None:
        for candidate in self.root.iterdir():
            if not _STAGING_NAME.fullmatch(candidate.name):
                continue
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            self._remove_tree(candidate, missing_ok=True)

    def _remove_tree(self, path: Path, *, missing_ok: bool) -> None:
        if not path.exists() and not path.is_symlink():
            if missing_ok:
                return
            raise ASRSetupError("asr_release_missing")
        if path.is_symlink() or path.parent.resolve(strict=True) != self.root:
            raise ASRSetupError("asr_release_unsafe")
        try:
            shutil.rmtree(path)
        except OSError as error:
            raise ASRSetupError("asr_remove_failed") from error

    @staticmethod
    def _secure_directory(path: Path) -> Path:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise ASRSetupError("asr_storage_unsafe")
        os.chmod(path, 0o700)
        return path.resolve(strict=True)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        else:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


__all__ = [
    "ASRSetupError",
    "ASRSetupManager",
    "ASRSetupState",
    "ASRSetupStatus",
]
