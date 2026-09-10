"""Standard-library-only release contract shared by bootstrap and analysis."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat


NOTICE_VERSION = "asr-qwen3-1.7b-confidence-v1"
RELEASE_ID = "qwen3-asr-1.7b-confidence-v1"
SUPERSEDED_RELEASE_IDS = (
    "mlx-whisper-small-v1",
    "mlx-whisper-large-v3-turbo-v1",
    "funasr-nano-consensus-v1",
)
MODEL_ID = "qwen3-asr-1.7b-int8"
MODEL_NAME = "Qwen3-ASR 1.7B（Int8）"
MODEL_REVISION = "a27b3ef0cd9c8115e13baa55b1be8992b3589756"
MODEL_REPOSITORY = "appautomaton/qwen3-asr-1.7b-int8-mlx"
MODEL_FILES = (
    ("chat_template.json", 1_161, "75a8cfca24f00de72d796fbfed6858fc9614ef3dabd8696684cc3bc03a9c58ff"),
    ("config.json", 5_849, "5e35ceadb80a954a4790152ab45a91366d2a5c84ae6e42a3d3efb810df3b3374"),
    ("generation_config.json", 142, "1da527824d81e07118facff437e03f2e24a23311e3bdeb2368973fe77e5f275c"),
    ("merges.txt", 1_671_853, "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5"),
    ("model.safetensors", 2_506_670_749, "8a9aca31c5715d080f7d891dbac08146aeddf8c34cd53e46cf24d665dcd33786"),
    ("preprocessor_config.json", 330, "45e120a4eda2c20c5d7f2ea9354e63536bf35e27aa573fb7cdf78017b378770d"),
    ("tokenizer_config.json", 12_487, "4942d005604266809309cabc9f4e9cb89ce855d59b14681fdc0e1cc62ea26c4c"),
    ("vocab.json", 2_776_833, "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
)
MODEL_DOWNLOADS = tuple(
    (
        filename,
        f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{filename}",
        size,
        digest,
    )
    for filename, size, digest in MODEL_FILES
)
DOWNLOAD_BYTES = sum(item[1] for item in MODEL_FILES) + 260_000_000
INSTALLED_BYTES = sum(item[1] for item in MODEL_FILES) + 420_000_000
ASR_SOFTWARE_LICENSE = "MIT"
ASR_SOFTWARE_LICENSE_URL = "https://github.com/appautomaton/mlx-speech/blob/main/LICENSE"
MODEL_LICENSE = "Apache-2.0"
MODEL_LICENSE_URL = f"https://huggingface.co/{MODEL_REPOSITORY}"
LICENSE_NOTICE = (
    "Qwen3-ASR 模型採 Apache-2.0，MLX Speech 執行程式採 MIT；"
    "Clipen 只下載固定版本，不會把模型檔收進 Repo。"
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
            "schema": 3,
            "release_id": RELEASE_ID,
            "notice_version": NOTICE_VERSION,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "requirements_sha256": requirements_digest(requirements),
            "runtime_python": "runtime/bin/python",
            "model_path": "model",
        }
        if not isinstance(receipt, dict) or any(
            receipt.get(key) != value for key, value in required.items()
        ):
            return False
        if set(receipt) != {*required, "model_manifest_sha256"}:
            return False
        manifest = receipt.get("model_manifest_sha256")
        if not isinstance(manifest, str) or len(manifest) != 64:
            return False
        runtime_root = release / "runtime"
        runtime_bin = runtime_root / "bin"
        model_root = release / "model"
        if any(
            path.is_symlink() or not path.is_dir()
            for path in (runtime_root, runtime_bin, model_root)
        ):
            return False
        python = runtime_bin / "python"
        if python.is_symlink() or not python.is_file() or not os.access(python, os.X_OK):
            return False
        for filename, size, _digest in MODEL_FILES:
            path = model_root / filename
            info = os.lstat(path)
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size != size:
                return False
        return True
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


__all__ = [
    "ASR_SOFTWARE_LICENSE",
    "ASR_SOFTWARE_LICENSE_URL",
    "DOWNLOAD_BYTES",
    "INSTALLED_BYTES",
    "LICENSE_NOTICE",
    "MODEL_DOWNLOADS",
    "MODEL_FILES",
    "MODEL_ID",
    "MODEL_LICENSE",
    "MODEL_LICENSE_URL",
    "MODEL_NAME",
    "MODEL_REPOSITORY",
    "MODEL_REVISION",
    "NOTICE_VERSION",
    "PROGRESS_RECORD_PREFIX",
    "RELEASE_ID",
    "SUPERSEDED_RELEASE_IDS",
    "quick_release_is_ready",
    "release_root",
    "requirements_digest",
]
