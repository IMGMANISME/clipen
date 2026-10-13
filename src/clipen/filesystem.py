"""Filesystem helpers that also work on user-selected non-POSIX volumes."""

from __future__ import annotations

import os
from pathlib import Path


def restrict_permissions(path: Path, mode: int) -> None:
    """Apply POSIX privacy bits, or verify access when the volume lacks them."""

    try:
        os.chmod(path, mode)
        return
    except OSError:
        pass
    if path.is_symlink() or not path.exists():
        raise OSError(f"unsafe managed path: {path}")
    required = os.R_OK
    if mode & 0o200:
        required |= os.W_OK
    if path.is_dir():
        required |= os.X_OK
    if not os.access(path, required):
        raise OSError(f"managed path permissions unavailable: {path}")
