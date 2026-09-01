"""Standard-library-only source bootstrap used before Clipen dependencies exist."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
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
import uuid
import venv
from typing import Iterator, Mapping, Sequence, TextIO


_RUNTIME_SCHEMA = 1
_PIP_VERSION = "26.2.1"
_SETUPTOOLS_VERSION = "84.0.0"
_WHEEL_VERSION = "0.48.0"
_PROFILE_START = "# >>> Clipen managed PATH >>>"
_PROFILE_END = "# <<< Clipen managed PATH <<<"
_COMMAND_MARKER = "# Managed by Clipen; rerun ./clipen to update."


class BootstrapError(RuntimeError):
    """The managed source runtime could not be prepared safely."""


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
    for relative in ("pyproject.toml", "requirements/runtime.txt"):
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
) -> bool:
    print("Clipen 第一次設定", file=stdout)
    print("將會建立：", file=stdout)
    print("  • Clipen 專用 Python 虛擬環境", file=stdout)
    print("  • 鎖定版本的本機 Controller 與安全驗證套件", file=stdout)
    if command_path is not None and profile_path is not None:
        print(f"  • `clipen` 指令：{command_path}", file=stdout)
        print(f"  • 將 Clipen 加入 PATH：{profile_path}", file=stdout)
    print(f"安裝位置：{runtime}", file=stdout)
    print("不會修改系統 Python，也不會使用 sudo。", file=stdout)
    print("Codex 登入與模型呼叫不會在設定階段執行。", file=stdout)
    if not stdin.isatty():
        raise BootstrapError("第一次設定需要在互動式 Terminal 中確認。")
    stdout.write("開始建立環境？[Y/n] ")
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
    lock_path = runtime_root / ".bootstrap.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


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
    fingerprint = runtime_fingerprint(repo_root)
    runtime = selected_data_root / "runtimes" / fingerprint
    runtime_ready = runtime_is_ready(runtime, fingerprint, repo_root)
    command_ready = _command_is_ready(
        repo_root, selected_data_root, selected_home, selected_environment
    )

    if not runtime_ready:
        command_path = selected_data_root / "bin" / "clipen"
        profile_path = _shell_profile(selected_home, selected_environment)
        if not _confirm_setup(
            runtime,
            stdin,
            stdout,
            command_path=command_path,
            profile_path=profile_path,
        ):
            print("已取消，沒有建立 Clipen runtime。", file=stdout)
            return None
        installed_runtime = ensure_runtime(
            repo_root,
            data_root=selected_data_root,
            stdin=stdin,
            stdout=stdout,
            confirmed=True,
        )
        if installed_runtime is None:
            return None
        command_path, profile_path = _install_command(
            repo_root, selected_data_root, selected_home, selected_environment
        )
        print(f"✓ 已建立指令：{command_path}", file=stdout)
        print(f"✓ 已更新 PATH：{profile_path}", file=stdout)
        print("下次開啟 Terminal 後可直接輸入：clipen codex", file=stdout)
        return installed_runtime

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
            print("已略過；目前仍可使用 ./clipen codex。", file=stdout)
            return runtime
        command_path, profile_path = _install_command(
            repo_root, selected_data_root, selected_home, selected_environment
        )
        print(f"✓ 已建立指令：{command_path}", file=stdout)
        print(f"✓ 已更新 PATH：{profile_path}", file=stdout)
        print("下次開啟 Terminal 後可直接輸入：clipen codex", file=stdout)
    return runtime


def main(repo_root: Path, arguments: Sequence[str]) -> int:
    if list(arguments) in (["--help"], ["-h"]):
        print("用法：clipen codex")
        return 0
    if list(arguments) != ["codex"]:
        print("用法：clipen codex", file=sys.stderr)
        return 2
    try:
        runtime = ensure_installation(repo_root)
    except (BootstrapError, OSError, subprocess.SubprocessError) as error:
        print(f"Clipen 設定失敗：{error}", file=sys.stderr)
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
        [str(python), "-m", "clipen.cli", "codex"],
        environment,
    )
    return 1
