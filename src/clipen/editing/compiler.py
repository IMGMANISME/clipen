"""Output settings stored with an edit timeline."""

from __future__ import annotations

from dataclasses import dataclass

_PRESETS = {
    "landscape_1080p": (1920, 1080),
    "portrait_1080p": (1080, 1920),
}
_FRAME_RATES = frozenset((24, 25, 30, 60))


@dataclass(frozen=True, slots=True)
class OutputSettings:
    preset: str
    frame_rate: int

    def __post_init__(self) -> None:
        if not isinstance(self.preset, str) or self.preset not in _PRESETS:
            raise ValueError("output preset is invalid")
        if (
            isinstance(self.frame_rate, bool)
            or not isinstance(self.frame_rate, int)
            or self.frame_rate not in _FRAME_RATES
        ):
            raise ValueError("output frame rate is invalid")

    @property
    def canvas(self) -> tuple[int, int]:
        return _PRESETS[self.preset]

    def to_payload(self) -> dict[str, object]:
        width, height = self.canvas
        return {
            "preset": self.preset,
            "frame_rate": self.frame_rate,
            "width": width,
            "height": height,
        }
