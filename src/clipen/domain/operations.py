"""State rules shared by every long-running Clipen operation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Mapping, AbstractSet


class InvalidTransition(ValueError):
    """A requested state change violates the product state machine."""


class OperationKind(str, Enum):
    MATERIAL_INVENTORY = "material_inventory"
    MATERIAL_ANALYSIS = "material_analysis"
    REFERENCE_ANALYSIS = "reference_analysis"
    PLAN_GENERATION = "plan_generation"
    EDIT_EXECUTION = "edit_execution"
    EDIT_REVISION = "edit_revision"
    VIDEO_EXPORT = "video_export"
    RESOLVE_EXPORT = "resolve_export"


class OperationState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class AttemptState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


_OPERATION_TRANSITIONS: Mapping[OperationState, AbstractSet[OperationState]] = {
    OperationState.QUEUED: {OperationState.RUNNING, OperationState.CANCELLED},
    OperationState.RUNNING: {
        OperationState.CANCEL_REQUESTED,
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.INTERRUPTED,
    },
    OperationState.CANCEL_REQUESTED: {
        OperationState.CANCELLED,
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.INTERRUPTED,
    },
    OperationState.FAILED: {OperationState.QUEUED},
    OperationState.INTERRUPTED: {OperationState.QUEUED},
    OperationState.SUCCEEDED: set(),
    OperationState.CANCELLED: set(),
}

_ATTEMPT_TRANSITIONS: Mapping[AttemptState, AbstractSet[AttemptState]] = {
    AttemptState.QUEUED: {AttemptState.RUNNING, AttemptState.CANCELLED},
    AttemptState.RUNNING: {
        AttemptState.CANCEL_REQUESTED,
        AttemptState.SUCCEEDED,
        AttemptState.FAILED,
        AttemptState.INTERRUPTED,
    },
    AttemptState.CANCEL_REQUESTED: {
        AttemptState.CANCELLED,
        AttemptState.SUCCEEDED,
        AttemptState.FAILED,
        AttemptState.INTERRUPTED,
    },
    AttemptState.SUCCEEDED: set(),
    AttemptState.FAILED: set(),
    AttemptState.INTERRUPTED: set(),
    AttemptState.CANCELLED: set(),
}


def _require_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{field} must be a non-empty bounded identifier")


@dataclass(frozen=True)
class Operation:
    operation_id: str
    project_id: str
    kind: OperationKind
    input_snapshot_id: str
    state: OperationState = OperationState.QUEUED
    revision: int = 0

    def __post_init__(self) -> None:
        _require_identifier(self.operation_id, "operation_id")
        _require_identifier(self.project_id, "project_id")
        _require_identifier(self.input_snapshot_id, "input_snapshot_id")
        if self.revision < 0:
            raise ValueError("revision cannot be negative")

    def transition(self, target: OperationState) -> "Operation":
        if target not in _OPERATION_TRANSITIONS[self.state]:
            raise InvalidTransition(f"operation cannot move from {self.state} to {target}")
        return replace(self, state=target, revision=self.revision + 1)


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    operation_id: str
    number: int
    state: AttemptState = AttemptState.QUEUED
    revision: int = 0

    def __post_init__(self) -> None:
        _require_identifier(self.attempt_id, "attempt_id")
        _require_identifier(self.operation_id, "operation_id")
        if self.number < 1:
            raise ValueError("attempt number must start at one")
        if self.revision < 0:
            raise ValueError("revision cannot be negative")

    def transition(self, target: AttemptState) -> "Attempt":
        if target not in _ATTEMPT_TRANSITIONS[self.state]:
            raise InvalidTransition(f"attempt cannot move from {self.state} to {target}")
        return replace(self, state=target, revision=self.revision + 1)
