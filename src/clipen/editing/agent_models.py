"""Validated values shared by AI editing storage and tool layers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping

from clipen.domain import EditTimeline
from clipen.projects import ProjectError


RUN_ID_PATTERN = re.compile(r"editrun_[0-9a-f]{32}\Z")
REVISION_ID_PATTERN = re.compile(r"editrev_[0-9a-f]{32}\Z")
ALLOWED_TITLE_STYLES = frozenset(
    {"title.default", "title.chapter", "title.caption"}
)
ALLOWED_EFFECTS = frozenset(
    {
        "video.punch_in",
        "video.slow_push",
        "video.reframe",
        "video.speed_ramp",
        "audio.dialogue_focus",
        "audio.duck_music",
    }
)


def encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def bounded_text(
    value: object,
    label: str,
    maximum: int,
    *,
    empty: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or "\x00" in value
        or len(value) > maximum
        or (not empty and not value.strip())
    ):
        raise ProjectError("agent_input_invalid", f"{label}內容無效。", status_code=422)
    return value.strip()


def make_id(prefix: str, value: object) -> str:
    return prefix + hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class AgentRun:
    run_id: str
    project_id: str
    base_timeline_id: str
    state: str
    phase: str
    max_tool_calls: int
    tool_calls: int
    max_preview_renders: int
    preview_renders: int
    current_revision_id: str
    final_revision_id: str | None
    provider: str
    provider_attempt_id: str | None
    usage: Mapping[str, int] | None
    error_code: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None

    def to_public(self) -> dict[str, object]:
        usage = None
        if self.usage is not None:
            usage = {
                **self.usage,
                "total_tokens": self.usage["input_tokens"]
                + self.usage["output_tokens"],
            }
        return {
            "run_id": self.run_id,
            "state": self.state,
            "phase": self.phase,
            "base_timeline_id": self.base_timeline_id,
            "current_revision_id": self.current_revision_id,
            "final_revision_id": self.final_revision_id,
            "provider": self.provider,
            "provider_attempt_id": self.provider_attempt_id,
            "usage": usage,
            "budget": {
                "tool_calls": self.tool_calls,
                "max_tool_calls": self.max_tool_calls,
                "remaining_tool_calls": max(0, self.max_tool_calls - self.tool_calls),
                "preview_renders": self.preview_renders,
                "max_preview_renders": self.max_preview_renders,
            },
            "error_code": self.error_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class AgentRevision:
    revision_id: str
    run_id: str
    ordinal: int
    parent_revision_id: str | None
    timeline: EditTimeline
    summary: str
    created_at: str

    def to_public(self, *, include_timeline: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "revision_id": self.revision_id,
            "ordinal": self.ordinal,
            "parent_revision_id": self.parent_revision_id,
            "summary": self.summary,
            "created_at": self.created_at,
        }
        if include_timeline:
            result["timeline"] = self.timeline.to_payload()
        return result


@dataclass(frozen=True, slots=True)
class ToolResult:
    payload: Mapping[str, object]
    images: tuple[Path, ...] = ()
