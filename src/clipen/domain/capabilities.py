"""Versioned delivery capabilities proven by concrete implementation tests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .timeline import TrackKind
from .validation import require_identifier


_EFFECT_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_MAX_PARAMETER_SCHEMA_BYTES = 32_768
_MAX_EFFECT_CAPABILITIES = 10_000


class DeliveryMode(str, Enum):
    MOVIE = "movie"
    RESOLVE_PROJECT = "resolve_project"


@dataclass(frozen=True)
class VerifiedDelivery:
    """One delivery path backed by a named implementation and test report."""

    mode: DeliveryMode
    implementation_id: str
    test_report_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.mode, DeliveryMode):
            raise ValueError("delivery mode is invalid")
        require_identifier(self.implementation_id, "implementation_id")
        require_identifier(self.test_report_id, "test_report_id")

    def to_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "implementation_id": self.implementation_id,
            "test_report_id": self.test_report_id,
        }


def _verified_deliveries(
    values: tuple[VerifiedDelivery, ...],
) -> tuple[VerifiedDelivery, ...]:
    deliveries = tuple(values)
    if any(not isinstance(value, VerifiedDelivery) for value in deliveries):
        raise ValueError("verified deliveries are invalid")
    modes = [value.mode for value in deliveries]
    if len(modes) != len(set(modes)):
        raise ValueError("delivery modes must be unique")
    return tuple(sorted(deliveries, key=lambda value: value.mode.value))


@dataclass(frozen=True)
class ElementCapability:
    """Delivery support for one native timeline item kind."""

    kind: TrackKind
    deliveries: tuple[VerifiedDelivery, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TrackKind):
            raise ValueError("element capability kind is invalid")
        object.__setattr__(self, "deliveries", _verified_deliveries(self.deliveries))

    def supports(self, mode: DeliveryMode) -> bool:
        return any(delivery.mode is mode for delivery in self.deliveries)

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "deliveries": [delivery.to_payload() for delivery in self.deliveries],
        }


def _reject_external_references(value: object, *, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("effect parameter schema is nested too deeply")
    if isinstance(value, Mapping):
        reference = value.get("$ref")
        if reference is not None and (
            not isinstance(reference, str) or not reference.startswith("#")
        ):
            raise ValueError("effect parameter schema cannot use external references")
        for child in value.values():
            _reject_external_references(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _reject_external_references(child, depth=depth + 1)


def _parameter_schema(value: Mapping[str, object]) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("effect parameter schema must be an object")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        parsed = json.loads(encoded)
    except (RecursionError, TypeError, ValueError) as error:
        raise ValueError("effect parameter schema must contain JSON values") from error
    if len(encoded.encode("utf-8")) > _MAX_PARAMETER_SCHEMA_BYTES:
        raise ValueError("effect parameter schema is too large")
    _reject_external_references(parsed)
    try:
        Draft202012Validator.check_schema(parsed)
    except (RecursionError, SchemaError) as error:
        raise ValueError("effect parameter schema is invalid") from error
    if parsed.get("type") != "object":
        raise ValueError("effect parameter schema must validate an object")
    return encoded


@dataclass(frozen=True, init=False)
class EffectCapability:
    """A safe, versioned effect contract for the supported delivery paths."""

    effect_id: str
    version: int
    track_kinds: tuple[TrackKind, ...]
    deliveries: tuple[VerifiedDelivery, ...]
    _parameter_schema_json: str

    def __init__(
        self,
        effect_id: str,
        version: int,
        track_kinds: tuple[TrackKind, ...],
        parameter_schema: Mapping[str, object],
        deliveries: tuple[VerifiedDelivery, ...],
    ) -> None:
        if not isinstance(effect_id, str) or _EFFECT_ID.fullmatch(effect_id) is None:
            raise ValueError("effect capability ID is invalid")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("effect capability version is invalid")
        normalized_kinds = tuple(track_kinds)
        if not normalized_kinds or any(
            not isinstance(kind, TrackKind) for kind in normalized_kinds
        ):
            raise ValueError("effect capability track kinds are invalid")
        if len(normalized_kinds) != len(set(normalized_kinds)):
            raise ValueError("effect capability track kinds must be unique")
        object.__setattr__(self, "effect_id", effect_id)
        object.__setattr__(self, "version", version)
        object.__setattr__(
            self,
            "track_kinds",
            tuple(sorted(normalized_kinds, key=lambda kind: kind.value)),
        )
        object.__setattr__(self, "deliveries", _verified_deliveries(deliveries))
        object.__setattr__(
            self, "_parameter_schema_json", _parameter_schema(parameter_schema)
        )

    @property
    def parameter_schema(self) -> Mapping[str, object]:
        return json.loads(self._parameter_schema_json)

    def supports(self, kind: TrackKind, mode: DeliveryMode) -> bool:
        return kind in self.track_kinds and any(
            delivery.mode is mode for delivery in self.deliveries
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "version": self.version,
            "track_kinds": [kind.value for kind in self.track_kinds],
            "parameter_schema": self.parameter_schema,
            "deliveries": [delivery.to_payload() for delivery in self.deliveries],
        }


@dataclass(frozen=True)
class CapabilityProfile:
    """An immutable snapshot; absent capabilities are deliberately unsupported."""

    profile_id: str
    revision: int
    elements: tuple[ElementCapability, ...] = ()
    effects: tuple[EffectCapability, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.profile_id, "profile_id")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("capability profile revision is invalid")
        elements = tuple(self.elements)
        effects = tuple(self.effects)
        if any(not isinstance(item, ElementCapability) for item in elements):
            raise ValueError("element capabilities are invalid")
        if any(not isinstance(item, EffectCapability) for item in effects):
            raise ValueError("effect capabilities are invalid")
        if len(effects) > _MAX_EFFECT_CAPABILITIES:
            raise ValueError("capability profile contains too many effects")
        if len({item.kind for item in elements}) != len(elements):
            raise ValueError("element capability kinds must be unique")
        effect_keys = {(item.effect_id, item.version) for item in effects}
        if len(effect_keys) != len(effects):
            raise ValueError("effect capability versions must be unique")
        object.__setattr__(
            self, "elements", tuple(sorted(elements, key=lambda item: item.kind.value))
        )
        object.__setattr__(
            self,
            "effects",
            tuple(sorted(effects, key=lambda item: (item.effect_id, item.version))),
        )

    def element(self, kind: TrackKind) -> ElementCapability | None:
        return next((item for item in self.elements if item.kind is kind), None)

    def effect(self, effect_id: str, version: int) -> EffectCapability | None:
        return next(
            (
                item
                for item in self.effects
                if item.effect_id == effect_id and item.version == version
            ),
            None,
        )

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self._encoded_content().encode("utf-8")).hexdigest()

    def _encoded_content(self) -> str:
        return json.dumps(
            {
                "profile_id": self.profile_id,
                "revision": self.revision,
                "elements": [item.to_payload() for item in self.elements],
                "effects": [item.to_payload() for item in self.effects],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def to_payload(self) -> dict[str, object]:
        content = json.loads(self._encoded_content())
        return {"schema_version": 1, **content, "content_hash": self.content_hash}
