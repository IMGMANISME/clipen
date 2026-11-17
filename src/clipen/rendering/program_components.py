"""Deterministic in-picture graphics for versioned programme skill packages."""

import math

from PIL import Image, ImageDraw

from . import custom_components, program_creator, program_identities
from .program_motion import FAMILIES, draw_motion_graphic, layout, motion_frames
from .program_styles import draw_styled_graphic, load_style_font, skin, style_font, styled_text_length, type_outline
from .titles import TitleRenderError


def animation_boundaries(item, profile, timeline=None):
    if custom_components.is_custom(profile):
        # A free design can change on any frame; the frames themselves say where.
        return (
            custom_components.changes(timeline, item, profile)
            if timeline is not None
            else set(range(item.start_frame, item.start_frame + item.duration_frames + 1))
        )
    motion = profile["presets"]["motion"]
    if program_creator.is_creator(profile):
        entry_length, exit_length = program_creator.frames(profile)
    else:
        entry_length, exit_length = (
            motion_frames(profile["component_id"], profile)
            if profile["component_id"] in FAMILIES
            else (motion["entry_frames"], motion["exit_frames"])
        )
    entry = min(entry_length, item.duration_frames)
    exit_frames = min(exit_length, item.duration_frames)
    return {
        item.start_frame + n
        for n in (*range(entry + 1), *range(item.duration_frames - exit_frames, item.duration_frames + 1))
    }


def _bounds(draw, font_path, timeline, item, profile):
    if program_creator.is_creator(profile):
        return program_creator.layout(draw, font_path, timeline, item, profile)
    if program_identities.identity(profile):
        return program_identities.layout(draw, font_path, timeline, item, profile)
    if profile["component_id"] in FAMILIES:
        return layout(draw, font_path, timeline, item, profile)
    if skin(profile) == "studio":
        from .program_studio import layout as studio_layout

        return studio_layout(draw, font_path, timeline, item, profile)
    font_path = style_font(font_path, profile)
    typography = profile["presets"].get("typography", {})
    scale = (
        typography.get("reaction", 0.064)
        if profile["component_id"] == "reaction"
        else typography.get("component_primary", 0.038)
    )
    font = load_style_font(font_path, max(14, round(timeline.height * scale)), profile)
    primary, _, detail = item.text.partition("\n")
    detail_font = load_style_font(
        font_path, max(12, round(timeline.height * typography.get("component_detail", 0.024))), profile, role="detail"
    )
    width = max(styled_text_length(primary, font), styled_text_length(detail, detail_font) if detail else 0)
    pad = max(8, round(timeline.height * 0.016))
    x, y = round(timeline.width * profile["x"]), round(timeline.height * profile["y"])
    box_width = max(math.ceil(width) + pad * 2, round(timeline.width * 0.15))
    box_height = round(timeline.height * scale * 1.65) + pad
    if profile["component_id"] != "reaction":
        box_height += round(timeline.height * 0.025)
    if detail:
        box_height += round(timeline.height * 0.038)
    if (
        box_width > timeline.width * 0.58
        or x + box_width > timeline.width * 0.95
        or y + box_height > timeline.height * 0.77
    ):
        raise TitleRenderError("program_component_overflow")
    return (x, y, x + box_width, y + box_height), font, pad


