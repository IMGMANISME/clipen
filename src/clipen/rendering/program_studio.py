"""Original editorial identity: type lockups, cut-paper tickets and drawn marks.

All decoration and motion stays inside the measured component footprint. This is
procedural artwork, so no remote assets, invented source facts or raster scaling.
"""

import math

from .program_styles import draw_style_text, load_style_font, styled_text_length, type_outline
from .titles import TitleRenderError

HERO = {"program_title", "headline", "impact", "chapter", "burst"}


def title_lines(value):
    if "・" in value:
        return tuple(value.split("・", 1))
    if len(value) > 3:
        middle = (len(value) + 1) // 2
        return value[:middle], value[middle:]
    return (value,)


def layout(draw, font_path, timeline, item, profile):
    kind = profile["component_id"]
    hero = kind in HERO
    w, h = timeline.width, timeline.height
    x, y = round(w * profile["x"]), round(h * profile["y"])
    primary, _, detail = item.text.partition("\n")
    split = kind == "program_title"
    lines = title_lines(primary) if split else (primary,)
    scale = 0.108 if split else 0.091 if hero else 0.061 if kind == "reaction" else 0.040
    pad = max(8, round(h * 0.022))
    limit = min(w * (0.83 if hero else 0.58), w * 0.945 - x)
    minimum = max(14, round(h * scale * 0.70))
    size = max(14, round(h * scale))
    small = load_style_font(font_path, max(12, round(h * 0.024)), profile, role="detail")
    # Reserve a separate column for a rosette; artwork never covers title type.
    ornament = h * 0.13 if split else pad * 2
    while True:
        font = load_style_font(font_path, size, profile)
        text_width = max(styled_text_length(line, font) for line in lines)
        width = max(
            text_width + pad * 4 + ornament,
            styled_text_length(detail, small) + pad * 4 if detail else 0,
            w * (0.39 if split else 0.19 if hero else 0.13),
        )
        if width <= limit or size <= minimum:
            break
        size -= 1
    height = round(size * len(lines) * 1.18 + pad * 3 + (small.size * 1.35 if detail else 0))
    if split:
        height += round(pad * 0.5)
    if width > limit or y + height > h * 0.77:
        raise TitleRenderError("program_component_overflow")
    return (x, y, x + math.ceil(width), y + height), font, pad


