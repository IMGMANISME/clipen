"""Global (cross-project), content-fingerprint-keyed cache for local
analysis results. `version_tag` should encode anything besides file
identity that should bust the cache -- a model name, a threshold, a frame
cap -- so two different settings on the same file get two independent,
both-valid cache entries instead of one clobbering the other."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from clipen.config import paths as clipen_paths

SCHEMA_VERSION = 1

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _safe_tag(tag: str) -> str:
    return _UNSAFE_CHARS.sub("_", tag)


def _cache_path(fingerprint: str, stage: str, version_tag: str) -> Path:
    directory = clipen_paths.analysis_cache_dir() / fingerprint
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{stage}.v{SCHEMA_VERSION}.{_safe_tag(version_tag)}.json"


def get_or_compute(
    fingerprint: str, stage: str, version_tag: str, compute: Callable[[], Any]
) -> Any:
    path = _cache_path(fingerprint, stage, version_tag)
    if path.exists():
        return json.loads(path.read_text())
    result = compute()
    path.write_text(json.dumps(result))
    return result


def cache_hit(fingerprint: str, stage: str, version_tag: str) -> bool:
    return _cache_path(fingerprint, stage, version_tag).exists()
