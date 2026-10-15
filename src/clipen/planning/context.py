"""Build a bounded, path-free provider context from current project snapshots."""

from __future__ import annotations

from collections import defaultdict
from fractions import Fraction
import hashlib
import json
from typing import Any

from clipen.domain import Rational, SourceSpan
from clipen.annotations.suggestions import annotation_suggestions
from clipen.projects import ProjectError, ProjectRepository

from .models import PlanningCandidate, PlanningContext, TranscriptReliability


_MAX_CANDIDATES = 256
_MAX_TRANSCRIPTS = 512
_MAX_TRANSCRIPTS_PER_STREAM = 4
_MAX_WINDOWS_PER_STREAM = 4
_WINDOW_SECONDS = 12

_DAY_PART_LABELS = {
    "dawn": "清晨", "morning": "早上", "noon": "中午",
    "afternoon": "下午", "evening": "傍晚", "night": "晚上",
    "late_night": "深夜",
}


def _candidate_id(key: str, span: SourceSpan, kind: str) -> str:
    identity = json.dumps(
        {"key": key, "kind": kind, "source": span.to_storage()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "candidate-" + hashlib.sha256(identity).hexdigest()[:32]


def _window(span: SourceSpan, center_pts: int, seconds: int = _WINDOW_SECONDS) -> SourceSpan:
    half = Fraction(seconds, 2) * span.time_base.denominator / span.time_base.numerator
    radius = max(1, int(half))
    start = max(int(span.start_pts), center_pts - radius)
    end = min(int(span.end_pts), center_pts + radius)
    if end <= start:
        return span
    return SourceSpan(
        span.version_id,
        span.stream_index,
        str(start),
        str(end),
        span.time_base,
    )


def _fallback_windows(span: SourceSpan) -> tuple[SourceSpan, ...]:
    duration_seconds = (
        Fraction(span.duration_pts)
        * span.time_base.numerator
        / span.time_base.denominator
    )
    if duration_seconds <= 30:
        return (span,)
    count = min(_MAX_WINDOWS_PER_STREAM, max(2, int(duration_seconds // 30)))
    return tuple(
        _window(
            span,
            int(span.start_pts) + span.duration_pts * (index * 2 + 1) // (count * 2),
        )
        for index in range(count)
    )


def _safe_json(raw: object, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as error:
        raise ProjectError(
            "storage_corrupt", f"Clipen 無法讀取{label}。", status_code=500
        ) from error
    if not isinstance(value, dict):
        raise ProjectError(
            "storage_corrupt", f"Clipen 無法讀取{label}。", status_code=500
        )
    return value


class PlanningContextBuilder:
    def __init__(self, projects: ProjectRepository) -> None:
        self.projects = projects

    def build(self, project_id: str) -> PlanningContext:
        with self.projects.connect(project_id) as database:
            project = database.execute(
                "SELECT brief FROM project WHERE project_id = ?", (project_id,)
            ).fetchone()
            inventory = database.execute(
                "SELECT runs.run_id FROM current_inventory AS current "
                "JOIN inventory_runs AS runs ON runs.run_id = current.run_id "
                "WHERE current.slot = 1 AND runs.state = 'succeeded'"
            ).fetchone()
            if project is None:
                raise ProjectError("project_not_found", "找不到這個作品。", status_code=404)
            if not project["brief"].strip():
                raise ProjectError(
                    "planning_brief_required",
                    "請先填寫這次希望怎麼剪。",
                    status_code=409,
                )
            if inventory is None:
                raise ProjectError(
                    "planning_inventory_required",
                    "請先完成素材檢查。",
                    status_code=409,
                )
            evidence = database.execute(
                "SELECT run_id, scope_id FROM evidence_runs "
                "WHERE inventory_run_id = ? AND state = 'succeeded' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (inventory["run_id"],),
            ).fetchone()
            if evidence is None:
                raise ProjectError(
                    "planning_evidence_required",
                    "請先完成本機畫面證據擷取。",
                    status_code=409,
                )
            result_rows = database.execute(
                "SELECT item_key, result_json FROM evidence_results "
                "WHERE run_id = ? ORDER BY ordinal LIMIT 2001",
                (evidence["run_id"],),
            ).fetchall()
            if len(result_rows) > 2_000:
                raise ProjectError(
                    "planning_scope_too_large",
                    "素材範圍超過規劃上限，請拆成不同作品。",
                    status_code=409,
                )
            observation_rows = database.execute(
                "SELECT item_key, result_json FROM evidence_observations "
                "WHERE run_id = ? AND state = 'succeeded' AND result_json IS NOT NULL",
                (evidence["run_id"],),
            ).fetchall()
            transcript_rows = database.execute(
                "WITH ranked AS ("
                "SELECT items.version_id, items.stream_index, items.review_required, "
                "segments.ordinal, segments.start_pts, segments.end_pts, segments.text, "
                "revisions.revision_id, revisions.source_span_json, "
                "ROW_NUMBER() OVER ("
                "PARTITION BY items.version_id, items.stream_index "
                "ORDER BY segments.ordinal) AS segment_rank "
                "FROM current_preparation AS current "
                "JOIN preparation_items AS items ON items.run_id = current.run_id "
                "JOIN transcription_revisions AS revisions "
                "ON revisions.revision_id = items.transcript_revision_id "
                "JOIN transcript_segments AS segments "
                "ON segments.revision_id = revisions.revision_id "
                "WHERE current.slot = 1 AND items.status = 'transcribed') "
                "SELECT * FROM ranked WHERE segment_rank <= ? "
                "ORDER BY version_id, stream_index, segment_rank LIMIT ?",
                (_MAX_TRANSCRIPTS_PER_STREAM, _MAX_TRANSCRIPTS + 1),
            ).fetchall()
            material_annotation_rows = database.execute(
                "SELECT version_id,revision,people_json,location,trip_day,day_part,note,usage "
                "FROM material_annotations"
            ).fetchall()
            transcript_annotation_rows = database.execute(
                "SELECT version_id,stream_index,source_revision_id,revision,confirmed,discarded,corrections_json "
                "FROM transcript_annotations"
            ).fetchall()
            automatic_annotations = annotation_suggestions(database)

        observations = {
            row["item_key"]: _safe_json(row["result_json"], "畫面觀察")
            for row in observation_rows
        }
        material_annotations = {
            row["version_id"]: self._material_annotation(row)
            for row in material_annotation_rows
        }
        for version_id, suggestion in automatic_annotations.items():
            if version_id not in material_annotations:
                material_annotations[version_id] = {
                    "people": [],
                    "location": suggestion.get("location"),
                    "trip_day": suggestion.get("trip_day"),
                    "day_part": suggestion.get("day_part"),
                    "note": "",
                    "usage": "normal",
                    "automatic": True,
                }
        transcript_annotations = {
            (row["version_id"], row["stream_index"]): self._transcript_annotation(row)
            for row in transcript_annotation_rows
        }
        transcripts: dict[str, list[dict[str, object]]] = defaultdict(list)
        review_versions: set[str] = set()
        review_count = 0
        for row in transcript_rows[:_MAX_TRANSCRIPTS]:
            annotation = transcript_annotations.get((row["version_id"], row["stream_index"]))
            annotation_current = bool(
                annotation
                and annotation["source_revision_id"] == row["revision_id"]
            )
            confirmed = bool(annotation_current and annotation["confirmed"])
            material = material_annotations.get(row["version_id"], {})
            if material.get("usage") == "ambient_only" or (
                annotation and annotation["discarded"]
            ):
                continue
            if row["review_required"] and not confirmed:
                review_count += 1
                review_versions.add(row["version_id"])
                continue
            source = SourceSpan.from_storage(
                _safe_json(row["source_span_json"], "逐字稿來源")
            )
            transcripts[row["version_id"]].append(
                {
                    "span": SourceSpan(
                        row["version_id"],
                        row["stream_index"],
                        row["start_pts"],
                        row["end_pts"],
                        source.time_base,
                    ),
                    "text": (
                        annotation["corrections"].get(row["ordinal"], row["text"])
                        if annotation_current else row["text"]
                    ),
                }
            )

        priority_groups: list[list[PlanningCandidate]] = []
        candidate_groups: list[list[PlanningCandidate]] = []
        reference_notes: list[str] = []
        source_ordinal = 0
        for row in result_rows:
            result = _safe_json(row["result_json"], "畫面證據")
            if result.get("error_code") or not isinstance(result.get("source"), dict):
                continue
            span = SourceSpan.from_storage(result["source"])
            observation = observations.get(row["item_key"])
            if result.get("role") == "reference":
                if observation is not None:
                    summary = observation.get("style_summary")
                    if isinstance(summary, str) and summary.strip():
                        reference_notes.append(summary[:1_000])
                continue
            if result.get("role") != "source" or result.get("kind") not in {"video", "audio"}:
                continue
            material_annotation = material_annotations.get(span.version_id, {})
            if material_annotation.get("usage") == "exclude":
                continue
            source_ordinal += 1
            label = f"毛片 {source_ordinal:03d}"
            trusted = transcripts.get(span.version_id, [])
            group: list[PlanningCandidate] = []
            for item in trusted:
                candidate_span = item["span"]
                text = str(item["text"])[:500]
                if not text.strip():
                    continue
                group.append(
                    PlanningCandidate(
                        _candidate_id(row["item_key"], candidate_span, "transcript"),
                        candidate_span,
                        label,
                        str(result["kind"]),
                        self._with_annotation(
                            "來源時間已由本機逐字稿對齊；文字已通過自動檢查或由使用者核對。",
                            material_annotation,
                        ),
                        TranscriptReliability.TRUSTED,
                        text,
                    )
                )
            visual_candidates = self._visual_candidates(result, observation, span)
            for candidate_span, summary in visual_candidates:
                group.append(
                    PlanningCandidate(
                        _candidate_id(row["item_key"], candidate_span, "visual"),
                        candidate_span,
                        label,
                        str(result["kind"]),
                        self._with_annotation(summary, material_annotation),
                        (
                            TranscriptReliability.NEEDS_REVIEW
                            if span.version_id in review_versions
                            else TranscriptReliability.UNAVAILABLE
                        ),
                    )
                )
            if group:
                if material_annotation.get("usage") == "must_use":
                    priority_groups.append(group)
                else:
                    candidate_groups.append(group)

        candidate_groups = priority_groups + candidate_groups
        candidate_count = sum(len(group) for group in candidate_groups)
        candidates = self._round_robin(candidate_groups, _MAX_CANDIDATES)
        if not candidates:
            raise ProjectError(
                "planning_candidates_empty",
                "目前沒有可建立剪輯計畫的來源片段。",
                status_code=409,
            )
        limitations = []
        if review_count:
            limitations.append(
                f"有 {review_count} 段逐字稿需要核對，未將其文字提供給 AI；請以畫面證據規劃。"
            )
        if len(transcript_rows) > _MAX_TRANSCRIPTS:
            limitations.append("可信逐字稿片段超過本次上限，已限制每支素材的取樣數。")
        if candidate_count > _MAX_CANDIDATES:
            limitations.append(
                f"候選片段超過本次上限，已跨素材從 {candidate_count} 段均衡縮減為 256 段。"
            )
        analysis_identity = json.dumps(
            {
                "evidence_run_id": evidence["run_id"],
                "preparation": [
                    [row["version_id"], row["stream_index"], row["start_pts"], row["end_pts"], row["revision_id"]]
                    for row in transcript_rows[:_MAX_TRANSCRIPTS]
                ],
                "material_annotations": [
                    [row["version_id"], row["revision"]]
                    for row in material_annotation_rows
                ],
                "transcript_annotations": [
                    [row["version_id"], row["stream_index"], row["revision"]]
                    for row in transcript_annotation_rows
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return PlanningContext(
            project_id,
            project["brief"],
            inventory["run_id"],
            "analysis-" + hashlib.sha256(analysis_identity).hexdigest()[:32],
            tuple(candidates),
            tuple(dict.fromkeys(reference_notes)),
            tuple(limitations),
        )

    @staticmethod
    def _material_annotation(row: object) -> dict[str, object]:
        people = json.loads(row["people_json"])
        if not isinstance(people, list) or any(not isinstance(item, str) for item in people):
            raise ProjectError("storage_corrupt", "Clipen 無法讀取素材標註。", status_code=500)
        return {
            "people": people,
            "location": row["location"],
            "trip_day": row["trip_day"],
            "day_part": row["day_part"],
            "note": row["note"],
            "usage": row["usage"],
        }

    @staticmethod
    def _transcript_annotation(row: object) -> dict[str, object]:
        raw = json.loads(row["corrections_json"])
        if not isinstance(raw, list):
            raise ProjectError("storage_corrupt", "Clipen 無法讀取逐字稿修正。", status_code=500)
        corrections: dict[int, str] = {}
        for item in raw:
            if (
                not isinstance(item, dict)
                or set(item) != {"ordinal", "text"}
                or isinstance(item["ordinal"], bool)
                or not isinstance(item["ordinal"], int)
                or not isinstance(item["text"], str)
                or item["ordinal"] in corrections
            ):
                raise ProjectError("storage_corrupt", "Clipen 無法讀取逐字稿修正。", status_code=500)
            corrections[item["ordinal"]] = item["text"]
        return {
            "source_revision_id": row["source_revision_id"],
            "confirmed": bool(row["confirmed"]),
            "discarded": bool(row["discarded"]),
            "corrections": corrections,
        }

    @staticmethod
    def _with_annotation(summary: str, annotation: dict[str, object]) -> str:
        details = []
        if annotation.get("trip_day"):
            details.append(f"旅行第 {annotation['trip_day']} 天")
        if annotation.get("day_part"):
            details.append(_DAY_PART_LABELS.get(str(annotation["day_part"]), str(annotation["day_part"])))
        if annotation.get("location"):
            details.append(f"地點：{annotation['location']}")
        people = annotation.get("people")
        if isinstance(people, list) and people:
            details.append("人物：" + "、".join(str(item) for item in people))
        if annotation.get("usage") == "must_use":
            details.append("使用者要求優先使用")
        if annotation.get("usage") == "ambient_only":
            details.append("只使用畫面或環境音，不採用口語內容")
        if annotation.get("note"):
            details.append("使用者備註：" + str(annotation["note"]))
        if annotation.get("automatic") and details:
            details.append("旅行時間與地點來自本機素材 metadata，使用者尚未核對")
        return (summary + ("；" + "；".join(details) if details else ""))[:2_000]

    @staticmethod
    def _round_robin(
        groups: list[list[PlanningCandidate]], limit: int
    ) -> list[PlanningCandidate]:
        selected: list[PlanningCandidate] = []
        depth = 0
        while len(selected) < limit:
            added = False
            for group in groups:
                if depth < len(group):
                    selected.append(group[depth])
                    added = True
                    if len(selected) == limit:
                        break
            if not added:
                break
            depth += 1
        return selected

    @staticmethod
    def _visual_candidates(
        result: dict[str, Any],
        observation: dict[str, Any] | None,
        span: SourceSpan,
    ) -> tuple[tuple[SourceSpan, str], ...]:
        if observation is None:
            return tuple(
                (window, "本機已確認來源範圍，但沒有經使用者授權的 AI 畫面描述。")
                for window in _fallback_windows(span)
            )
        frames = observation.get("frame_evidence")
        descriptions = observation.get("observations")
        if not isinstance(frames, list) or not isinstance(descriptions, list):
            return tuple(
                (window, "畫面觀察內容不完整，僅能依來源順序規劃。")
                for window in _fallback_windows(span)
            )
        candidates = []
        for description in descriptions[:_MAX_WINDOWS_PER_STREAM]:
            if not isinstance(description, dict):
                continue
            index = description.get("index")
            text = description.get("description")
            uncertainty = description.get("uncertainty")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(frames)
                or not isinstance(frames[index], dict)
                or not isinstance(text, str)
                or not text.strip()
            ):
                continue
            try:
                candidate_span = _window(span, int(str(frames[index]["pts"])))
            except (KeyError, TypeError, ValueError):
                continue
            summary = text[:1_500]
            if isinstance(uncertainty, str) and uncertainty.strip():
                summary += f" 不確定處：{uncertainty[:400]}"
            candidates.append((candidate_span, summary))
        return tuple(candidates) or tuple(
            (window, "畫面觀察未提供可定位描述，僅能依來源順序規劃。")
            for window in _fallback_windows(span)
        )
