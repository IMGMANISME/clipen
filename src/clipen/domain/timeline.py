"""Exact, provider-independent timeline values used by every exporter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
import re
from typing import Mapping, TypeAlias

from .media_time import Rational, SourceSpan
from .validation import require_identifier


_EFFECT_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FRAME = 2**63 - 1
_MAX_TRACKS = 256
_MAX_ITEMS_PER_TRACK = 100_000
_MAX_MARKERS = 100_000
_MAX_EFFECTS_PER_CLIP = 128


def _positive_integer(value: int, field: str, maximum: int = _MAX_FRAME) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"{field} must be a positive integer")


def _non_negative_integer(value: int, field: str, maximum: int = _MAX_FRAME) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise ValueError(f"{field} must be a non-negative integer")


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > 8:
        raise ValueError("effect parameters are nested too deeply")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if not -(2**63) <= value <= 2**63 - 1:
            raise ValueError("effect integer is outside supported bounds")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("effect parameters must contain finite numbers")
        return
    if isinstance(value, Mapping):
        if len(value) > 128 or any(not isinstance(key, str) for key in value):
            raise ValueError("effect object keys are invalid")
        for child in value.values():
            _validate_json_value(child, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 1_000:
            raise ValueError("effect parameter list is too large")
        for child in value:
            _validate_json_value(child, depth=depth + 1)
        return
    raise ValueError("effect parameters must contain JSON values")


@dataclass(frozen=True)
class ClipSource:
    """A selected source span and the available trim handles around it."""

    span: SourceSpan
    handle_before_pts: int = 0
    handle_after_pts: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.span, SourceSpan):
            raise ValueError("source range requires a validated source span")
        _non_negative_integer(self.handle_before_pts, "handle_before_pts")
        _non_negative_integer(self.handle_after_pts, "handle_after_pts")
        if (
            int(self.span.start_pts) - self.handle_before_pts < -(2**63)
            or int(self.span.end_pts) + self.handle_after_pts > _MAX_FRAME
        ):
            raise ValueError("source handles are outside supported PTS bounds")

    def to_payload(self) -> dict[str, object]:
        return {
            **self.span.to_storage(),
            "handle_before_pts": str(self.handle_before_pts),
            "handle_after_pts": str(self.handle_after_pts),
        }


@dataclass(frozen=True, init=False)
class EffectRequest:
    """A semantic effect request; exporters decide whether they can map it."""

    effect_id: str
    _parameters_json: str

    def __init__(self, effect_id: str, parameters: Mapping[str, object]) -> None:
        if not isinstance(effect_id, str) or _EFFECT_ID.fullmatch(effect_id) is None:
            raise ValueError("effect_id is invalid")
        if not isinstance(parameters, Mapping):
            raise ValueError("effect parameters must be an object")
        _validate_json_value(parameters)
        try:
            encoded = json.dumps(
                parameters,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("effect parameters must be finite JSON values") from error
        if len(encoded.encode("utf-8")) > 16_384:
            raise ValueError("effect parameters are too large")
        object.__setattr__(self, "effect_id", effect_id)
        object.__setattr__(self, "_parameters_json", encoded)

    @property
    def parameters(self) -> Mapping[str, object]:
        return json.loads(self._parameters_json)

    def to_payload(self) -> dict[str, object]:
        return {"effect_id": self.effect_id, "parameters": json.loads(self._parameters_json)}


class TrackKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    TITLE = "title"
    GRAPHIC = "graphic"


@dataclass(frozen=True)
class MediaClip:
    item_id: str
    start_frame: int
    duration_frames: int
    source: ClipSource
    effects: tuple[EffectRequest, ...] = ()

    def __post_init__(self) -> None:
        _validate_item(self.item_id, self.start_frame, self.duration_frames)
        if not isinstance(self.source, ClipSource):
            raise ValueError("media clip source is invalid")
        object.__setattr__(self, "effects", tuple(self.effects))
        if len(self.effects) > _MAX_EFFECTS_PER_CLIP:
            raise ValueError("media clip contains too many effects")
        if any(not isinstance(effect, EffectRequest) for effect in self.effects):
            raise ValueError("media clip effects are invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": "media",
            "item_id": self.item_id,
            "start_frame": self.start_frame,
            "duration_frames": self.duration_frames,
            "source": self.source.to_payload(),
            "effects": [effect.to_payload() for effect in self.effects],
        }


@dataclass(frozen=True)
class TextClip:
    item_id: str
    start_frame: int
    duration_frames: int
    text: str
    style_id: str

    def __post_init__(self) -> None:
        _validate_item(self.item_id, self.start_frame, self.duration_frames)
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or "\x00" in self.text
            or len(self.text) > 4_000
        ):
            raise ValueError("text clip content is invalid")
        require_identifier(self.style_id, "style_id")

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": "text",
            "item_id": self.item_id,
            "start_frame": self.start_frame,
            "duration_frames": self.duration_frames,
            "text": self.text,
            "style_id": self.style_id,
        }


@dataclass(frozen=True)
class GraphicClip:
    item_id: str
    start_frame: int
    duration_frames: int
    artifact_id: str

    def __post_init__(self) -> None:
        _validate_item(self.item_id, self.start_frame, self.duration_frames)
        require_identifier(self.artifact_id, "artifact_id")

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": "graphic",
            "item_id": self.item_id,
            "start_frame": self.start_frame,
            "duration_frames": self.duration_frames,
            "artifact_id": self.artifact_id,
        }


TimelineItem: TypeAlias = MediaClip | TextClip | GraphicClip


def _validate_item(item_id: str, start_frame: int, duration_frames: int) -> None:
    require_identifier(item_id, "item_id")
    _non_negative_integer(start_frame, "start_frame")
    _positive_integer(duration_frames, "duration_frames")
    if start_frame + duration_frames > _MAX_FRAME:
        raise ValueError("timeline item end is outside supported frame bounds")


@dataclass(frozen=True)
class Track:
    track_id: str
    kind: TrackKind
    items: tuple[TimelineItem, ...]

    def __post_init__(self) -> None:
        require_identifier(self.track_id, "track_id")
        if not isinstance(self.kind, TrackKind):
            raise ValueError("track kind is invalid")
        object.__setattr__(self, "items", tuple(self.items))
        if len(self.items) > _MAX_ITEMS_PER_TRACK:
            raise ValueError("track contains too many items")
        expected = {
            TrackKind.VIDEO: MediaClip,
            TrackKind.AUDIO: MediaClip,
            TrackKind.TITLE: TextClip,
            TrackKind.GRAPHIC: GraphicClip,
        }[self.kind]
        if any(not isinstance(item, expected) for item in self.items):
            raise ValueError(f"{self.kind.value} track contains an incompatible item")
        ordered = sorted(self.items, key=lambda item: item.start_frame)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.start_frame + previous.duration_frames > current.start_frame:
                raise ValueError("items on the same track cannot overlap")
        identifiers = [item.item_id for item in self.items]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("track item IDs must be unique")
        object.__setattr__(self, "items", tuple(ordered))

    def to_payload(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "kind": self.kind.value,
            "items": [item.to_payload() for item in self.items],
        }


@dataclass(frozen=True)
class Marker:
    marker_id: str
    frame: int
    label: str
    note: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.marker_id, "marker_id")
        _non_negative_integer(self.frame, "frame")
        if (
            not isinstance(self.label, str)
            or not isinstance(self.note, str)
            or not self.label.strip()
            or "\x00" in self.label
            or "\x00" in self.note
            or len(self.label) > 160
            or len(self.note) > 2_000
        ):
            raise ValueError("marker text is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "marker_id": self.marker_id,
            "frame": self.frame,
            "label": self.label,
            "note": self.note,
        }


@dataclass(frozen=True)
class EditTimeline:
    timeline_id: str
    project_id: str
    plan_id: str
    plan_revision: int
    plan_content_hash: str
    frame_rate: Rational
    width: int
    height: int
    tracks: tuple[Track, ...]
    markers: tuple[Marker, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tracks", tuple(self.tracks))
        object.__setattr__(self, "markers", tuple(self.markers))
        for field in ("timeline_id", "project_id", "plan_id"):
            require_identifier(getattr(self, field), field)
        _positive_integer(self.plan_revision, "plan_revision")
        if (
            not isinstance(self.plan_content_hash, str)
            or _SHA256.fullmatch(self.plan_content_hash) is None
        ):
            raise ValueError("plan_content_hash must be a lowercase SHA-256 digest")
        if not isinstance(self.frame_rate, Rational):
            raise ValueError("timeline frame rate is invalid")
        _positive_integer(self.width, "width", 65_535)
        _positive_integer(self.height, "height", 65_535)
        if not self.tracks:
            raise ValueError("timeline must contain at least one track")
        if len(self.tracks) > _MAX_TRACKS or len(self.markers) > _MAX_MARKERS:
            raise ValueError("timeline contains too many tracks or markers")
        if any(not isinstance(track, Track) for track in self.tracks):
            raise ValueError("timeline tracks are invalid")
        if any(not isinstance(marker, Marker) for marker in self.markers):
            raise ValueError("timeline markers are invalid")
        object.__setattr__(
            self,
            "markers",
            tuple(sorted(self.markers, key=lambda marker: (marker.frame, marker.marker_id))),
        )
        track_ids = [track.track_id for track in self.tracks]
        item_ids = [item.item_id for track in self.tracks for item in track.items]
        marker_ids = [marker.marker_id for marker in self.markers]
        if len(track_ids) != len(set(track_ids)):
            raise ValueError("timeline track IDs must be unique")
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("timeline item IDs must be unique")
        if len(marker_ids) != len(set(marker_ids)):
            raise ValueError("timeline marker IDs must be unique")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "timeline_id": self.timeline_id,
            "project_id": self.project_id,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "plan_content_hash": self.plan_content_hash,
            "frame_rate": self.frame_rate.to_storage(),
            "canvas": {"width": self.width, "height": self.height},
            "tracks": [track.to_payload() for track in self.tracks],
            "markers": [marker.to_payload() for marker in self.markers],
        }
