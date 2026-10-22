"""Standard-library-only source bootstrap used before Clipen dependencies exist."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import shlex
import stat
import subprocess
import sys
from types import ModuleType
import uuid
import venv
from typing import Iterator, Mapping, Sequence, TextIO

from clipen.asr_release import (
    ASR_SOFTWARE_LICENSE,
    ASR_SOFTWARE_LICENSE_URL,
    DOWNLOAD_BYTES,
    INSTALLED_BYTES,
    MODEL_NAME,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    PROGRESS_RECORD_PREFIX,
    LICENSE_NOTICE,
    quick_release_is_ready,
    release_root,
)

try:
    import fcntl as _fcntl
except ImportError:  # Windows does not provide POSIX advisory file locks.
    _fcntl = None


_RUNTIME_SCHEMA = 1
_PIP_VERSION = "26.2.1"
_SETUPTOOLS_VERSION = "84.0.0"
_WHEEL_VERSION = "0.48.0"
_PROFILE_START = "# >>> Clipen managed PATH >>>"
_PROFILE_END = "# <<< Clipen managed PATH <<<"
_COMMAND_MARKER = "# Managed by Clipen; rerun ./install or clipen install to update."
_MANAGED_COMMAND_PREFIX = "# Managed by Clipen;"
_REMOVABLE_DIRECTORIES = (
    "runtime",  # Legacy source runtime.
    "runtimes",
    "asr",
    "models",
    "cache",
    "logs",
)
_REMOVABLE_FILES = (
    "setup.json",  # Legacy setup receipt.
    "install-receipt.json",
    ".controller.json",
    ".controller.lock",
)


class BootstrapError(RuntimeError):
    """The managed source runtime could not be prepared safely."""


def _require_posix_file_locking() -> ModuleType:
    if _fcntl is None:
        raise BootstrapError(
            "這個開發版本尚不支援 Windows；第一次設定需要 POSIX 檔案鎖"
        )
    return _fcntl


def clipen_data_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Clipen"
    xdg_data = os.environ.get("XDG_DATA_HOME")
    return Path(xdg_data) / "clipen" if xdg_data else Path.home() / ".local" / "share" / "clipen"


def runtime_fingerprint(repo_root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(f"schema={_RUNTIME_SCHEMA}\n".encode())
    digest.update(f"python={sys.version_info.major}.{sys.version_info.minor}\n".encode())
    digest.update(f"platform={platform.system()}-{platform.machine()}\n".encode())
    digest.update(f"repo={repo_root.resolve(strict=True)}\n".encode())
    digest.update(
        f"bootstrap-tools={_PIP_VERSION},{_SETUPTOOLS_VERSION},{_WHEEL_VERSION}\n".encode()
    )
    for relative in (
        "pyproject.toml",
        "requirements/runtime.txt",
        "requirements/asr-runtime.txt",
    ):
        path = repo_root / relative
        if not path.is_file() or path.is_symlink():
            raise BootstrapError(f"缺少必要的 runtime 定義：{relative}")
        digest.update(relative.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def runtime_python(runtime: Path) -> Path:
    return runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _receipt_path(runtime: Path) -> Path:
    return runtime / "clipen-runtime.json"


def runtime_is_ready(runtime: Path, fingerprint: str, repo_root: Path) -> bool:
    if runtime.is_symlink() or not runtime.is_dir():
        return False
    python = runtime_python(runtime)
    receipt_path = _receipt_path(runtime)
    if python.is_symlink() or not python.is_file() or not os.access(python, os.X_OK):
        return False
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return False
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(receipt, dict)
        and receipt.get("schema") == _RUNTIME_SCHEMA
        and receipt.get("fingerprint") == fingerprint
        and receipt.get("repo_root") == str(repo_root.resolve(strict=True))
    )


def _confirm_setup(
    runtime: Path,
    stdin: TextIO,
    stdout: TextIO,
    *,
    command_path: Path | None = None,
    profile_path: Path | None = None,
    asr_root: Path | None = None,
    include_runtime: bool = True,
) -> bool:
    print("Clipen 第一次設定" if include_runtime else "Clipen 需要完成設定", file=stdout)
    print("將會建立：", file=stdout)
    if include_runtime:
        print("  • Clipen 專用 Python 虛擬環境", file=stdout)
        print("  • 鎖定版本的本機 Controller 與安全驗證套件", file=stdout)
    if asr_root is not None:
        print("  • 隔離的本機語音辨識執行環境", file=stdout)
        print(f"  • 固定版本 {MODEL_NAME} 模型", file=stdout)
    if command_path is not None and profile_path is not None:
        print(f"  • `clipen` 指令：{command_path}", file=stdout)
        print(f"  • 將 Clipen 加入 PATH：{profile_path}", file=stdout)
    if include_runtime:
        print(f"Clipen runtime：{runtime}", file=stdout)
    if asr_root is not None:
        print(f"語音辨識：{asr_root}", file=stdout)
        print(
            f"語音辨識部分需下載約 {DOWNLOAD_BYTES / 1_000_000_000:.1f} GB，"
            f"安裝後約 {INSTALLED_BYTES / 1_000_000_000:.1f} GB。",
            file=stdout,
        )
        print(
            f"模型來源：{MODEL_REPOSITORY} @ {MODEL_REVISION[:8]}",
            file=stdout,
        )
        print("繁體中文轉換套件：PyPI 鎖定版本", file=stdout)
        print(
            f"MLX Speech 本機執行程式：{ASR_SOFTWARE_LICENSE}（{ASR_SOFTWARE_LICENSE_URL}）",
            file=stdout,
        )
        print(LICENSE_NOTICE, file=stdout)
        print("依網路速度可能需要數分鐘；安裝期間會持續顯示目前階段與經過時間。", file=stdout)
    print("不會修改系統 Python，也不會使用 sudo。", file=stdout)
    print("Codex 登入與 AI 模型呼叫不會在設定階段執行。", file=stdout)
    if not stdin.isatty():
        raise BootstrapError("第一次設定需要在互動式 Terminal 中確認。")
    stdout.write("開始完整設定？[Y/n] ")
    stdout.flush()
    answer = stdin.readline().strip().lower()
    return answer in {"", "y", "yes"}


def _command_script(repo_root: Path) -> str:
    launcher = repo_root.resolve(strict=True) / "clipen"
    if launcher.is_symlink() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise BootstrapError("Repo 內的 clipen launcher 不存在或不可執行")
    return f"#!/bin/sh\n{_COMMAND_MARKER}\nexec {shlex.quote(str(launcher))} \"$@\"\n"


def _shell_profile(home: Path, environment: Mapping[str, str]) -> Path:
    shell = Path(environment.get("SHELL", "")).name
    if shell == "zsh" or (not shell and sys.platform == "darwin"):
        return home / ".zshrc"
    if shell == "bash":
        return home / (".bash_profile" if sys.platform == "darwin" else ".bashrc")
    raise BootstrapError(
        "目前只能自動設定 zsh 或 bash；請將 Clipen 的 bin 目錄手動加入 PATH"
    )


def _profile_block(bin_root: Path) -> str:
    return (
        f"{_PROFILE_START}\n"
        f"export PATH={shlex.quote(str(bin_root))}:\"$PATH\"\n"
        f"{_PROFILE_END}"
    )


def _replace_profile_block(contents: str, block: str) -> str:
    if contents.count(_PROFILE_START) > 1 or contents.count(_PROFILE_END) > 1:
        raise BootstrapError("shell 設定中有重複的 Clipen PATH 區塊，請先保留並回報問題")
    start = contents.find(_PROFILE_START)
    end = contents.find(_PROFILE_END)
    if (start < 0) != (end < 0) or (start >= 0 and end < start):
        raise BootstrapError("shell 設定中的 Clipen PATH 區塊不完整，請先保留並回報問題")
    if start >= 0:
        end += len(_PROFILE_END)
        updated = contents[:start] + block + contents[end:]
    else:
        separator = "" if not contents or contents.endswith("\n") else "\n"
        updated = f"{contents}{separator}{block}\n"
    return updated


def _remove_profile_block(contents: str) -> str:
    if contents.count(_PROFILE_START) > 1 or contents.count(_PROFILE_END) > 1:
        raise BootstrapError("shell 設定中有重複的 Clipen PATH 區塊，未進行移除")
    start = contents.find(_PROFILE_START)
    end = contents.find(_PROFILE_END)
    if (start < 0) != (end < 0) or (start >= 0 and end < start):
        raise BootstrapError("shell 設定中的 Clipen PATH 區塊不完整，未進行移除")
    if start < 0:
        return contents
    end += len(_PROFILE_END)
    if end < len(contents) and contents[end] == "\n":
        end += 1
    elif start > 0 and contents[start - 1] == "\n":
        start -= 1
    return contents[:start] + contents[end:]


def _atomic_write(path: Path, contents: str, mode: int) -> None:
    staging = path.parent / f".{path.name}.clipen-{uuid.uuid4().hex}.tmp"
    try:
        with staging.open("x", encoding="utf-8") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        staging.chmod(mode)
        os.replace(staging, path)
    finally:
        if staging.exists() and not staging.is_symlink():
            staging.unlink()


def _install_command(
    repo_root: Path,
    data_root: Path,
    home: Path,
    environment: Mapping[str, str],
) -> tuple[Path, Path]:
    bin_root = data_root / "bin"
    if bin_root.exists() and (bin_root.is_symlink() or not bin_root.is_dir()):
        raise BootstrapError("Clipen bin 目錄不是安全的實體目錄")
    bin_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    command_path = bin_root / "clipen"
    script = _command_script(repo_root)
    if command_path.exists() or command_path.is_symlink():
        if command_path.is_symlink() or not command_path.is_file():
            raise BootstrapError("Clipen 指令位置已被不安全的檔案占用")
        try:
            existing_command = command_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise BootstrapError("無法安全讀取既有的 Clipen 指令") from error
        if _COMMAND_MARKER not in existing_command:
            raise BootstrapError("Clipen 指令位置已有其他程式，未進行覆寫")

    profile_path = _shell_profile(home, environment)
    if profile_path.exists() or profile_path.is_symlink():
        if profile_path.is_symlink() or not profile_path.is_file():
            raise BootstrapError("shell 設定檔不是安全的實體檔案，未進行修改")
        try:
            profile = profile_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise BootstrapError("無法安全讀取 shell 設定檔") from error
        profile_mode = stat.S_IMODE(profile_path.stat().st_mode)
    else:
        profile = ""
        profile_mode = 0o600
    updated_profile = _replace_profile_block(profile, _profile_block(bin_root))

    _atomic_write(command_path, script, 0o700)
    if updated_profile != profile:
        _atomic_write(profile_path, updated_profile, profile_mode)
    return command_path, profile_path


def _command_is_ready(
    repo_root: Path,
    data_root: Path,
    home: Path,
    environment: Mapping[str, str],
) -> bool:
    try:
        command_path = data_root / "bin" / "clipen"
        profile_path = _shell_profile(home, environment)
        return (
            not command_path.is_symlink()
            and command_path.is_file()
            and command_path.read_text(encoding="utf-8") == _command_script(repo_root)
            and not profile_path.is_symlink()
            and profile_path.is_file()
            and _profile_block(data_root / "bin") in profile_path.read_text(encoding="utf-8")
        )
    except (BootstrapError, OSError, UnicodeError):
        return False


@contextmanager
def _bootstrap_lock(runtime_root: Path) -> Iterator[None]:
    locking = _require_posix_file_locking()
    lock_path = runtime_root / ".bootstrap.lock"
    with lock_path.open("a+b") as lock:
        locking.flock(lock.fileno(), locking.LOCK_EX)
        try:
            yield
        finally:
            locking.flock(lock.fileno(), locking.LOCK_UN)


def _run_logged(command: Sequence[str], *, cwd: Path, log: TextIO) -> None:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=15 * 60,
        env={
            **os.environ,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONNOUSERSITE": "1",
        },
    )
    if completed.returncode != 0:
        raise BootstrapError(f"runtime 安裝命令失敗（結束代碼 {completed.returncode}）")


def _install_runtime(staging: Path, repo_root: Path, fingerprint: str, log_path: Path) -> None:
    venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(staging)
    python = runtime_python(staging)
    if not python.is_file():
        raise BootstrapError("無法建立 Clipen Python runtime")

    with log_path.open("w", encoding="utf-8") as log:
        _run_logged(
            (
                str(python),
                "-m",
                "pip",
                "install",
                f"pip=={_PIP_VERSION}",
                f"setuptools=={_SETUPTOOLS_VERSION}",
                f"wheel=={_WHEEL_VERSION}",
            ),
            cwd=repo_root,
            log=log,
        )
        _run_logged(
            (
                str(python),
                "-m",
                "pip",
                "install",
                "--requirement",
                str(repo_root / "requirements" / "runtime.txt"),
            ),
            cwd=repo_root,
            log=log,
        )
        _run_logged(
            (
                str(python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-build-isolation",
                "--editable",
                str(repo_root),
            ),
            cwd=repo_root,
            log=log,
        )

    receipt = {
        "schema": _RUNTIME_SCHEMA,
        "fingerprint": fingerprint,
        "repo_root": str(repo_root.resolve(strict=True)),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform": f"{platform.system()}-{platform.machine()}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt_path = _receipt_path(staging)
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    receipt_path.chmod(0o600)


def _install_asr_bundle(
    runtime: Path,
    repo_root: Path,
    data_root: Path,
    log_path: Path,
    stdout: TextIO,
) -> None:
    command = (
        str(runtime_python(runtime)),
        "-m",
        "clipen.analysis.setup_cli",
        "--data-root",
        str(data_root),
        "--requirements",
        str(repo_root / "requirements" / "asr-runtime.txt"),
    )
    captured = 0
    process: subprocess.Popen[str] | None = None
    progress_active = False
    progress_width = 0
    try:
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=repo_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env={
                    **os.environ,
                    "PYTHONNOUSERSITE": "1",
                    "CLIPEN_REPO_ROOT": str(repo_root),
                },
            )
            assert process.stdout is not None
            for line in process.stdout:
                captured += len(line.encode("utf-8"))
                if captured > 4 * 1024 * 1024:
                    process.terminate()
                    raise BootstrapError("語音辨識設定輸出超過安全上限")
                is_progress = line.startswith(PROGRESS_RECORD_PREFIX)
                visible_line = (
                    line[len(PROGRESS_RECORD_PREFIX) :].rstrip("\r\n")
                    if is_progress
                    else line
                )
                log.write(f"{visible_line}\n" if is_progress else visible_line)
                log.flush()
                if is_progress and stdout.isatty():
                    padding = " " * max(0, progress_width - len(visible_line))
                    stdout.write(f"\r{visible_line}{padding}")
                    progress_active = True
                    progress_width = len(visible_line)
                else:
                    if progress_active:
                        stdout.write("\n")
                        progress_active = False
                        progress_width = 0
                    stdout.write(f"{visible_line}\n" if is_progress else visible_line)
                stdout.flush()
            returncode = process.wait()
            if progress_active:
                stdout.write("\n")
                stdout.flush()
                progress_active = False
    except BaseException:
        if progress_active:
            stdout.write("\n")
            stdout.flush()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise
    if returncode != 0:
        raise BootstrapError(f"本機語音辨識設定失敗；診斷紀錄：{log_path}")


def ensure_runtime(
    repo_root: Path,
    *,
    data_root: Path | None = None,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    confirmed: bool = False,
) -> Path | None:
    repo_root = repo_root.resolve(strict=True)
    fingerprint = runtime_fingerprint(repo_root)
    root = (data_root or clipen_data_root()) / "runtimes"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise BootstrapError("Clipen runtime 根目錄不能是 symbolic link")
    runtime = root / fingerprint

    with _bootstrap_lock(root):
        if runtime_is_ready(runtime, fingerprint, repo_root):
            return runtime
        if runtime.exists():
            raise BootstrapError(
                f"Clipen runtime 不完整，請先保留此目錄並回報問題：{runtime}"
            )
        if not confirmed and not _confirm_setup(runtime, stdin, stdout):
            print("已取消，沒有建立 Clipen runtime。", file=stdout)
            return None

        staging = root / f".{fingerprint}.staging-{uuid.uuid4().hex}"
        log_root = (data_root or clipen_data_root()) / "logs"
        log_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_path = log_root / f"bootstrap-{fingerprint}.log"
        print("正在建立 Clipen 執行環境…", file=stdout)
        try:
            staging.mkdir(mode=0o700)
            _install_runtime(staging, repo_root, fingerprint, log_path)
            staging.rename(runtime)
        except Exception:
            if staging.exists() and not staging.is_symlink():
                shutil.rmtree(staging)
            raise
        print("✓ Clipen 執行環境建立完成", file=stdout)
        return runtime


def ensure_installation(
    repo_root: Path,
    *,
    data_root: Path | None = None,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
) -> Path | None:
    repo_root = repo_root.resolve(strict=True)
    selected_data_root = data_root or clipen_data_root()
    selected_home = home or Path.home()
    selected_environment = environment if environment is not None else os.environ
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise BootstrapError("目前完整設定只支援 Apple 晶片的 macOS")
    fingerprint = runtime_fingerprint(repo_root)
    runtime = selected_data_root / "runtimes" / fingerprint
    asr_requirements = repo_root / "requirements" / "asr-runtime.txt"
    runtime_ready = runtime_is_ready(runtime, fingerprint, repo_root)
    asr_ready = quick_release_is_ready(selected_data_root, asr_requirements)
    command_ready = _command_is_ready(
        repo_root, selected_data_root, selected_home, selected_environment
    )

    if not runtime_ready or not asr_ready:
        command_path = selected_data_root / "bin" / "clipen"
        profile_path = _shell_profile(selected_home, selected_environment)
        if not _confirm_setup(
            runtime,
            stdin,
            stdout,
            command_path=None if command_ready else command_path,
            profile_path=None if command_ready else profile_path,
            asr_root=None if asr_ready else release_root(selected_data_root),
            include_runtime=not runtime_ready,
        ):
            print("已取消，既有 Clipen 資料沒有變動。", file=stdout)
            return None
        if not runtime_ready:
            installed_runtime = ensure_runtime(
                repo_root,
                data_root=selected_data_root,
                stdin=stdin,
                stdout=stdout,
                confirmed=True,
            )
            if installed_runtime is None:
                return None
            runtime = installed_runtime
        if not asr_ready:
            log_root = selected_data_root / "logs"
            log_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if log_root.is_symlink():
                raise BootstrapError("Clipen log 目錄不能是 symbolic link")
            asr_log = log_root / f"bootstrap-asr-{fingerprint}.log"
            print("正在準備本機語音辨識與固定模型…", file=stdout)
            _install_asr_bundle(
                runtime,
                repo_root,
                selected_data_root,
                asr_log,
                stdout,
            )
            if not quick_release_is_ready(selected_data_root, asr_requirements):
                raise BootstrapError(f"語音辨識安裝未通過啟動檢查；診斷紀錄：{asr_log}")
        if not command_ready:
            command_path, profile_path = _install_command(
                repo_root, selected_data_root, selected_home, selected_environment
            )
            print(f"✓ 已建立指令：{command_path}", file=stdout)
            print(f"✓ 已更新 PATH：{profile_path}", file=stdout)
            print("下次開啟 Terminal 後可直接輸入：clipen codex（或 clipen claude）", file=stdout)
        print("✓ Clipen 完整設定完成", file=stdout)
        return runtime

    if not command_ready:
        if not stdin.isatty():
            return runtime
        print("Clipen 需要完成一次指令設定。", file=stdout)
        print(f"將建立：{selected_data_root / 'bin' / 'clipen'}", file=stdout)
        profile_path = _shell_profile(selected_home, selected_environment)
        print(f"將更新 PATH：{profile_path}", file=stdout)
        stdout.write("完成設定？[Y/n] ")
        stdout.flush()
        if stdin.readline().strip().lower() not in {"", "y", "yes"}:
            print("已略過；可再次執行 ./install 完成指令設定。", file=stdout)
            return runtime
        command_path, profile_path = _install_command(
            repo_root, selected_data_root, selected_home, selected_environment
        )
        print(f"✓ 已建立指令：{command_path}", file=stdout)
        print(f"✓ 已更新 PATH：{profile_path}", file=stdout)
        print("下次開啟 Terminal 後可直接輸入：clipen codex（或 clipen claude）", file=stdout)
    return runtime


def completed_runtime(repo_root: Path, *, data_root: Path | None = None) -> Path:
    """Return the installed runtime without performing setup or downloads."""

    repo_root = repo_root.resolve(strict=True)
    selected_data_root = data_root or clipen_data_root()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise BootstrapError("目前只支援 Apple 晶片的 macOS")
    fingerprint = runtime_fingerprint(repo_root)
    runtime = selected_data_root / "runtimes" / fingerprint
    requirements = repo_root / "requirements" / "asr-runtime.txt"
    if not runtime_is_ready(runtime, fingerprint, repo_root) or not quick_release_is_ready(
        selected_data_root, requirements
    ):
        raise BootstrapError(
            "Clipen 尚未完成安裝；第一次請在 Repo 執行 ./install，"
            "已有 clipen 指令時可執行 clipen install 修復"
        )
    return runtime


def _controller_is_running(data_root: Path) -> bool:
    locking = _require_posix_file_locking()
    lock_path = data_root / ".controller.lock"
    if not lock_path.exists() and not lock_path.is_symlink():
        return False
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags)
    except OSError as error:
        raise BootstrapError("無法安全檢查 Clipen 是否仍在執行") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BootstrapError("Clipen 執行鎖不是安全的實體檔案")
        try:
            locking.flock(descriptor, locking.LOCK_EX | locking.LOCK_NB)
        except BlockingIOError:
            return True
        locking.flock(descriptor, locking.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _validate_removable_path(path: Path, data_root: Path, *, directory: bool) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.parent.resolve(strict=True) != data_root:
        raise BootstrapError(f"拒絕移除不安全的 Clipen 路徑：{path}")
    if directory:
        if not path.is_dir() or path.is_mount():
            raise BootstrapError(f"拒絕移除非一般目錄：{path}")
        for current, directories, _files in os.walk(path, followlinks=False):
            for name in directories:
                candidate = Path(current) / name
                if not candidate.is_symlink() and candidate.is_mount():
                    raise BootstrapError(f"拒絕移除包含 mount point 的目錄：{candidate}")
    elif not path.is_file():
        raise BootstrapError(f"拒絕移除非一般檔案：{path}")


def uninstall_installation(
    *,
    data_root: Path | None = None,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
) -> bool:
    """Remove Clipen-managed installation files while preserving all projects."""

    selected_data_root = data_root or clipen_data_root()
    selected_home = home or Path.home()
    selected_environment = environment if environment is not None else os.environ
    profile_path = _shell_profile(selected_home, selected_environment)
    bin_root = selected_data_root / "bin"
    command_path = bin_root / "clipen"

    print("移除 Clipen", file=stdout)
    print("將會移除：", file=stdout)
    print("  • Clipen runtime 與本機語音辨識模型", file=stdout)
    print(f"  • `clipen` 指令：{command_path}", file=stdout)
    print(f"  • PATH 設定：{profile_path}", file=stdout)
    print("會保留 Clipen 作品資料、毛片、參考作品與匯出檔案。", file=stdout)
    if not stdin.isatty():
        raise BootstrapError("移除 Clipen 需要在互動式 Terminal 中確認")
    stdout.write("確定移除？[y/N] ")
    stdout.flush()
    if stdin.readline().strip().lower() not in {"y", "yes"}:
        print("已取消，沒有移除任何內容。", file=stdout)
        return False

    if selected_data_root.exists() or selected_data_root.is_symlink():
        if selected_data_root.is_symlink() or not selected_data_root.is_dir():
            raise BootstrapError("Clipen 資料根目錄不安全，未進行移除")
        resolved_data_root = selected_data_root.resolve(strict=True)
        if _controller_is_running(resolved_data_root):
            raise BootstrapError("Clipen 仍在執行；請先在啟動它的 Terminal 按 Ctrl+C")
    else:
        resolved_data_root = selected_data_root

    profile = ""
    updated_profile = ""
    profile_mode = 0o600
    if profile_path.exists() or profile_path.is_symlink():
        if profile_path.is_symlink() or not profile_path.is_file():
            raise BootstrapError("shell 設定檔不是安全的實體檔案，未進行移除")
        profile = profile_path.read_text(encoding="utf-8")
        updated_profile = _remove_profile_block(profile)
        profile_mode = stat.S_IMODE(profile_path.stat().st_mode)

    if bin_root.exists() or bin_root.is_symlink():
        if bin_root.is_symlink() or not bin_root.is_dir():
            raise BootstrapError("Clipen bin 目錄不安全，未進行移除")
    if command_path.exists() or command_path.is_symlink():
        if command_path.is_symlink() or not command_path.is_file():
            raise BootstrapError("clipen 指令位置不是安全的實體檔案，未進行移除")
        command = command_path.read_text(encoding="utf-8")
        if not any(
            line.startswith(_MANAGED_COMMAND_PREFIX)
            for line in command.splitlines()[:3]
        ):
            raise BootstrapError("clipen 指令不是由 Clipen 管理，未進行移除")

    if selected_data_root.exists():
        for name in _REMOVABLE_DIRECTORIES:
            _validate_removable_path(
                resolved_data_root / name, resolved_data_root, directory=True
            )
        for name in _REMOVABLE_FILES:
            _validate_removable_path(
                resolved_data_root / name, resolved_data_root, directory=False
            )

    if selected_data_root.exists():
        for name in _REMOVABLE_DIRECTORIES:
            path = resolved_data_root / name
            if path.exists():
                shutil.rmtree(path)
        for name in _REMOVABLE_FILES:
            path = resolved_data_root / name
            if path.exists():
                path.unlink()
        bin_root = resolved_data_root / "bin"
        if command_path.exists():
            command_path.unlink()
        if bin_root.is_dir() and not bin_root.is_symlink() and not any(bin_root.iterdir()):
            bin_root.rmdir()
    if updated_profile != profile:
        _atomic_write(profile_path, updated_profile, profile_mode)
    print("✓ Clipen 已移除", file=stdout)
    print("作品資料與所有毛片均已保留。", file=stdout)
    return True


def _print_usage(stream: TextIO) -> None:
    print("第一次安裝：./install", file=stream)
    print("啟動 Clipen：clipen codex 或 clipen claude", file=stream)
    print("移除 Clipen：./uninstall", file=stream)


def main(repo_root: Path, arguments: Sequence[str]) -> int:
    if list(arguments) in (["--help"], ["-h"]):
        _print_usage(sys.stdout)
        return 0
    from clipen.provider_registry import PROVIDERS, implemented_providers

    launch = len(arguments) == 1 and arguments[0] in implemented_providers()
    if list(arguments) not in (["install"], ["uninstall"]) and not launch:
        if len(arguments) == 1 and arguments[0] in PROVIDERS:
            print(f"Clipen 目前還不支援 {PROVIDERS[arguments[0]].display_name}（{arguments[0]}）。", file=sys.stderr)
        _print_usage(sys.stderr)
        return 2
    try:
        _require_posix_file_locking()
        if list(arguments) == ["install"]:
            runtime = ensure_installation(repo_root)
            if runtime is not None:
                print("✓ Clipen 已安裝完成")
                print("\n接下來：")
                print("1. 開啟新的 Terminal")
                print("2. cd 到你想存放 Clipen 作品資料的目錄")
                print("3. 執行：clipen codex（或 clipen claude）")
                print("\n不需要回到 Clipen 程式碼目錄；毛片也可以放在其他位置。")
            return 0
        if list(arguments) == ["uninstall"]:
            uninstall_installation()
            return 0
        runtime = completed_runtime(repo_root)
    except KeyboardInterrupt:
        action = "移除" if list(arguments) == ["uninstall"] else "設定"
        print(f"\nClipen {action}已取消", file=sys.stderr)
        return 130
    except (BootstrapError, OSError, subprocess.SubprocessError) as error:
        action = {
            "install": "安裝",
            "uninstall": "移除",
        }.get(list(arguments)[0], "啟動")
        print(f"Clipen {action}失敗：{error}", file=sys.stderr)
        return 1
    if runtime is None:
        return 0

    python = runtime_python(runtime)
    environment = {
        **os.environ,
        "CLIPEN_REPO_ROOT": str(repo_root.resolve(strict=True)),
        "PYTHONNOUSERSITE": "1",
    }
    os.execve(
        python,
        [str(python), "-m", "clipen.cli", arguments[0]],
        environment,
    )
    return 1
