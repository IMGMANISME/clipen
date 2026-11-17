"""Project-owned audio evidence, exact source sampling and local mix execution."""

import json
import math
import os
import secrets
from fractions import Fraction
from pathlib import Path

from clipen.domain import MediaClip, TrackKind
from clipen.finishing.audio import open_asset, run_process
from clipen.finishing.models import digest, encoded
from clipen.projects import ProjectError


def atomic_json(path, value):
    if path.is_symlink():
        raise ProjectError("storage_unsafe", "製作檔案不安全。", status_code=409)
    temporary = path.parent / (".write-" + secrets.token_hex(16))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(encoded(value))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AgentAudioStore:
    def __init__(self, projects, project_id):
        self.directory = projects.root / project_id / "agent-audio"
        if self.directory.is_symlink():
            raise ProjectError("storage_unsafe", "音訊目錄不安全。", status_code=409)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.manifest = self.directory / "assets.json"

    def records(self):
        if self.manifest.is_symlink():
            raise ProjectError("storage_unsafe", "音訊索引不安全。", status_code=409)
        if not self.manifest.exists():
            return []
        if self.manifest.stat().st_size > 8 * 1024 * 1024:
            raise ProjectError("storage_corrupt", "音訊索引超過上限。", status_code=409)
        return json.loads(self.manifest.read_text())

    def add(self, assets, reference):
        records = self.records()
        known = {r["asset"]["asset_id"] for r in records}
        records.extend({"asset": a, "reference": reference} for a in assets if a["asset_id"] not in known)
        if len(records) > 48:
            raise ProjectError("agent_audio_limit", "這個作品已達 48 個音訊候選上限。", status_code=409)
        atomic_json(self.manifest, records)

    def public_assets(self):
        return [
            {k: v for k, v in r["asset"].items() if k not in {"relative_path", "fingerprint", "mtime_ns", "size_bytes"}}
            for r in self.records()
        ]

    def context(self, asset_id):
        record = next((r for r in self.records() if r["asset"]["asset_id"] == asset_id), None)
        if record is None:
            raise ProjectError("agent_audio_missing", "找不到這個作品已取得的音訊。", status_code=404)
        asset = record["asset"]
        reference = record["reference"]
        path = Path(reference["canonical_path"])
        if path.parent != self.directory or path.is_symlink():
            raise ProjectError("storage_unsafe", "音訊來源不在本作品的管理範圍。", status_code=409)
        return {"assets": [asset], "directories": {asset["role"]: reference}}


def sample_frames(media, project_id, span, offsets, directory, cancel):
    source = media.open(project_id, span.version_id)
    paths = []
    manifest = []
    try:
        if span.stream_index not in source.video_stream_indices:
            raise ProjectError("agent_video_missing", "指定來源沒有這個影像串流。", status_code=409)
        unit = Fraction(span.time_base.numerator, span.time_base.denominator)
        for offset in offsets:
            seconds = int(span.start_pts) * unit + Fraction(str(offset))
            path = directory / ("frame-" + secrets.token_hex(12) + ".png")
            run_process(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-seek_timestamp",
                    "1",
                    "-ss",
                    f"{float(seconds):.9f}",
                    "-i",
                    f"/dev/fd/{source.descriptor}",
                    "-map",
                    f"0:{span.stream_index}",
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2",
                    "-threads",
                    "1",
                    str(path),
                ],
                cancel,
                pass_fds=(source.descriptor,),
                timeout=30,
            )
            if not path.is_file() or not path.stat().st_size:
                raise ProjectError("agent_sample_missing", "取樣時間沒有可解碼畫面。", status_code=409)
            paths.append(path)
            manifest.append({"offset_seconds": offset, "source_seconds": float(seconds), "version_id": span.version_id})
    finally:
        source.close()
    return tuple(paths), manifest


