"""Crash-safe persistence for immutable compiled timelines."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3

from clipen.domain import EditTimeline
from clipen.projects.models import ProjectError, validate_project_id, validate_request_id
from clipen.projects.storage import ProjectRepository, command_digest, utc_now

from .compiler import OutputSettings


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class TimelineRecord:
    timeline: EditTimeline
    settings: OutputSettings
    created_at: str

    def to_public(self) -> dict[str, object]:
        timeline = self.timeline
        return {
            "timeline_id": timeline.timeline_id,
            "plan_id": timeline.plan_id,
            "plan_revision": timeline.plan_revision,
            "plan_content_hash": timeline.plan_content_hash,
            "output": self.settings.to_payload(),
            "duration_frames": timeline.duration_frames,
            "duration_seconds": round(
                timeline.duration_frames
                * timeline.frame_rate.denominator
                / timeline.frame_rate.numerator,
                3,
            ),
            "tracks": [
                {
                    "track_id": track.track_id,
                    "kind": track.kind.value,
                    "item_count": len(track.items),
                }
                for track in timeline.tracks
            ],
            "markers": [marker.to_payload() for marker in timeline.markers],
            "created_at": self.created_at,
        }


class TimelineRepository:
    def __init__(self, projects: ProjectRepository) -> None:
        self.projects = projects

    def save(
        self,
        project_id: str,
        request_id: object,
        plan_id: str,
        plan_revision: int,
        settings: OutputSettings,
        timeline: EditTimeline,
    ) -> tuple[TimelineRecord, bool]:
        validate_project_id(project_id)
        normalized_request = validate_request_id(request_id)
        if (
            timeline.project_id != project_id
            or timeline.plan_id != plan_id
            or timeline.plan_revision != plan_revision
        ):
            raise ValueError("timeline identity does not match the save request")
        if (
            timeline.width,
            timeline.height,
            timeline.frame_rate.numerator,
            timeline.frame_rate.denominator,
        ) != (*settings.canvas, settings.frame_rate, 1):
            raise ValueError("timeline output does not match the save request")
        request_hash = command_digest(
            {
                "plan_id": plan_id,
                "plan_revision": plan_revision,
                "output": settings.to_payload(),
            }
        )
        payload = timeline.to_payload()
        encoded = _encode(payload)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = utc_now()
        try:
            with self.projects.connect(project_id) as database:
                database.execute("BEGIN IMMEDIATE")
                receipt = database.execute(
                    "SELECT command_type,request_hash,timeline_id "
                    "FROM timeline_command_receipts WHERE request_id=?",
                    (normalized_request,),
                ).fetchone()
                if receipt is not None:
                    if (
                        receipt["command_type"] != "build_timeline"
                        or receipt["request_hash"] != request_hash
                    ):
                        raise ProjectError(
                            "request_conflict",
                            "這次建立剪輯版本的操作與既有內容衝突。",
                            status_code=409,
                        )
                    row = database.execute(
                        "SELECT * FROM edit_timelines WHERE timeline_id=?",
                        (receipt["timeline_id"],),
                    ).fetchone()
                    database.rollback()
                    if row is None:
                        raise ProjectError(
                            "storage_corrupt",
                            "Clipen 無法讀取剪輯版本。",
                            status_code=500,
                        )
                    return self._record(row), False
                existing = database.execute(
                    "SELECT payload_digest FROM edit_timelines WHERE timeline_id=?",
                    (timeline.timeline_id,),
                ).fetchone()
                if existing is not None and existing["payload_digest"] != digest:
                    raise ProjectError(
                        "timeline_conflict",
                        "剪輯版本識別資訊與既有內容衝突。",
                        status_code=409,
                    )
                if existing is None:
                    database.execute(
                        "INSERT INTO edit_timelines "
                        "(timeline_id,project_id,plan_id,plan_revision,plan_content_hash,"
                        "output_preset,frame_rate,payload_digest,payload_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            timeline.timeline_id,
                            project_id,
                            timeline.plan_id,
                            timeline.plan_revision,
                            timeline.plan_content_hash,
                            settings.preset,
                            settings.frame_rate,
                            digest,
                            encoded,
                            now,
                        ),
                    )
                database.execute(
                    "INSERT INTO current_timeline(slot,timeline_id) VALUES(1,?) "
                    "ON CONFLICT(slot) DO UPDATE SET timeline_id=excluded.timeline_id",
                    (timeline.timeline_id,),
                )
                database.execute(
                    "INSERT INTO timeline_command_receipts "
                    "(request_id,command_type,request_hash,timeline_id,created_at) "
                    "VALUES(?,'build_timeline',?,?,?)",
                    (normalized_request, request_hash, timeline.timeline_id, now),
                )
                database.commit()
            return self._get(project_id, timeline.timeline_id), True
        except ProjectError:
            raise
        except sqlite3.Error as error:
            raise ProjectError(
                "storage_failed", "Clipen 無法保存剪輯版本。", status_code=500
            ) from error

    def current(self, project_id: str) -> TimelineRecord | None:
        validate_project_id(project_id)
        try:
            with self.projects.connect(project_id) as database:
                row = database.execute(
                    "SELECT timelines.* FROM current_timeline AS current "
                    "JOIN edit_timelines AS timelines "
                    "ON timelines.timeline_id=current.timeline_id WHERE current.slot=1"
                ).fetchone()
            return None if row is None else self._record(row)
        except ProjectError:
            raise
        except sqlite3.Error as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取剪輯版本。", status_code=500
            ) from error

    def _get(self, project_id: str, timeline_id: str) -> TimelineRecord:
        with self.projects.connect(project_id) as database:
            row = database.execute(
                "SELECT * FROM edit_timelines WHERE timeline_id=?", (timeline_id,)
            ).fetchone()
        if row is None:
            raise ProjectError("timeline_not_found", "找不到剪輯版本。", status_code=404)
        return self._record(row)

    @staticmethod
    def _record(row) -> TimelineRecord:
        try:
            payload = json.loads(row["payload_json"])
            encoded = _encode(payload)
            if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row["payload_digest"]:
                raise ValueError("timeline digest mismatch")
            timeline = EditTimeline.from_payload(payload)
            if (
                timeline.timeline_id != row["timeline_id"]
                or timeline.project_id != row["project_id"]
                or timeline.plan_id != row["plan_id"]
                or timeline.plan_revision != row["plan_revision"]
                or timeline.plan_content_hash != row["plan_content_hash"]
            ):
                raise ValueError("timeline metadata mismatch")
            settings = OutputSettings(row["output_preset"], row["frame_rate"])
            if (
                timeline.width,
                timeline.height,
                timeline.frame_rate.numerator,
                timeline.frame_rate.denominator,
            ) != (*settings.canvas, settings.frame_rate, 1):
                raise ValueError("timeline output settings mismatch")
            return TimelineRecord(timeline, settings, row["created_at"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取剪輯版本。", status_code=500
            ) from error
