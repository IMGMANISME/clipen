"""A source-bound episode treatment; never a synthetic aesthetic score."""

import json

from clipen.domain import TrackKind
from clipen.finishing.models import digest
from clipen.projects import ProjectError

from .agent_models import ToolResult
from .production_media import atomic_json


def cut_signature(timeline):
    return digest(
        [
            {
                "item_id": i.item_id,
                "start_frame": i.start_frame,
                "duration_frames": i.duration_frames,
                "source": i.source.span.to_storage(),
                "source_audio_muted": any(
                    e.effect_id == "audio.source" and e.parameters["mode"] == "mute" for e in i.effects
                ),
            }
            for t in timeline.tracks
            if t.kind is TrackKind.VIDEO
            for i in t.items
        ]
    )


def read_episode(projects, project_id, run_id, timeline):
    base = projects.root / project_id / "agent-production"
    path = base / run_id / "analysis" / "episode.json"
    if any(p.is_symlink() for p in (base, base / run_id, base / run_id / "analysis", path)):
        raise ProjectError("storage_unsafe", "節目規劃路徑不安全。", status_code=409)
    if not path.is_file():
        return None
    if path.stat().st_size > 128000:
        raise ProjectError("storage_corrupt", "節目規劃超過上限。", status_code=409)
    value = json.loads(path.read_text())
    return value if value["cut_signature"] == cut_signature(timeline) else None


def plan_episode(tools, args):
    clips = {i.item_id: i for i in tools._video().items}
    cursor = 0
    beat_ids = []
    cues = {c["cue_id"]: c for c in tools._production_list_transcripts({"include_drafts": True}).payload["cues"]}
    for beat in args["beats"]:
        if beat["beat_id"] in beat_ids:
            raise ProjectError("episode_invalid", "節目段落 ID 不可重複。", status_code=422)
        beat_ids.append(beat["beat_id"])
        if any(i not in clips for i in beat["item_ids"]):
            raise ProjectError("episode_invalid", "節目段落引用了不在目前剪輯的鏡頭。", status_code=422)
        selected = [clips[i] for i in beat["item_ids"]]
        explicit = "start_frame" in beat or "duration_frames" in beat
        if explicit and not {"start_frame", "duration_frames"} <= set(beat):
            raise ProjectError("episode_invalid", "段內敘事時間需要同時提供起點與時長。", status_code=422)
        left = beat["start_frame"] if explicit else selected[0].start_frame
        right = left + beat["duration_frames"] if explicit else selected[-1].start_frame + selected[-1].duration_frames
        overlapping = [
            i.item_id for i in clips.values() if i.start_frame < right and left < i.start_frame + i.duration_frames
        ]
        if left != cursor or right > tools._timeline().duration_frames or overlapping != beat["item_ids"]:
            raise ProjectError(
                "episode_invalid",
                "段落需依序覆蓋時間軸，並引用該範圍內的所有鏡頭；同一連續鏡頭可用明確時間分成不同敘事段落。",
                status_code=422,
            )
        for cue_id in beat["dialogue_ids"]:
            if (
                cue_id not in cues
                or cues[cue_id]["item_id"] not in beat["item_ids"]
                or not (
                    cues[cue_id]["start_frame"] < right
                    and left < cues[cue_id]["start_frame"] + cues[cue_id]["duration_frames"]
                )
            ):
                raise ProjectError("episode_invalid", "對話證據必須來自這個段落實際保留的完整口語。", status_code=422)
        cursor = right
    if cursor != tools._timeline().duration_frames:
        raise ProjectError("episode_invalid", "每個鏡頭都需要明確的節目用途，不可遺漏或重複歸類。", status_code=422)
    value = {"authority": "model_authored", "cut_signature": cut_signature(tools._timeline()), **args}
    atomic_json(tools._workdir("analysis") / "episode.json", value)
    return ToolResult(
        {
            "episode": value,
            "review_scope": "Source links and treatment only; semantic causality and entertainment value still need review.",
        }
    )