def transcript_cues(projects, project_id, timeline, *, include_drafts=False):
    from .speech_quality import speech_flags

    cues = []
    limitations = []
    fps = Fraction(timeline.frame_rate.numerator, timeline.frame_rate.denominator)
    with projects.connect(project_id) as db:
        for track in timeline.tracks:
            if track.kind is not TrackKind.VIDEO:
                continue
            for clip in track.items:
                source_mode = next(
                    (e.parameters["mode"] for e in clip.effects if e.effect_id == "audio.source"), "keep"
                )
                if source_mode == "mute":
                    continue
                if any(e.effect_id == "video.speed_ramp" for e in clip.effects):
                    limitations.append({"item_id": clip.item_id, "reason": "speed_ramp_requires_new_transcription"})
                    continue
                span = clip.source.span
                rows = db.execute(
                    "SELECT tr.*,pi.review_required FROM transcription_revisions tr JOIN preparation_items pi ON pi.transcript_revision_id=tr.revision_id JOIN current_preparation cp ON cp.run_id=pi.run_id WHERE tr.version_id=? AND tr.status IN ('transcribed','needs_review') AND tr.stream_index=(SELECT MIN(stream_index) FROM media_streams WHERE version_id=tr.version_id AND kind='audio') ORDER BY tr.stream_index",
                    (span.version_id,),
                ).fetchall()
                if not rows:
                    continue
                row = rows[0]
                annotation = db.execute(
                    "SELECT * FROM transcript_annotations WHERE version_id=? AND stream_index=? AND source_revision_id=?",
                    (span.version_id, row["stream_index"], row["revision_id"]),
                ).fetchone()
                if annotation and annotation["discarded"]:
                    continue
                needs_review = bool(
                    (row["review_required"] or row["status"] == "needs_review")
                    and not (annotation and annotation["confirmed"])
                )
                corrections = (
                    {}
                    if annotation is None
                    else {str(c["ordinal"]): c["text"] for c in json.loads(annotation["corrections_json"])}
                )
                src = json.loads(row["source_span_json"])
                tb = Fraction(src["time_base"]["num"], src["time_base"]["den"])
                unit = Fraction(span.time_base.numerator, span.time_base.denominator)
                start, end = int(span.start_pts) * unit, int(span.end_pts) * unit
                for seg in db.execute(
                    "SELECT * FROM transcript_segments WHERE revision_id=? ORDER BY ordinal", (row["revision_id"],)
                ):
                    left, right = int(seg["start_pts"]) * tb, int(seg["end_pts"]) * tb
                    if right <= start or left >= end:
                        continue
                    complete_span = {
                        "version_id": span.version_id,
                        "stream_index": span.stream_index,
                        "start_pts": str(math.floor(left / unit)),
                        "end_pts": str(math.ceil(right / unit)),
                        "time_base": span.time_base.to_storage(),
                    }
                    # Video cuts are quantized to frames while ASR uses audio
                    # samples. A trailing AAC/sample remainder below one frame
                    # is not a cut-off utterance; clamp displayed timing only.
                    tolerance = 1 / fps
                    partial = left < start - tolerance or right > end + tolerance
                    if needs_review:
                        limitations.append(
                            {
                                "item_id": clip.item_id,
                                "reason": "transcript_needs_review",
                                "segment_id": seg["segment_id"],
                                "provisional_text": corrections.get(str(seg["ordinal"]), seg["text"]),
                                "source_revision_id": row["revision_id"],
                                "source_start_seconds": float(left),
                                "source_end_seconds": float(right),
                                "complete_source_span": complete_span,
                                "partial": partial,
                                "policy": "Low-confidence ASR. Explicit include_drafts/allow_asr_draft can produce a labelled draft; never claim human confirmation.",
                            }
                        )
                        if not include_drafts or partial:
                            continue
                    if partial:
                        limitations.append(
                            {
                                "item_id": clip.item_id,
                                "reason": "partial_utterance",
                                "segment_id": seg["segment_id"],
                                "text": corrections.get(str(seg["ordinal"]), seg["text"]),
                                "complete_source_span": {
                                    "version_id": span.version_id,
                                    "stream_index": span.stream_index,
                                    "start_pts": str(math.floor(left / unit)),
                                    "end_pts": str(math.ceil(right / unit)),
                                    "time_base": span.time_base.to_storage(),
                                },
                                "repair": "Use replace_clip_source with this complete_source_span; inspect the expanded shot, then rebuild subtitles. Alternatively remove/mute this speech for an explicit editorial reason.",
                            }
                        )
                        continue
                    begin = max(clip.start_frame, clip.start_frame + math.floor((left - start) * fps))
                    finish = min(
                        clip.start_frame + clip.duration_frames, clip.start_frame + math.ceil((right - start) * fps)
                    )
                    cues.append(
                        {
                            "cue_id": digest([clip.item_id, row["revision_id"], seg["segment_id"]])[:24],
                            "item_id": clip.item_id,
                            "start_frame": begin,
                            "duration_frames": finish - begin,
                            "text": corrections.get(str(seg["ordinal"]), seg["text"]),
                            "segment_id": seg["segment_id"],
                            "source_revision_id": row["revision_id"],
                            "verification": "asr_draft" if needs_review else "reviewed_source",
                            "confidence": seg["confidence"],
                            "quality_flags": speech_flags(corrections.get(str(seg["ordinal"]), seg["text"]))
                            if needs_review
                            else [],
                            "source": complete_span,
                            "boundary_adjustment_seconds": float(max(start - left, 0) + max(right - end, 0)),
                        }
                    )
    return sorted(cues, key=lambda c: c["start_frame"]), limitations


