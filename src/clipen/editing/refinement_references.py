"""User-owned, source-anchored review notes for incremental AI revisions.

A note marks a moment the person paused at while watching (at_offset_frames),
the range it covers inside one shot, and optionally the caption or programme
component they pointed at. Location facts stay for later rounds; every other
note belongs to the round it was sent with.
"""

import hashlib
import json
import math
from fractions import Fraction

from clipen.domain import TrackKind
from clipen.projects import ProjectError
from clipen.projects.models import validate_request_id
from clipen.projects.storage import command_digest, utc_now
from clipen.rendering.renderer import FfmpegPreviewRenderer

from .agent_models import bounded_text, encoded

KINDS = {"change", "remove", "keep", "highlight", "location", "audio", "note"}
LASTING = {"location"}
_OPTIONAL = {"source_gain_db", "fill_music", "audio_layer_id", "at_offset_frames", "explicit_range", "element_id"}


def references(intent):
    return intent.get("user_references", {"version": 0, "items": []})


def source_pts(clip, fps, offset):
    """Map a cut-relative frame boundary through the native speed ramp."""
    span = clip.source.span
    unit = Fraction(span.time_base.numerator, span.time_base.denominator)
    duration = span.duration_pts * unit
    elapsed = Fraction(offset, 1) / fps
    speed = FfmpegPreviewRenderer._speed_profile(clip)
    if speed:
        outside, center = map(lambda rate: Fraction(str(rate)), speed)
        first = duration * Fraction(2, 5) / outside
        middle = duration * Fraction(1, 5) / center
        if elapsed < first:
            elapsed *= outside
        elif elapsed < first + middle:
            elapsed = duration * Fraction(2, 5) + (elapsed - first) * center
        else:
            elapsed = duration * Fraction(3, 5) + (elapsed - first - middle) * outside
    return Fraction(int(span.start_pts)) + min(duration, max(Fraction(0), elapsed)) / unit


def locate(reference, timeline):
    """Only claim a current position when the anchored source cut still matches."""
    for track in timeline.tracks:
        if track.kind is not TrackKind.VIDEO:
            continue
        for clip in track.items:
            if (
                clip.item_id == reference["item_id"]
                and clip.source.to_payload() == reference["clip_source"]
                and clip.duration_frames == reference["clip_duration_frames"]
                and timeline.frame_rate.to_storage() == reference["frame_rate"]
                and list(FfmpegPreviewRenderer._speed_profile(clip) or ()) == reference["speed_profile"]
            ):
                layer = reference.get("audio_layer_id")
                if layer and not any(
                    l["layer_id"] == layer
                    for effect in clip.effects
                    if effect.effect_id == "audio.layers"
                    for l in effect.parameters["layers"]
                ):
                    return None
                located = {
                    "item_id": clip.item_id,
                    "start_frame": clip.start_frame + reference["start_offset_frames"],
                    "duration_frames": reference["duration_frames"],
                    "at_frame": clip.start_frame + reference.get("at_offset_frames", reference["start_offset_frames"]),
                }
                element = reference.get("element_id")
                if element:
                    located["element_present"] = any(
                        item.item_id == element for t in timeline.tracks for item in t.items
                    )
                return located
    return None


def sent(intent):
    """What the last revision request handed to the AI, for review."""
    request = intent.get("latest_revision_request")
    if not request:
        return None
    return {
        "instruction": request.get("instruction"),
        "created_at": request.get("created_at"),
        "items": request.get("references", {}).get("items", []),
    }


def hand_over(intent):
    """Close the round: only lasting facts stay for the next one."""
    state = references(intent)
    kept = [item for item in state["items"] if item["kind"] in LASTING]
    if len(kept) != len(state["items"]):
        intent["user_references"] = {"version": state["version"] + 1, "items": kept}


def public_state(intent, timeline):
    state = references(intent)
    return {**state, "items": [{**item, "current_range": locate(item, timeline)} for item in state["items"]]}