def draw_graphic(draw, font_path, timeline, item, profile, bounds, font, pad, relative_frame=None):
    kind = profile["component_id"]
    hero = kind in HERO
    appearance = profile["presets"]["appearance"]
    motion = profile["presets"]["motion"]
    entry = appearance["hero_entry_frames"] if hero else motion["entry_frames"]
    end = appearance["hero_exit_frames"] if hero else motion["exit_frames"]
    relative = entry if relative_frame is None else relative_frame
    p = min(1.0, max(0.0, (relative + 1) / entry))
    exit_progress = min(1.0, max(0.0, (item.duration_frames - relative) / end))
    alpha = min(1.0, p * 3, exit_progress)
    ease = 1 - (1 - p) ** 3
    x, y, right, bottom = bounds
    width, height = right - x, bottom - y
    primary, _, detail = item.text.partition("\n")
    palette = profile["presets"]["palette"]
    stroke = max(1, round(timeline.height * 0.0025))
    small = load_style_font(font_path, max(12, round(timeline.height * 0.024)), profile, role="detail")
    micro = load_style_font(font_path, max(9, round(timeline.height * 0.017)), profile, role="detail")

    def color(name, opacity=1):
        r, g, b, a = palette[name]
        return r, g, b, round(a * alpha * opacity)

    def text(pos, value, selected=font, fill="paper", outline=False):
        draw_style_text(
            draw,
            pos,
            value,
            selected,
            color(fill),
            type_outline(profile, selected, stroke * 3) if outline else 0,
            color("ink"),
        )
        if selected is font and not profile["presets"].get("font_roles"):
            draw_style_text(draw, pos, value, selected, color(fill), max(1, stroke), color(fill))

    def rosette(cx, cy, radius, fill="accent"):
        points = []
        for n in range(32):
            angle = math.tau * n / 32 - 0.18 * (1 - ease)
            r = radius * (1 if n % 2 == 0 else 0.76) * (0.65 + 0.35 * ease)
            points.append((cx + math.cos(angle) * r, cy + math.sin(angle) * r))
        draw.polygon(points, fill=color(fill))
        # Original four-point cutout inside the print rosette.
        r = radius * 0.40
        draw.polygon(
            [
                (cx, cy - r),
                (cx + r * 0.22, cy - r * 0.22),
                (cx + r, cy),
                (cx + r * 0.22, cy + r * 0.22),
                (cx, cy + r),
                (cx - r * 0.22, cy + r * 0.22),
                (cx - r, cy),
                (cx - r * 0.22, cy - r * 0.22),
            ],
            fill=color("paper"),
        )

    if kind == "program_title":
        # Open typographic lockup with staggered rows, a registration rail and
        # a separate decorative seal. No enclosing title rectangle.
        rail = x + pad * 0.25
        draw.line((rail, y + pad, rail, y + height * ease - pad), fill=color("paper"), width=stroke)
        for dy in (pad, height * 0.49, height - pad):
            draw.line((rail - stroke * 2, y + dy, rail + stroke * 2, y + dy), fill=color("paper"), width=stroke)
        badge_x = x + pad * 1.2
        badge_w = micro.getlength("FIELD NOTES") + pad * 1.8
        draw.rounded_rectangle(
            (badge_x, y + pad * 0.2, badge_x + badge_w, y + pad * 1.35), radius=pad * 0.55, fill=color("accent")
        )
        text((badge_x + pad * 0.9, y + pad * 0.40), "FIELD NOTES", micro, "ink")
        lines = title_lines(primary)
        ty = y + pad * 1.9
        for n, line in enumerate(lines):
            phase = min(1.0, max(0.0, p * 1.65 - n * 0.35))
            if not phase:
                continue
            shift = (1 - phase) ** 3 * pad
            tx = x + pad * (1.2 + n * 0.7) + shift
            yy = ty + n * font.size * 1.18
            shadow = stroke if profile["presets"].get("font_roles") else stroke * 2
            text((tx + shadow, yy + shadow), line, fill="ink", outline=True)
            text((tx, yy), line, outline=True)
        r = min(timeline.height * 0.055, height * 0.19)
        rosette(right - r - pad * 0.6, y + pad * 2 + r, r)
        underline_y = ty + font.size * len(lines) * 1.18 + pad * 0.12
        length = max(styled_text_length(line, font) for line in lines)
        draw.line(
            [
                (x + pad * 1.4, underline_y),
                (x + pad + length * ease, underline_y - pad * 0.3),
                (x + pad + length * ease, underline_y + stroke * 2),
            ],
            fill=color("accent"),
            width=stroke * 3,
        )
    elif kind in {"impact", "reaction", "burst"}:
        # A cut-paper sticker with offset ink edge and an asymmetrical silhouette.
        top = y + pad * 0.7
        points = [
            (x + pad, top),
            (right - pad * 2, y + pad * 0.2),
            (right - pad * 0.3, top + pad),
            (right - pad, bottom - pad * 0.65),
            (x + pad * 0.5, bottom - pad * 0.3),
            (x + pad * 0.15, top + pad),
        ]
        draw.polygon([(a + stroke * 2, b + stroke * 2) for a, b in points], fill=color("ink"))
        draw.polygon(points, fill=color("accent"), outline=color("paper"), width=stroke * 2)
        if kind == "burst":
            rosette(right - pad * 1.25, y + pad * 1.25, pad * 0.8, "secondary")
        if p > 0.18:
            text((x + pad * 1.3, y + pad * 1.3 + (1 - ease) * pad * 0.5), primary, fill="ink")
        for n in range(3):
            xx = x + pad * (1.2 + n * 0.5)
            draw.line((xx, y + pad * 0.5, xx + pad * 0.2, y + pad * 0.1), fill=color("paper"), width=stroke)
    elif kind == "chapter":
        # A coloured tab and open ruled heading, without a full panel.
        tab_width = styled_text_length("CHAPTER", micro) + pad * 1.2
        draw.rounded_rectangle(
            (x + pad * 0.3, y + pad * 0.2, x + pad * 0.3 + tab_width, y + pad * 1.35),
            radius=pad * 0.2,
            fill=color("accent"),
        )
        text((x + pad * 0.6, y + pad * 0.35), "CHAPTER", micro, "ink")
        text((x + pad, y + pad * 1.9 + (1 - ease) * pad), primary, outline=True)
        draw.line(
            (x + pad, bottom - pad * 0.5, x + pad + (width - pad * 2) * ease, bottom - pad * 0.5),
            fill=color("paper"),
            width=stroke * 2,
        )
    else:
        # Ticket shape: clipped corners, perforation line, coloured folded tab.
        notch = pad * 0.4
        bx, by, br, bb = x + pad * 0.15, y + pad * 0.2, right - pad * 0.2, bottom - pad * 0.2
        draw.polygon(
            [
                (bx + notch, by),
                (br - notch, by),
                (br, by + notch),
                (br, bb - notch),
                (br - notch, bb),
                (bx + notch, bb),
                (bx, bb - notch),
                (bx, by + notch),
            ],
            fill=color("paper"),
        )
        draw.polygon([(bx, by), (bx + pad * 0.9, by), (bx, by + pad * 0.9)], fill=color("accent"))
        for yy in range(round(by + pad * 0.5), round(bb - pad * 0.3), max(3, stroke * 4)):
            draw.line((br - pad * 0.65, yy, br - pad * 0.65, yy + stroke), fill=color("secondary"), width=stroke)
        text((bx + pad, y + pad * 0.85 + (1 - ease) * pad * 0.2), primary, fill="ink")
        draw.line(
            (bx + pad, bb - pad * 0.35, bx + pad + (width - pad * 3) * ease, bb - pad * 0.35),
            fill=color("accent"),
            width=stroke * 2,
        )
    if detail:
        # Secondary line is reserved below the primary composition.
        ty = bottom - small.size - pad * 0.7
        if kind in {"program_title", "chapter"}:
            tw = styled_text_length(detail, small)
            draw.rectangle((x + pad, ty - pad * 0.25, x + pad * 2 + tw, bottom - pad * 0.35), fill=color("ink"))
            text((x + pad * 1.5, ty), detail, small)
        else:
            text((x + pad * 1.3, ty), detail, small, "ink")
    return bounds
