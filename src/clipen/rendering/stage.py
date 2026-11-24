"""What a render is doing right now, in words a person reads next to its progress bar.

Renders report progress as a number through many nested callbacks; the step
they are on is set here by the step itself, per thread, so a render running
in one thread never reads another's.
"""

from __future__ import annotations

import threading
from pathlib import Path

_local = threading.local()


def set_stage(text: str | None) -> None:
    _local.stage = text


def current_stage() -> str | None:
    return getattr(_local, "stage", None)


def stage_path(film: Path) -> Path:
    """The hidden file beside a film being rendered that names its current step."""
    return film.with_name(f".{film.stem}.stage")


def read_stage(film: Path) -> str | None:
    try:
        return stage_path(film).read_text(encoding="utf-8").strip()[:80] or None
    except OSError:
        return None
