from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EditDecision:
    source_path: Path
    start: float
    end: float
    order: int
    transition_in: str | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start
