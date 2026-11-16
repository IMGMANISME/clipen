"""Check a rendered programme against its timeline before Clipen hands it over."""

from __future__ import annotations

import json

from clipen.projects import ProjectError

from .audio import run_process


def verify_output(path, timeline, cancel):
    raw = run_process(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], cancel, timeout=20
    )
    value = json.loads(raw)
    videos = [s for s in value["streams"] if s["codec_type"] == "video"]
    audios = [s for s in value["streams"] if s["codec_type"] == "audio"]
    fps = timeline.frame_rate.numerator / timeline.frame_rate.denominator
    duration = timeline.duration_frames / fps
    if (
        len(videos) != 1
        or len(audios) != 1
        or (videos[0]["width"], videos[0]["height"]) != (timeline.width, timeline.height)
    ):
        raise ProjectError("finishing_verification_failed", "成片的影像或聲音規格不完整。", status_code=409)
    from fractions import Fraction

    if (
        videos[0]["codec_name"] != "h264"
        or audios[0]["codec_name"] != "aac"
        or Fraction(videos[0]["avg_frame_rate"])
        != Fraction(timeline.frame_rate.numerator, timeline.frame_rate.denominator)
        or abs(int(videos[0].get("nb_frames", 0)) - timeline.duration_frames) > 1
    ):
        raise ProjectError(
            "finishing_verification_failed",
            f"成片的編碼、格率或格數與時間軸不一致：{videos[0]['codec_name']}/{audios[0]['codec_name']}，格率 {videos[0]['avg_frame_rate']}，格數 {videos[0].get('nb_frames', 0)}（預期 {timeline.duration_frames}）。",
            status_code=409,
        )
    if any(abs(float(s.get("duration", 0)) - duration) > max(0.1, 2 / fps) for s in (videos[0], audios[0])):
        raise ProjectError("finishing_verification_failed", "成片的影音長度與時間軸不一致。", status_code=409)
    run_process(
        ["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(path), "-f", "null", "-"], cancel, timeout=90
    )
