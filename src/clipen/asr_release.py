"""Standard-library-only release contract shared by bootstrap and analysis."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat


NOTICE_VERSION = "asr-mlx-whisper-small-v1"
RELEASE_ID = "mlx-whisper-small-v1"
MODEL_REVISION = "eb52dbc58f50f19eb8c87b54b7c621633c67b7e0"
MODEL_REPOSITORY = "mlx-community/whisper-small-mlx"
MODEL_FILES = (
    (
        "config.json",
        266,
        "e8f58e638208af66d5d5d67801259dc7a12d199e971967a9f9d33a8e3635668e",
    ),
    (
        "weights.npz",
        481_307_592,
        "55b6674c9b339702d486e2b1573839a66f8ec8f821ed2886993ef717a86b09f5",
    ),
)
DOWNLOAD_BYTES = 1_200_000_000
INSTALLED_BYTES = 1_700_000_000
WHISPER_LICENSE = "MIT"
WHISPER_LICENSE_URL = "https://github.com/openai/whisper/blob/main/LICENSE"
CONVERSION_NOTICE = (
    "MLX 轉換模型頁未另外標示 license；Clipen 會從固定來源下載，"
    "不會把模型檔收進 Repo。"
)
PROGRESS_RECORD_PREFIX = "CLIPEN_PROGRESS\t"


def release_root(data_root: Path) -> Path:
    return Path(data_root) / "asr" / RELEASE_ID


def requirements_digest(requirements: Path) -> str:
    digest = hashlib.sha256()
    with Path(requirements).open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def quick_release_is_ready(data_root: Path, requirements: Path) -> bool:
    """Cheap bootstrap check; the controller performs the full content hash."""

    release = release_root(data_root)
    receipt_path = release / "receipt.json"
    if release.is_symlink() or not release.is_dir():
        return False
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return False
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        required = {
            "schema": 1,
            "release_id": RELEASE_ID,
            "notice_version": NOTICE_VERSION,
            "model_id": "mlx-whisper-small",
            "model_revision": MODEL_REVISION,
            "requirements_sha256": requirements_digest(requirements),
            "runtime_python": "runtime/bin/python",
            "model_path": "model",
        }
        if not isinstance(receipt, dict) or any(
            receipt.get(key) != value for key, value in required.items()
        ):
            return False
        manifest = receipt.get("model_manifest_sha256")
        if not isinstance(manifest, str) or len(manifest) != 64:
            return False
        runtime_root = release / "runtime"
        runtime_bin = runtime_root / "bin"
        model_root = release / "model"
        if (
            runtime_root.is_symlink()
            or not runtime_root.is_dir()
            or runtime_bin.is_symlink()
            or not runtime_bin.is_dir()
            or model_root.is_symlink()
            or not model_root.is_dir()
        ):
            return False
        python = release / "runtime" / "bin" / "python"
        if python.is_symlink() or not python.is_file() or not os.access(python, os.X_OK):
            return False
        for filename, size, _digest in MODEL_FILES:
            path = release / "model" / filename
            info = os.lstat(path)
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size != size:
                return False
        return True
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


__all__ = [
    "CONVERSION_NOTICE",
    "DOWNLOAD_BYTES",
    "INSTALLED_BYTES",
    "MODEL_FILES",
    "MODEL_REPOSITORY",
    "MODEL_REVISION",
    "NOTICE_VERSION",
    "PROGRESS_RECORD_PREFIX",
    "RELEASE_ID",
    "WHISPER_LICENSE",
    "WHISPER_LICENSE_URL",
    "quick_release_is_ready",
    "release_root",
    "requirements_digest",
]