PRODUCTION_EFFECTS = {
    "video.punch_in",
    "video.slow_push",
    "video.reframe",
    "video.speed_ramp",
    "video.grade",
    "video.mosaic",
    "video.transition",
    "video.fade",
    "audio.source",
    "audio.layers",
}


def validate_production_timeline(timeline, *, max_texts=96):
    issues = []
    cursor = 0
    video = next((t for t in timeline.tracks if t.kind is TrackKind.VIDEO), None)
    if video is None:
        return [{"code": "video_required", "message": "至少需要一個畫面軌。"}]
    if not video.items:
        return [{"code": "video_required", "message": "空白草稿尚無可渲染畫面；請先加入來源片段。"}]
    if len([t for t in timeline.tracks if t.kind is TrackKind.VIDEO]) != 1:
        issues.append({"code": "multiple_video_tracks_unsupported"})
    if any(t.items for t in timeline.tracks if t.kind is TrackKind.GRAPHIC):
        issues.append({"code": "graphics_delivery_unsupported"})
    for i in video.items:
        if i.start_frame != cursor:
            issues.append({"code": "video_gap_or_overlap", "item_id": i.item_id})
        cursor = i.start_frame + i.duration_frames
    if cursor != timeline.duration_frames:
        issues.append({"code": "video_end_mismatch"})
    texts = []
    for t in timeline.tracks:
        for i in t.items:
            if i.start_frame + i.duration_frames > timeline.duration_frames:
                issues.append({"code": "item_out_of_bounds", "item_id": i.item_id})
            if t.kind is TrackKind.TITLE:
                texts.append(i)
            if isinstance(i, MediaClip):
                for e in i.effects:
                    if e.effect_id not in PRODUCTION_EFFECTS:
                        issues.append({"code": "unsupported_effect", "item_id": i.item_id, "effect_id": e.effect_id})
    for n, a in enumerate(texts):
        for b in texts[n + 1 :]:
            if (
                a.style_id == b.style_id
                and a.start_frame < b.start_frame + b.duration_frames
                and b.start_frame < a.start_frame + a.duration_frames
            ):
                issues.append(
                    {
                        "code": "text_layout_collision",
                        "items": [a.item_id, b.item_id],
                        "message": "相同位置文字同時顯示；請調整時間或樣式。",
                    }
                )
    if len(texts) > max_texts:
        issues.append({"code": "text_limit_96" if max_texts == 96 else "production_text_limit"})
    return issues