def update(
    repository, project_id, run_id, expected_revision_id, expected_version, mutation_id, reference=None, delete_id=None
):
    run_id = repository._validate_run_id(run_id)
    mutation_id = validate_request_id(mutation_id)
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise ProjectError("reference_invalid", "參考註記版本無效。", status_code=422)
    if (reference is None) == (delete_id is None):
        raise ProjectError("reference_invalid", "請指定一筆要儲存或刪除的註記。", status_code=422)
    request_hash = command_digest(
        {
            "run_id": run_id,
            "revision": expected_revision_id,
            "version": expected_version,
            "reference": reference,
            "delete_id": delete_id,
        }
    )
    event_id = "reference_" + hashlib.sha256(mutation_id.encode()).hexdigest()[:32]
    with repository.projects.connect(project_id) as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT payload_json FROM operation_events WHERE event_id=?", (event_id,)).fetchone()
        if prior:
            payload = json.loads(prior["payload_json"])
            if payload["request_hash"] != request_hash:
                raise ProjectError("request_reused", "操作識別已用於其他註記。", status_code=409)
            return payload["result"]
        run = repository._read_run(db, run_id)
        latest = db.execute("SELECT run_id FROM edit_agent_runs ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
        if (
            latest["run_id"] != run_id
            or db.execute("SELECT 1 FROM edit_agent_runs WHERE state IN ('queued','running')").fetchone()
        ):
            raise ProjectError("edit_agent_busy", "請先停止 AI 製作，再修改參考註記。", status_code=409)
        if run.current_revision_id != expected_revision_id:
            raise ProjectError("revision_conflict", "影片已更新，請重新讀取後標記。", status_code=409)
        row = db.execute("SELECT intent_json FROM production_sessions WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ProjectError("production_required", "這個版本不支援參考註記。", status_code=422)
        intent = json.loads(row["intent_json"])
        state = references(intent)
        if state["version"] != expected_version:
            raise ProjectError("reference_conflict", "註記已在其他視窗更新，請重新讀取後再修改。", status_code=409)
        revision = repository._read_revision(db, run.current_revision_id)
        items = list(state["items"])
        if delete_id is not None:
            if not isinstance(delete_id, str) or not any(item["reference_id"] == delete_id for item in items):
                raise ProjectError("reference_not_found", "找不到這筆註記。", status_code=404)
            items = [item for item in items if item["reference_id"] != delete_id]
        else:
            item = normalize(reference, revision)
            old = next((r for r in items if r["reference_id"] == item["reference_id"]), None)
            if old:
                items = [item if r["reference_id"] == item["reference_id"] else r for r in items]
            else:
                if len(items) >= 100:
                    raise ProjectError("reference_limit", "每支影片最多保留 100 筆參考註記。", status_code=422)
                items.append(item)
        intent["user_references"] = {"version": state["version"] + 1, "items": items}
        db.execute("UPDATE production_sessions SET intent_json=? WHERE run_id=?", (encoded(intent), run_id))
        result = public_state(intent, revision.timeline)
        payload = {"authority": "user", "run_id": run_id, "request_hash": request_hash, "result": result}
        db.execute(
            "INSERT INTO operation_events(event_id,project_id,operation_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (event_id, project_id, mutation_id, "production_reference_changed", encoded(payload), utc_now()),
        )
        db.commit()
        return result


def normalize(value, revision):
    required = {"reference_id", "kind", "item_id", "start_offset_frames", "duration_frames", "note"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - _OPTIONAL:
        raise ProjectError("reference_invalid", "參考註記欄位無效。", status_code=422)
    identifier = validate_request_id(value["reference_id"])
    kind = value["kind"]
    if not isinstance(kind, str) or kind not in KINDS:
        raise ProjectError("reference_invalid", "請選擇註記類型。", status_code=422)
    # "Remove this" and "this is fine" say enough on their own.
    note = bounded_text(value["note"], "參考說明", 2000, empty=kind in {"remove", "keep"})
    clip = next(
        (
            item
            for track in revision.timeline.tracks
            if track.kind is TrackKind.VIDEO
            for item in track.items
            if item.item_id == value["item_id"]
        ),
        None,
    )
    if clip is None:
        raise ProjectError("reference_invalid", "請選擇這支影片中的畫面片段。", status_code=422)
    start, length = value["start_offset_frames"], value["duration_frames"]
    if (
        any(isinstance(v, bool) or not isinstance(v, int) for v in (start, length))
        or start < 0
        or length < 1
        or start + length > clip.duration_frames
    ):
        raise ProjectError("reference_invalid", "標記範圍必須在選定片段內。", status_code=422)
    at = value.get("at_offset_frames", start)
    if isinstance(at, bool) or not isinstance(at, int) or not start <= at < start + length:
        raise ProjectError("reference_invalid", "標記的時間點必須在範圍內。", status_code=422)
    if not isinstance(value.get("explicit_range", False), bool):
        raise ProjectError("reference_invalid", "參考註記欄位無效。", status_code=422)
    item = {k: value[k] for k in required}
    item.update(at_offset_frames=at, explicit_range=value.get("explicit_range", False))
    if value.get("element_id") is not None:
        element_id = value["element_id"]
        found = (
            next(((t, i) for t in revision.timeline.tracks for i in t.items if i.item_id == element_id), None)
            if isinstance(element_id, str)
            else None
        )
        begin = clip.start_frame + start
        if (
            found is None
            or found[1].start_frame >= begin + length
            or found[1].start_frame + found[1].duration_frames <= begin
        ):
            raise ProjectError("reference_invalid", "指定的字幕或元件不在這段時間。", status_code=422)
        track, element = found
        item.update(
            element_id=element_id,
            element_track=track.track_id,
            element_text=(getattr(element, "text", None) or "")[:120] or None,
        )
    item.update(
        reference_id=identifier,
        note=note,
        authority="user",
        base_revision_id=revision.revision_id,
        clip_source=clip.source.to_payload(),
        clip_duration_frames=clip.duration_frames,
        frame_rate=revision.timeline.frame_rate.to_storage(),
        speed_profile=list(FfmpegPreviewRenderer._speed_profile(clip) or ()),
    )
    fps = Fraction(revision.timeline.frame_rate.numerator, revision.timeline.frame_rate.denominator)
    item["source_range"] = {
        **clip.source.span.to_storage(),
        "start_pts": str(math.floor(source_pts(clip, fps, start))),
        "end_pts": str(math.ceil(source_pts(clip, fps, start + length))),
    }
    if kind == "audio":
        gain = value.get("source_gain_db", -12)
        if (
            isinstance(gain, bool)
            or not isinstance(gain, (int, float))
            or not math.isfinite(gain)
            or not -60 <= gain <= 0
            or not isinstance(value.get("fill_music", True), bool)
        ):
            raise ProjectError("reference_invalid", "原音音量需介於 -60 至 0 dB。", status_code=422)
        item.update(source_gain_db=gain, fill_music=value.get("fill_music", True))
        layer = value.get("audio_layer_id")
        if layer:
            if not isinstance(layer, str) or not any(
                l["layer_id"] == layer
                for e in clip.effects
                if e.effect_id == "audio.layers"
                for l in e.parameters["layers"]
            ):
                raise ProjectError("reference_invalid", "找不到選定片段的音軌。", status_code=422)
            item["audio_layer_id"] = layer
    elif any(key in value for key in ("source_gain_db", "fill_music", "audio_layer_id")):
        raise ProjectError("reference_invalid", "只有聲音註記可以指定混音設定。", status_code=422)
    return item
