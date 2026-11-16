"""Cancellable installation of the fixed local alignment runtime, without source uploads."""

import hashlib
import json
import os
import platform
import shutil
import sys
import time
import urllib.request
import uuid
import venv
from pathlib import Path

from clipen.projects import ProjectError

from .alignment import FILES, MODEL, REVISION

HASHES = {
    "chat_template.json": "75a8cfca24f00de72d796fbfed6858fc9614ef3dabd8696684cc3bc03a9c58ff",
    "config.json": "8dc126e1a4ac001acb02f3a9ca6277c8a20a25485fde293bc99eeab3aedc604d",
    "generation_config.json": "948d089b23bca1d214e768d59c4438365665f52ec6d33678f4062206b3fbbb8c",
    "merges.txt": "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5",
    "model.safetensors": "be19ef8ac4326d032e7673342930b14c2df30bd68c1632493b0f563e30829f91",
    "model.safetensors.index.json": "766eb50873855eeb4d95ab92def358cfd173bfc8c7aabd8635ababa1edf6db79",
    "preprocessor_config.json": "45e120a4eda2c20c5d7f2ea9354e63536bf35e27aa573fb7cdf78017b378770d",
    "tokenizer_config.json": "3ab80063f8511deb9566e6ad438d17b7a6277fcffd52d92854112f19d36bd81c",
    "vocab.json": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
}
RECEIPT = {"model": MODEL, "revision": REVISION, "runtime": "mlx-audio-0.6.0"}


def root_for(data_root):
    return Path(data_root) / "alignment" / "qwen3-0.6b-8bit-v1"


def verified_runtime(root):
    root = Path(root)
    receipt = root / "ready.json"
    model = root / "model"
    python = root / "runtime/bin/python"
    if (
        any(p.is_symlink() for p in (root.parent, root, model, root / "runtime", root / "runtime/bin", receipt))
        or not receipt.is_file()
        or receipt.stat().st_size > 4096
        or not python.is_file()
    ):
        return None
    try:
        if json.loads(receipt.read_text()) != RECEIPT:
            return None
        for name, size in FILES.items():
            path = model / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                return None
            value = hashlib.sha256()
            with path.open("rb") as source:
                while block := source.read(1024 * 1024):
                    value.update(block)
            if value.hexdigest() != HASHES[name]:
                return None
        return python, model
    except (OSError, ValueError):
        return None


def install_runtime(data_root, cancel, progress=lambda _: None):
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ProjectError("alignment_platform_unsupported", "本機對齊目前支援 Apple Silicon Mac。", status_code=409)
    if data_root is None:
        raise ProjectError("alignment_unavailable", "本機對齊目錄尚未設定。", status_code=409)
    import fcntl

    from clipen.finishing.audio import run_process

    from .production_media import atomic_json

    root = root_for(data_root)
    if root.parent.is_symlink() or root.is_symlink():
        raise ProjectError("storage_unsafe", "模型安裝路徑不安全。", status_code=409)
    root.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
    lock_path = root.parent / "setup.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    staging = None
    try:
        deadline = time.monotonic() + 1800
        while True:
            if cancel.is_set():
                raise ProjectError("user_cancelled", "已取消模型準備。", status_code=409)
            if time.monotonic() > deadline:
                raise ProjectError("alignment_setup_timeout", "模型準備逾時。", status_code=409)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                cancel.wait(0.2)
        if verified_runtime(root):
            return {"ready": True, "model": MODEL, "revision": REVISION, "reused": True}
        if root.exists():
            # An invalid prior environment is preserved for diagnosis. Never
            # silently delete an existing user installation during a retry.
            raise ProjectError(
                "alignment_install_invalid", "現有對齊環境未通過驗證，請修復該本機環境。", status_code=409
            )
        if shutil.disk_usage(root.parent).free < sum(FILES.values()) + 1024**3:
            raise ProjectError("alignment_disk_space", "準備對齊模型需要額外約 2.3 GB 可用空間。", status_code=409)
        staging = root.parent / (".setup-" + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        (staging / "model").mkdir(mode=0o700)
        progress({"phase": "runtime"})
        venv.EnvBuilder(with_pip=True).create(staging / "runtime")
        run_process(
            [
                str(staging / "runtime/bin/python"),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "mlx-audio==0.6.0",
            ],
            cancel,
            timeout=900,
        )
        total = sum(FILES.values())
        completed = 0
        for name, size in FILES.items():
            path = staging / "model" / name
            request = urllib.request.Request(
                f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}",
                headers={"User-Agent": "Clipen-local-alignment/1"},
            )
            value = hashlib.sha256()
            count = 0
            with urllib.request.urlopen(request, timeout=30) as response, path.open("xb") as output:
                while block := response.read(1024 * 1024):
                    if cancel.is_set():
                        raise ProjectError("user_cancelled", "已取消模型下載。", status_code=409)
                    if time.monotonic() > deadline:
                        raise ProjectError("alignment_setup_timeout", "模型下載逾時。", status_code=409)
                    count += len(block)
                    if count > size:
                        raise ValueError("alignment download exceeds pinned size")
                    output.write(block)
                    value.update(block)
                    progress({"phase": "model", "completed_bytes": completed + count, "total_bytes": total})
            if count != size or value.hexdigest() != HASHES[name]:
                raise ProjectError("alignment_download_invalid", "模型下載未通過固定版本校驗。", status_code=409)
            completed += size
        atomic_json(staging / "ready.json", RECEIPT)
        os.rename(staging, root)
        staging = None
        return {"ready": True, "model": MODEL, "revision": REVISION, "reused": False}
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        os.close(fd)