def mix_audio_layers(media, project_id, timeline, base, output, cancel):
    if timeline.production_session_id is not None:
        from .program_audio import mix_program_audio

        return mix_program_audio(media, project_id, timeline, base, output, cancel)
    store = AgentAudioStore(media.projects, project_id)
    fps = timeline.frame_rate.numerator / timeline.frame_rate.denominator
    seconds = timeline.duration_frames / fps
    layers = [
        (i, l)
        for t in timeline.tracks
        for i in t.items
        if isinstance(i, MediaClip)
        for e in i.effects
        if e.effect_id == "audio.layers"
        for l in e.parameters["layers"]
    ]
    if len(layers) > 24:
        raise ProjectError("agent_audio_limit", "單次輸出最多 24 個混音片段。", status_code=409)
    command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-filter_complex_threads", "2", "-i", str(base)]
    opened = []
    filters = ["[0:a]anull[original]"]
    labels = ["[original]"]
    try:
        for index, (clip, layer) in enumerate(layers, 1):
            context = store.context(layer["asset_id"])
            fd, asset = open_asset(context, layer["asset_id"])
            opened.append(fd)
            duration = layer["duration_frames"] / fps
            start = (clip.start_frame + layer["start_frame"]) / fps
            if start + duration > seconds or layer["source_offset_seconds"] + duration > asset["duration_seconds"]:
                raise ProjectError("agent_audio_out_of_bounds", "混音片段超出影片或來源長度。", status_code=409)
            command += ["-ss", str(layer["source_offset_seconds"]), "-t", str(duration), "-i", f"/dev/fd/{fd}"]
            fade = min(layer["fade_seconds"], duration / 2)
            chain = f"[{index}:{asset['stream_index']}]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,volume={layer['gain_db']:.4f}dB"
            if fade:
                chain += f",afade=t=in:d={fade:.6f},afade=t=out:st={duration - fade:.6f}:d={fade:.6f}"
            chain += f",adelay={round(start * 1000)}|{round(start * 1000)}[layer{index}]"
            filters.append(chain)
            if layer["duck"]:
                filters.append(f"[0:a]anull[side{index}]")
                filters.append(
                    f"[layer{index}][side{index}]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=350[duck{index}]"
                )
                labels.append(f"[duck{index}]")
            else:
                labels.append(f"[layer{index}]")
        filters.append(
            "".join(labels)
            + f"amix=inputs={len(labels)}:duration=first:normalize=0,alimiter=limit=0.841395:level=0[aout]"
        )
        command += [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-t",
            str(seconds),
            "-movflags",
            "+faststart",
            str(output),
        ]
        run_process(command, cancel, pass_fds=tuple(opened), timeout=300)
    finally:
        for fd in opened:
            os.close(fd)


def text_style_path(projects, project_id):
    return projects.root / project_id / "agent-text-styles.json"


def load_text_styles(projects, project_id):
    if projects is None:
        return {}
    path = text_style_path(projects, project_id)
    if path.is_symlink():
        raise ProjectError("storage_unsafe", "文字樣式檔案不安全。", status_code=409)
    if not path.exists():
        return {}
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ProjectError("storage_corrupt", "文字樣式超出上限。", status_code=409)
    return json.loads(path.read_text())


def source_rows(projects, project_id):
    """The live inventory is the authority; prior planning windows are not a source boundary."""
    with projects.connect(project_id) as db:
        rows = db.execute(
            "SELECT i.version_id,i.display_name,s.stream_index,s.kind,s.details_json,v.probe_json FROM current_inventory c JOIN inventory_runs r ON r.run_id=c.run_id JOIN inventory_items i ON i.run_id=r.run_id JOIN assets a ON a.asset_id=i.asset_id JOIN asset_versions v ON v.version_id=i.version_id JOIN media_streams s ON s.version_id=i.version_id WHERE c.slot=1 AND r.state='succeeded' AND a.role='source' AND i.status='usable' AND s.kind IN ('video','audio') ORDER BY i.display_name,i.version_id,s.stream_index LIMIT 10001"
        ).fetchall()
    if len(rows) > 10000:
        raise ProjectError("agent_source_limit", "素材串流超過上限，請拆分作品。", status_code=409)
    result = []
    for row in rows:
        details = json.loads(row["details_json"])
        probe = json.loads(row["probe_json"] or "{}")
        if details.get("attached_pic") or details.get("timed_thumbnails"):
            continue
        try:
            base = Fraction(details["time_base"])
            start = int(details.get("start_pts", 0))
            duration = (
                int(details["duration_ts"])
                if "duration_ts" in details
                else int(Fraction(str(probe["duration_seconds"])) / base)
            )
            if duration <= 0 or base <= 0:
                continue
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            continue
        result.append(
            {
                "version_id": row["version_id"],
                "display_name": row["display_name"],
                "stream_index": row["stream_index"],
                "kind": row["kind"],
                "start_pts": str(start),
                "end_pts": str(start + duration),
                "time_base": {"num": base.numerator, "den": base.denominator},
                "duration_seconds": float(duration * base),
            }
        )
    return result
