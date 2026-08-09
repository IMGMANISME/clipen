"""Cheap content-identity hash for large video files: path + size + mtime,
not a full read of the file. Good enough to invalidate a cache entry when
a file actually changes, without hashing gigabytes of footage on every
run."""

from __future__ import annotations

import hashlib
from pathlib import Path


def fingerprint(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    return hashlib.sha256(raw.encode()).hexdigest()