def _draw(draw, font_path, timeline, item, profile, relative_frame=None):
    if program_creator.is_creator(profile):
        return program_creator.draw_graphic(draw, font_path, timeline, item, profile, relative_frame)
    identity = program_identities.identity(profile)
    if profile["component_id"] in FAMILIES and not identity:
        return draw_motion_graphic(draw, font_path, timeline, item, profile, relative_frame)
    bounds, font, pad = _bounds(draw, font_path, timeline, item, profile)
    motion = profile["presets"]["motion"]
    relative = motion["entry_frames"] if relative_frame is None else relative_frame
    entering = min(1.0, max(0.0, (relative + 1) / motion["entry_frames"]))
    leaving = min(1.0, (item.duration_frames - relative) / motion["exit_frames"])
    opacity = min(entering, leaving)
    eased = 1 - (1 - entering) ** 3
    dx = round((1 - eased) * timeline.width * motion["entry_distance_ratio"])
    x, y, right, bottom = bounds
    x -= dx
    right -= dx
    palette = profile["presets"]["palette"]

    def color(name):
        r, g, b, a = palette[name]
        return (r, g, b, round(a * opacity))

    kind = profile["component_id"]
    primary, _, detail = item.text.partition("\n")
    stroke = max(2, round(timeline.height * 0.004))
    if identity:
        program_identities.draw_graphic(draw, font_path, timeline, item, profile, bounds, font, pad, relative_frame)
    elif skin(profile) != "variety":
        draw_styled_graphic(draw, font_path, timeline, item, profile, bounds, font, pad, relative_frame)
    elif kind == "reaction":
        draw.text(
            (x + pad, y + pad // 2),
            primary,
            font=font,
            fill=color("accent"),
            stroke_width=type_outline(profile, font, stroke * 2),
            stroke_fill=color("ink"),
        )
        if detail:
            detail_font = load_style_font(font_path, max(12, round(timeline.height * 0.024)), profile, role="detail")
            draw.text(
                (x + pad, y + pad + round(timeline.height * 0.075)),
                detail,
                font=detail_font,
                fill=color("paper"),
                stroke_width=stroke,
                stroke_fill=color("ink"),
            )
        # A short pair of ticks gives an actual reaction a shared programme
        # accent without turning every dialogue line into a reaction graphic.
        draw.line((x - pad // 2, y + pad, x - pad, y - pad // 2), fill=color("accent"), width=stroke)
        draw.line((right + pad // 2, y + pad, right + pad, y - pad // 2), fill=color("accent"), width=stroke)
    else:
        # Narrow editorial labels sit inside the footage, with no full-screen
        # background. A shared accent edge and typography form programme identity.
        draw.rectangle((x + 3, y + 4, right + 3, bottom + 4), fill=(0, 0, 0, round(75 * opacity)))
        draw.rectangle((x, y, right, bottom), fill=color("ink"))
        draw.rectangle((x, y, x + stroke * 2, bottom), fill=color("secondary" if kind == "person" else "accent"))
        typography = profile["presets"].get("typography", {})
        kicker_font = load_style_font(
            font_path,
            max(10, round(timeline.height * typography.get("component_kicker", 0.017))),
            profile,
            role="detail",
        )
        kicker = (
            profile.get("eyebrow")
            or {"location": "現場", "person": "人物", "fact": "資訊", "pointer": "看這裡", "circle": "細節"}[kind]
        )
        draw.text(
            (x + pad, y + pad // 2), kicker, font=kicker_font, fill=color("secondary" if kind == "person" else "accent")
        )
        primary_y = y + pad // 2 + round(timeline.height * 0.027)
        draw.text((x + pad, primary_y), primary, font=font, fill=color("paper"))
        if detail:
            detail_font = load_style_font(
                font_path,
                max(12, round(timeline.height * typography.get("component_detail", 0.024))),
                profile,
                role="detail",
            )
            draw.text(
                (x + pad, primary_y + round(timeline.height * 0.052)), detail, font=detail_font, fill=color("paper")
            )
        draw.line(
            (x + pad, bottom - pad // 2, right - pad, bottom - pad // 2),
            fill=color("accent"),
            width=max(1, stroke // 2),
        )
    if kind not in {"pointer", "circle"}:
        return bounds
    target = profile["target"]
    tx, ty = target["x"] * timeline.width, target["y"] * timeline.height
    radius = target["radius"] * min(timeline.width, timeline.height)
    if kind == "circle":
        rect = (tx - radius, ty - radius, tx + radius, ty + radius)
        draw.arc(rect, -90, -90 + max(2, round(360 * eased)), fill=color("accent"), width=stroke * 2)
    else:
        origin = (right, (y + bottom) / 2)
        angle = math.atan2(ty - origin[1], tx - origin[0])
        tip = (origin[0] + (tx - origin[0]) * eased, origin[1] + (ty - origin[1]) * eased)
        draw.line((origin, tip), fill=color("ink"), width=stroke * 3)
        draw.line((origin, tip), fill=color("accent"), width=stroke)
        size = max(9, timeline.height * 0.025)
        points = [
            tip,
            (tip[0] - size * math.cos(angle - 0.45), tip[1] - size * math.sin(angle - 0.45)),
            (tip[0] - size * math.cos(angle + 0.45), tip[1] - size * math.sin(angle + 0.45)),
        ]
        draw.polygon(points, fill=color("accent"))
    return bounds


class _Canvas:
    """A timeline seen at another size, so every style's own proportions scale with it."""

    def __init__(self, timeline, width, height):
        self._timeline, self.width, self.height = timeline, width, height

    def __getattr__(self, name):
        return getattr(self._timeline, name)


def _scaled(timeline, profile):
    """For a graphic a person resized: a larger or smaller canvas to draw it on, and where to place
    that canvas so the graphic keeps its top-left corner. None at its designed size."""
    scale = profile.get("scale", 1)
    if scale == 1:
        return None
    canvas = _Canvas(timeline, max(2, round(timeline.width * scale)), max(2, round(timeline.height * scale)))
    return canvas, (
        round(timeline.width * profile["x"]) - round(canvas.width * profile["x"]),
        round(timeline.height * profile["y"]) - round(canvas.height * profile["y"]),
    )


def component_bounds(draw, font_path, timeline, item, profile):
    """The graphic's box on the picture, after any size a person chose."""
    if custom_components.is_custom(profile):
        box = custom_components.bounds(timeline, item, profile)
        dx, dy = round(profile.get("x", 0) * timeline.width), round(profile.get("y", 0) * timeline.height)
        return (box or (dx, dy, dx, dy)), None, 0
    scaled = _scaled(timeline, profile)
    if scaled is None:
        return _bounds(draw, font_path, timeline, item, profile)
    canvas, (dx, dy) = scaled
    (left, top, right, bottom), font, pad = _bounds(draw, font_path, canvas, item, profile)
    box = (left + dx, top + dy, right + dx, bottom + dy)
    # Enlarged, it must still clear the picture's edge and the subtitles at the bottom.
    if box[0] < 0 or box[1] < 0 or box[2] > timeline.width * 0.97 or box[3] > timeline.height * 0.8:
        raise TitleRenderError("program_component_overflow")
    return box, font, pad


def draw_program_component(draw, font_path, timeline, item, profile, relative_frame=None):
    if custom_components.is_custom(profile):
        return custom_components.draw(draw, timeline, item, profile, relative_frame)
    scaled = _scaled(timeline, profile)
    if scaled is None:
        return _draw(draw, font_path, timeline, item, profile, relative_frame)
    canvas, (dx, dy) = scaled
    component_bounds(draw, font_path, timeline, item, profile)
    # Drawn at its new size on a canvas of its own, so type stays sharp instead of being resampled.
    layer = Image.new("RGBA", (canvas.width, canvas.height), (0, 0, 0, 0))
    result = _draw(ImageDraw.Draw(layer), font_path, canvas, item, profile, relative_frame)
    target = draw._image
    sx, sy, tx, ty = max(0, -dx), max(0, -dy), max(0, dx), max(0, dy)
    width, height = min(layer.width - sx, target.width - tx), min(layer.height - sy, target.height - ty)
    if width > 0 and height > 0:
        piece = layer.crop((sx, sy, sx + width, sy + height))
        if target.mode == "RGBA":
            target.alpha_composite(piece, (tx, ty))
        else:
            target.paste(piece, (tx, ty), piece)
    return result
