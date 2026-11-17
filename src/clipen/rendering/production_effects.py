"""Trusted filter builders for timed masks, transitions, grade and camera sound."""

import math

from clipen.projects import ProjectError


def mosaic_filters(filters, label, item, timeline, index):
    fps = timeline.frame_rate.numerator / timeline.frame_rate.denominator
    regions = [r for e in item.effects if e.effect_id == "video.mosaic" for r in e.parameters["regions"]]
    for n, r in enumerate(regions):
        if not all(
            isinstance(r[k], (int, float)) and not isinstance(r[k], bool) and math.isfinite(r[k])
            for k in ("x", "y", "width", "height", "block_size", "start_frame", "duration_frames")
        ):
            raise ProjectError("agent_mosaic_invalid", "馬賽克參數必須是有限數字。", status_code=422)
        if not (
            0 <= r["x"] < 1
            and 0 <= r["y"] < 1
            and 0 < r["width"] <= 1 - r["x"]
            and 0 < r["height"] <= 1 - r["y"]
            and 4 <= r["block_size"] <= 96
            and 0 <= r["start_frame"] < item.duration_frames
            and 0 < r["duration_frames"] <= item.duration_frames - r["start_frame"]
        ):
            raise ProjectError("agent_mosaic_invalid", "馬賽克區域或時間超出範圍。", status_code=422)
        width = max(2, min(timeline.width, round(timeline.width * r["width"]) // 2 * 2))
        height = max(2, min(timeline.height, round(timeline.height * r["height"]) // 2 * 2))
        x = min(timeline.width - width, round(timeline.width * r["x"]) // 2 * 2)
        y = min(timeline.height - height, round(timeline.height * r["y"]) // 2 * 2)
        small_w = max(1, math.ceil(width / r["block_size"]))
        small_h = max(1, math.ceil(height / r["block_size"]))
        stem = f"mosaic{index}_{n}"
        output = stem + "out"
        filters.append(f"[{label}]split=2[{stem}base][{stem}crop]")
        filters.append(
            f"[{stem}crop]crop={width}:{height}:{x}:{y},scale={small_w}:{small_h}:flags=area,scale={width}:{height}:flags=neighbor[{stem}pixel]"
        )
        start = r["start_frame"] / fps
        end = (r["start_frame"] + r["duration_frames"]) / fps
        filters.append(
            f"[{stem}base][{stem}pixel]overlay={x}:{y}:enable='gte(t,{start:.9f})*lt(t,{end:.9f})'[{output}]"
        )
        label = output
    return label


def grade_filters(item):
    grade = next((e.parameters for e in item.effects if e.effect_id == "video.grade"), None)
    if grade is None:
        return ""
    values = (float(grade["contrast"]), float(grade["saturation"]), float(grade["brightness"]))
    if not (0.5 <= values[0] <= 2 and 0 <= values[1] <= 2 and -0.3 <= values[2] <= 0.3):
        raise ValueError("invalid grade")
    return f",eq=contrast={values[0]:.6f}:saturation={values[1]:.6f}:brightness={values[2]:.6f}"


def transition_filters(item, following, timeline):
    fps = timeline.frame_rate.numerator / timeline.frame_rate.denominator
    incoming = next((e.parameters for e in item.effects if e.effect_id == "video.transition"), None)
    outgoing = (
        next((e.parameters for e in following.effects if e.effect_id == "video.transition"), None)
        if following
        else next((e.parameters for e in item.effects if e.effect_id == "video.transition_out"), None)
    )
    result = ""
    for kind, p in (("in", incoming), ("out", outgoing)):
        if p:
            duration = p["duration_frames"] / fps / 2
            start = 0 if kind == "in" else max(0, item.duration_frames / fps - duration)
            color = "white" if p["kind"] == "dip_white" else "black"
            result += f",fade=t={kind}:st={start:.9f}:d={duration:.9f}:color={color}"
    fade = next((e.parameters for e in item.effects if e.effect_id == "video.fade"), None)
    if fade:
        for kind, key in (("in", "fade_in_frames"), ("out", "fade_out_frames")):
            frames = fade[key]
            if isinstance(frames, bool) or not isinstance(frames, int) or frames < 0:
                raise ValueError("invalid fade")
            if frames:
                duration = frames / fps
                start = 0 if kind == "in" else item.duration_frames / fps - duration
                if start < 0:
                    raise ValueError("fade exceeds clip")
                result += f",fade=t={kind}:st={start:.9f}:d={duration:.9f}:color=black"
    return result


def camera_audio_filters(item):
    policy = next((e.parameters for e in item.effects if e.effect_id == "audio.source"), None)
    if policy is None:
        return ""
    gain = -120 if policy["mode"] == "mute" else policy["gain_db"]
    result = f",volume={float(gain):.6f}dB"
    if policy["mode"] == "dialogue":
        result += ",highpass=f=85,lowpass=f=11000,acompressor=threshold=0.125:ratio=2"
    return result
