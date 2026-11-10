"""Transactional persistence for user-authored material annotations."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping

from clipen.projects.models import ProjectError, validate_project_id, validate_request_id
from clipen.projects.storage import ProjectRepository, command_digest, utc_now

from .models import DayPart, MaterialUsage
from .suggestions import annotation_suggestion


_VERSION_ID = re.compile(r"av_[0-9a-f]{32}\Z")


class AnnotationRepository:
    def __init__(self, projects: ProjectRepository) -> None:
        self.projects = projects

    def get(self, project_id: str, version_id: str, stream_index: int) -> dict[str, object]:
        validate_project_id(project_id)
        self._validate_identity(version_id, stream_index)
        try:
            with self.projects.connect(project_id) as database:
                self._require_version(database, version_id)
                material = database.execute(
                    "SELECT * FROM material_annotations WHERE version_id=?",
                    (version_id,),
                ).fetchone()
                transcript = database.execute(
                    "SELECT * FROM transcript_annotations "
                    "WHERE version_id=? AND stream_index=?",
                    (version_id, stream_index),
                ).fetchone() if stream_index >= 0 else None
                current_revision = self._current_transcript_revision(
                    database, version_id, stream_index
                )
                suggestion = annotation_suggestion(database, version_id)
            return self._public(
                material, transcript, version_id, stream_index, current_revision, suggestion
            )
        except ProjectError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取素材標註。", status_code=500
            ) from error
        except sqlite3.Error as error:
            raise ProjectError(
                "storage_failed", "Clipen 無法讀取素材標註。", status_code=500
            ) from error

    def save(
        self,
        project_id: str,
        request_id: object,
        version_id: str,
        stream_index: int,
        expected_material_revision: object,
        expected_transcript_revision: object,
        material: object,
        transcript: object,
    ) -> dict[str, object]:
        validate_project_id(project_id)
        normalized_request = validate_request_id(request_id)
        self._validate_identity(version_id, stream_index)
        material_revision = self._expected_revision(expected_material_revision)
        transcript_revision = self._expected_revision(expected_transcript_revision)
        normalized_material = self._material(material)
        normalized_transcript = self._transcript(transcript, stream_index)
        request_hash = command_digest({
            "version_id": version_id,
            "stream_index": stream_index,
            "expected_material_revision": material_revision,
            "expected_transcript_revision": transcript_revision,
            "material": normalized_material,
            "transcript": normalized_transcript,
        })
        now = utc_now()
        try:
            with self.projects.connect(project_id) as database:
                database.execute("BEGIN IMMEDIATE")
                receipt = database.execute(
                    "SELECT command_type,request_hash,response_json "
                    "FROM annotation_command_receipts WHERE request_id=?",
                    (normalized_request,),
                ).fetchone()
                if receipt is not None:
                    if receipt["command_type"] != "save_annotation" or receipt["request_hash"] != request_hash:
                        raise ProjectError(
                            "request_conflict",
                            "這次保存標註的操作與既有內容衝突。",
                            status_code=409,
                        )
                    response = json.loads(receipt["response_json"])
                    database.rollback()
                    return response
                self._require_version(database, version_id)
                current_material = database.execute(
                    "SELECT revision FROM material_annotations WHERE version_id=?",
                    (version_id,),
                ).fetchone()
                observed_material = 0 if current_material is None else current_material["revision"]
                current_transcript = database.execute(
                    "SELECT revision FROM transcript_annotations "
                    "WHERE version_id=? AND stream_index=?",
                    (version_id, stream_index),
                ).fetchone() if stream_index >= 0 else None
                observed_transcript = 0 if current_transcript is None else current_transcript["revision"]
                if observed_material != material_revision or observed_transcript != transcript_revision:
                    raise ProjectError(
                        "annotation_revision_conflict",
                        "素材標註已更新，請重新整理後再保存。",
                        status_code=409,
                    )
                next_material = observed_material + 1
                database.execute(
                    "INSERT INTO material_annotations "
                    "(version_id,revision,people_json,location,trip_day,day_part,note,usage,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(version_id) DO UPDATE SET "
                    "revision=excluded.revision,people_json=excluded.people_json,"
                    "location=excluded.location,trip_day=excluded.trip_day,"
                    "day_part=excluded.day_part,note=excluded.note,usage=excluded.usage,"
                    "updated_at=excluded.updated_at",
                    (
                        version_id, next_material,
                        json.dumps(normalized_material["people"], ensure_ascii=False, separators=(",", ":")),
                        normalized_material["location"], normalized_material["trip_day"],
                        normalized_material["day_part"], normalized_material["note"],
                        normalized_material["usage"], now,
                    ),
                )
                next_transcript = observed_transcript
                if normalized_transcript is not None:
                    source_revision = self._current_transcript_revision(
                        database, version_id, stream_index
                    )
                    if source_revision is None or source_revision != normalized_transcript["source_revision_id"]:
                        raise ProjectError(
                            "annotation_transcript_changed",
                            "逐字稿已重新分析，請重新載入後再保存修正。",
                            status_code=409,
                        )
                    self._validate_corrections(
                        database, source_revision, normalized_transcript["corrections"]
                    )
                    next_transcript += 1
                    database.execute(
                        "INSERT INTO transcript_annotations "
                        "(version_id,stream_index,source_revision_id,revision,confirmed,discarded,"
                        "corrections_json,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(version_id,stream_index) DO UPDATE SET "
                        "source_revision_id=excluded.source_revision_id,revision=excluded.revision,"
                        "confirmed=excluded.confirmed,discarded=excluded.discarded,"
                        "corrections_json=excluded.corrections_json,"
                        "updated_at=excluded.updated_at",
                        (
                            version_id, stream_index, source_revision, next_transcript,
                            int(normalized_transcript["confirmed"]),
                            int(normalized_transcript["discarded"]),
                            json.dumps(normalized_transcript["corrections"], ensure_ascii=False, separators=(",", ":")),
                            now,
                        ),
                    )
                material_row = database.execute(
                    "SELECT * FROM material_annotations WHERE version_id=?", (version_id,)
                ).fetchone()
                transcript_row = database.execute(
                    "SELECT * FROM transcript_annotations WHERE version_id=? AND stream_index=?",
                    (version_id, stream_index),
                ).fetchone() if stream_index >= 0 else None
                response = self._public(
                    material_row, transcript_row, version_id, stream_index,
                    self._current_transcript_revision(database, version_id, stream_index),
                    annotation_suggestion(database, version_id),
                )
                database.execute(
                    "INSERT INTO annotation_command_receipts "
                    "(request_id,command_type,request_hash,response_json,created_at) "
                    "VALUES(?,'save_annotation',?,?,?)",
                    (
                        normalized_request, request_hash,
                        json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        now,
                    ),
                )
                database.commit()
                return response
        except ProjectError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProjectError(
                "annotation_invalid", "素材標註內容無效。", status_code=422
            ) from error
        except sqlite3.Error as error:
            raise ProjectError(
                "storage_failed", "Clipen 無法保存素材標註。", status_code=500
            ) from error

    @staticmethod
    def _validate_identity(version_id: str, stream_index: int) -> None:
        if not isinstance(version_id, str) or _VERSION_ID.fullmatch(version_id) is None:
            raise ProjectError("annotation_not_found", "找不到這個素材。", status_code=404)
        if isinstance(stream_index, bool) or not isinstance(stream_index, int) or stream_index < -1:
            raise ProjectError("annotation_not_found", "找不到這個素材。", status_code=404)

    @staticmethod
    def _expected_revision(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProjectError("annotation_invalid", "素材標註版本無效。", status_code=422)
        return value

    @staticmethod
    def _material(value: object) -> dict[str, object]:
        if not isinstance(value, Mapping) or set(value) != {"people", "location", "trip_day", "day_part", "note", "usage"}:
            raise ProjectError("annotation_invalid", "素材標註內容無效。", status_code=422)
        people = value["people"]
        if not isinstance(people, list) or len(people) > 20:
            raise ProjectError("annotation_invalid", "人物標註內容無效。", status_code=422)
        normalized_people = []
        for person in people:
            if not isinstance(person, str) or not person.strip() or "\x00" in person or len(person.strip()) > 80:
                raise ProjectError("annotation_invalid", "人物標註內容無效。", status_code=422)
            normalized_people.append(person.strip())
        if len(normalized_people) != len(set(normalized_people)):
            raise ProjectError("annotation_invalid", "人物標註不能重複。", status_code=422)
        location = value["location"]
        note = value["note"]
        if location is not None and (not isinstance(location, str) or "\x00" in location or len(location.strip()) > 160):
            raise ProjectError("annotation_invalid", "地點標註內容無效。", status_code=422)
        if not isinstance(note, str) or "\x00" in note or len(note) > 2_000:
            raise ProjectError("annotation_invalid", "剪輯備註內容無效。", status_code=422)
        trip_day = value["trip_day"]
        if trip_day is not None and (isinstance(trip_day, bool) or not isinstance(trip_day, int) or not 1 <= trip_day <= 365):
            raise ProjectError("annotation_invalid", "旅程日次無效。", status_code=422)
        day_part = value["day_part"]
        usage = value["usage"]
        try:
            if day_part is not None:
                day_part = DayPart(day_part).value
            usage = MaterialUsage(usage).value
        except (TypeError, ValueError) as error:
            raise ProjectError(
                "annotation_invalid", "素材使用方式或旅程時段無效。", status_code=422
            ) from error
        return {
            "people": normalized_people,
            "location": None if location is None or not location.strip() else location.strip(),
            "trip_day": trip_day,
            "day_part": day_part,
            "note": note.strip(),
            "usage": usage,
        }

    @staticmethod
    def _transcript(value: object, stream_index: int) -> dict[str, object] | None:
        if value is None:
            return None
        if stream_index < 0 or not isinstance(value, Mapping) or set(value) != {"source_revision_id", "confirmed", "discarded", "corrections"}:
            raise ProjectError("annotation_invalid", "逐字稿修正內容無效。", status_code=422)
        source_revision = value["source_revision_id"]
        confirmed = value["confirmed"]
        discarded = value["discarded"]
        corrections = value["corrections"]
        if not isinstance(source_revision, str) or not source_revision or not isinstance(confirmed, bool) or not isinstance(discarded, bool) or not isinstance(corrections, list) or len(corrections) > 1_000:
            raise ProjectError("annotation_invalid", "逐字稿修正內容無效。", status_code=422)
        normalized = []
        ordinals = set()
        for correction in corrections:
            if not isinstance(correction, Mapping) or set(correction) != {"ordinal", "text"}:
                raise ProjectError("annotation_invalid", "逐字稿修正內容無效。", status_code=422)
            ordinal = correction["ordinal"]
            text = correction["text"]
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0 or ordinal in ordinals or not isinstance(text, str) or "\x00" in text or len(text) > 1_000:
                raise ProjectError("annotation_invalid", "逐字稿修正內容無效。", status_code=422)
            ordinals.add(ordinal)
            normalized.append({"ordinal": ordinal, "text": text.strip()})
        return {
            "source_revision_id": source_revision,
            "confirmed": False if discarded else confirmed,
            "discarded": discarded,
            "corrections": normalized,
        }

    @staticmethod
    def _require_version(database: sqlite3.Connection, version_id: str) -> None:
        if database.execute("SELECT 1 FROM asset_versions WHERE version_id=?", (version_id,)).fetchone() is None:
            raise ProjectError("annotation_not_found", "找不到這個素材。", status_code=404)

    @staticmethod
    def _current_transcript_revision(database: sqlite3.Connection, version_id: str, stream_index: int) -> str | None:
        if stream_index < 0:
            return None
        row = database.execute(
            "SELECT items.transcript_revision_id FROM current_preparation AS current "
            "JOIN preparation_items AS items ON items.run_id=current.run_id "
            "WHERE current.slot=1 AND items.version_id=? AND items.stream_index=? "
            "AND items.status='transcribed'",
            (version_id, stream_index),
        ).fetchone()
        return None if row is None else row["transcript_revision_id"]

    @staticmethod
    def _validate_corrections(database: sqlite3.Connection, revision_id: str, corrections: object) -> None:
        available = {
            row["ordinal"]
            for row in database.execute(
                "SELECT ordinal FROM transcript_segments WHERE revision_id=?", (revision_id,)
            )
        }
        if any(item["ordinal"] not in available for item in corrections):
            raise ProjectError("annotation_transcript_changed", "逐字稿片段已更新，請重新載入。", status_code=409)


    @staticmethod
    def _public(
        material,
        transcript,
        version_id: str,
        stream_index: int,
        current_revision: str | None,
        suggestion: dict[str, object],
    ) -> dict[str, object]:
        if material is None:
            material_public = {
                "version_id": version_id, "revision": 0, "people": [], "location": None,
                "trip_day": None, "day_part": None, "note": "", "usage": "normal",
            }
        else:
            people = json.loads(material["people_json"])
            try:
                normalized = AnnotationRepository._material({
                    "people": people,
                    "location": material["location"],
                    "trip_day": material["trip_day"],
                    "day_part": material["day_part"],
                    "note": material["note"],
                    "usage": material["usage"],
                })
            except ProjectError as error:
                raise ValueError("stored material annotation is invalid") from error
            if isinstance(material["revision"], bool) or not isinstance(material["revision"], int) or material["revision"] < 1:
                raise ValueError("stored material annotation revision is invalid")
            material_public = {
                "version_id": version_id, "revision": material["revision"], **normalized,
            }
        transcript_public = None
        if current_revision is not None:
            stale = transcript is not None and transcript["source_revision_id"] != current_revision
            corrections = [] if transcript is None or stale else json.loads(transcript["corrections_json"])
            if transcript is not None and not stale:
                try:
                    normalized_transcript = AnnotationRepository._transcript({
                        "source_revision_id": current_revision,
                        "confirmed": bool(transcript["confirmed"]),
                        "discarded": bool(transcript["discarded"]),
                        "corrections": corrections,
                    }, stream_index)
                except ProjectError as error:
                    raise ValueError("stored transcript annotation is invalid") from error
                assert normalized_transcript is not None
                corrections = normalized_transcript["corrections"]
            transcript_public = {
                "stream_index": stream_index,
                "revision": 0 if transcript is None else transcript["revision"],
                "source_revision_id": current_revision,
                "confirmed": False if transcript is None or stale else bool(transcript["confirmed"]),
                "discarded": False if transcript is None else bool(transcript["discarded"]),
                "corrections": corrections,
                "stale": stale,
            }
        return {
            "material": material_public,
            "transcript": transcript_public,
            "suggestion": suggestion,
        }
