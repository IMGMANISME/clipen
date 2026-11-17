"""Original channel packages: measured type, distinct geometry and native motion.

An identity is frozen in the text profile, just like its fonts and palette. These
are procedural overlays used by both the samples and the actual film renderer.
"""

import math

from .program_styles import draw_style_text, load_style_font, styled_text_length, type_outline
from .titles import TitleRenderError

HERO = {"program_title", "headline", "impact", "chapter", "burst"}
IDENTITIES = {
    "journal",
    "city",
    "slow",
    "night",
    "food",
    "mission",
    "coffee",
    "family",
    "outdoor",
    "portrait",
    "retro",
    "tech",
}


def identity(profile):
    value = profile.get("presets", {}).get("appearance", {}).get("identity")
    return value if value in IDENTITIES else None


def _lines(value, name, kind):
    if kind != "program_title":
        return (value,)
    if "・" in value:
        return tuple(value.split("・", 1))
    if name in {"journal", "city", "food", "family", "retro"} and len(value) > 5:
        middle = (len(value) + 1) // 2
        return value[:middle], value[middle:]
    return (value,)


def layout(draw, font_path, timeline, item, profile):
    name, kind = identity(profile), profile["component_id"]
    hero = kind in HERO
    primary, _, detail = item.text.partition("\n")
    lines = _lines(primary, name, kind)
    h, w = timeline.height, timeline.width
    x, y = round(w * profile["x"]), round(h * profile["y"])
    pad = max(7, round(h * 0.021))
    scale = (
        (0.13 if name in {"city", "food", "mission"} else 0.105 if name in {"journal", "night", "retro"} else 0.09)
        if hero
        else 0.041
    )
    if kind in {"impact", "burst", "reaction"}:
        scale = 0.14 if hero else 0.067
    size = max(12, round(h * scale))
    minimum = max(12, round(size * 0.58))
    small = load_style_font(font_path, max(10, round(h * 0.025)), profile, role="detail")
    micro = load_style_font(font_path, max(8, round(h * 0.019)), profile, role="detail")
    limit = min(w * (0.83 if hero else 0.58), w * 0.945 - x)
    # A real header and footer are reserved in the collision footprint, rather
    # than adding decorative marks outside the validated bounds.
    extra = pad * (6 if name in {"coffee", "retro", "tech"} else 4)
    while True:
        font = load_style_font(font_path, size, profile)
        width = max(
            max(styled_text_length(line, font) for line in lines) + extra,
            styled_text_length(detail, small) + pad * 4 if detail else 0,
            styled_text_length(profile.get("eyebrow") or "FIELD EXPEDITION", micro) + pad * 7,
            pad * 12 if hero else pad * 8,
        )
        height = pad * 4 + size * len(lines) * 1.18 + (small.size * 1.5 if detail else 0)
        if (width <= limit and y + height <= h * 0.77) or size <= minimum:
            break
        size -= 1
    if width > limit or y + height > h * 0.77:
        raise TitleRenderError("program_component_overflow")
    return (x, y, x + math.ceil(width), y + math.ceil(height)), font, pad


def draw_graphic(draw, font_path, timeline, item, profile, bounds, font, pad, relative_frame=None):
    name, kind = identity(profile), profile["component_id"]
    hero = kind in HERO
    appearance = profile["presets"]["appearance"]
    motion = profile["presets"]["motion"]
    entry = appearance.get("hero_entry_frames", 20) if hero else motion["entry_frames"]
    exit_frames = appearance.get("hero_exit_frames", 8) if hero else motion["exit_frames"]
    relative = entry if relative_frame is None else relative_frame
    p = min(1.0, max(0.0, (relative + 1) / max(1, entry)))
    ease = 1 - (1 - p) ** 3
    alpha = min(1.0, p * 4, max(0.0, (item.duration_frames - relative) / max(1, exit_frames)))
    x, y, right, bottom = bounds
    width, height = right - x, bottom - y
    primary, _, detail = item.text.partition("\n")
    lines = _lines(primary, name, kind)
    palette = profile["presets"]["palette"]
    stroke = max(1, round(timeline.height * 0.003))
    small = load_style_font(font_path, max(10, round(timeline.height * 0.025)), profile, role="detail")
    micro = load_style_font(font_path, max(8, round(timeline.height * 0.019)), profile, role="detail")

    def color(key, opacity=1):
        r, g, b, a = palette[key]
        return r, g, b, round(a * alpha * opacity)

    def text(at, value, selected=font, fill="ink", outline=False):
        draw_style_text(
            draw,
            at,
            value,
            selected,
            color(fill),
            type_outline(profile, selected, stroke * 2) if outline else 0,
            color("ink"),
        )

    def rule(points, fill="accent", weight=1):
        draw.line(points, fill=color(fill), width=max(1, stroke * weight))

    def label(value, tx=None, ty=None, fill="accent"):
        text((x + pad if tx is None else tx, y + pad * 0.65 if ty is None else ty), value, micro, fill)

    text_x, text_y, text_fill, outlined = x + pad * 2, y + pad * 2, "paper", False
    kicker = (
        profile.get("eyebrow")
        or {
            "program_title": "ON THE SHOW",
            "chapter": "NEXT CHAPTER",
            "headline": "THE QUESTION",
            "impact": "HIGHLIGHT",
            "burst": "REACTION",
            "reaction": "REACTION",
            "location": "ON LOCATION",
            "person": "IN FRAME",
            "fact": "FIELD NOTES",
            "pointer": "LOOK CLOSER",
            "circle": "DETAIL",
        }[kind]
    )
    if kind == "program_title" and not profile.get("eyebrow"):
        kicker = {
            "journal": "FIELD JOURNAL",
            "city": "CITY / EXPLAINED",
            "slow": "A VISUAL JOURNAL",
            "night": "NIGHT WALK",
            "food": "ON THE MENU",
            "mission": "MISSION",
            "coffee": "COFFEE NOTES",
            "family": "LITTLE MOMENTS",
            "outdoor": "FIELD EXPEDITION",
            "portrait": "PORTRAIT",
            "retro": "TRAVEL CINEMA",
            "tech": "FIELD TEST",
        }[name]

    if name == "journal":
        # An open notebook margin, graphite handwriting and an uneven marker.
        text_fill = "ink"
        rule((x + pad * 0.4, y + pad, x + pad * 0.4, bottom - pad), "accent")
        for n in range(3):
            cy = y + pad * (1 + n * 0.7)
            draw.ellipse((x + pad * 0.1, cy, x + pad * 0.7, cy + pad * 0.6), outline=color("paper"), width=stroke)
        label(kicker, fill="ink")
        for n, value in enumerate(lines):
            yy = y + pad * 2 + n * font.size * 1.18
            draw.rectangle(
                (
                    x + pad * 1.6,
                    yy - pad * 0.2,
                    x + pad * 2.4 + styled_text_length(value, font),
                    yy + font.size + pad * 0.2,
                ),
                fill=color("paper", 0.96),
            )
        length = min(width - pad * 3, styled_text_length(lines[-1], font))
        yy = y + pad * 2 + font.size * len(lines) * 1.18
        rule(
            [
                (x + pad * 2, yy),
                (x + pad * 2 + length * ease * 0.6, yy - pad * 0.25),
                (x + pad * 2 + length * ease, yy + pad * 0.1),
            ],
            "accent",
            3,
        )
    elif name == "city":
        # Printed editorial column, boxed kicker and staggered typographic rows.
        draw.rectangle((x, y, right, bottom), fill=color("paper"))
        draw.rectangle((x, y, x + pad * 0.7, bottom), fill=color("accent"))
        draw.rectangle((x + pad * 2, y + pad * 0.6, right - pad * 2, y + pad * 1.6), fill=color("ink"))
        label(kicker, x + pad * 2.4, y + pad * 0.68, "paper")
        rule((right - pad * 1.3, y + pad * 2, right - pad * 1.3, bottom - pad), "ink")
        text_fill = "ink"
    elif name == "slow":
        # Small serif lockup and a single expanding hairline: no box.
        draw.rectangle((x, y, right, bottom), fill=color("ink", 0.35))
        text_x = x + pad
        label(kicker, fill="paper")
        rule((x + pad, bottom - pad * 0.55, x + pad + (width - pad * 2) * ease, bottom - pad * 0.55), "paper")
    elif name == "night":
        # Offset neon frame, cyan glyph edge and vertical index ticks.
        draw.rectangle((x, y, right, bottom), fill=color("ink", 0.78))
        rule(
            (x + stroke, bottom - pad, x + stroke, y + stroke, x + stroke + (width - stroke * 2) * ease, y + stroke),
            "accent",
            2,
        )
        rule((x + pad, bottom, right, bottom, right, bottom - height * ease), "secondary")
        for n in range(4):
            rule((right - pad * 0.7, y + pad * (1 + n * 0.7), right - pad * 0.3, y + pad * (1 + n * 0.7)), "secondary")
        label(kicker)
    elif name == "food":
        # A scalloped menu seal, red/yellow plates and bold reaction lettering.
        pts = []
        for n in range(40):
            angle = math.tau * n / 40
            radius = 1 if n % 2 == 0 else 0.94
            pts.append(
                (
                    x + width / 2 + math.cos(angle) * (width / 2 - stroke) * radius,
                    y + height / 2 + math.sin(angle) * (height / 2 - stroke) * radius,
                )
            )
        draw.polygon(pts, fill=color("accent"), outline=color("paper"), width=stroke * 2)
        draw.rounded_rectangle(
            (x + pad * 1.3, y + pad * 0.4, right - pad * 1.3, y + pad * 1.5), radius=pad * 0.5, fill=color("ink")
        )
        label(kicker, x + pad * 2, y + pad * 0.55, "secondary")
        text_fill, outlined = "paper", True
    elif name == "mission":
        # A clipped, industrial mission placard with diagonal hazard marks.
        cut = pad * 1.2
        draw.polygon(
            [(x, y), (right - cut, y), (right, y + cut), (right, bottom), (x + cut, bottom), (x, bottom - cut)],
            fill=color("ink"),
        )
        for n in range(5):
            xx = x + pad * (1 + n * 0.7)
            rule((xx, y + pad * 0.35, xx + pad * 0.4, y + pad * 0.9), "accent", 2)
        rule((x + pad, bottom - pad * 0.6, x + pad + (width - pad * 2) * ease, bottom - pad * 0.6), "accent", 3)
        # No fabricated timers, scores or progress counters in the overlay.
        label(kicker, x + pad * 5, y + pad * 0.4, "secondary")
        text_fill = "accent"
    elif name == "coffee":
        # A letterpress cafe docket: serif text, receipt edge, fine menu rules.
        draw.rectangle((x, y, right, bottom), fill=color("paper"))
        for n in range(max(1, round(width / pad))):
            xx = x + n * pad
            draw.polygon(
                [(xx, bottom), (min(right, xx + pad * 0.5), bottom - pad * 0.4), (min(right, xx + pad), bottom)],
                fill=color("ink"),
            )
        rule((x + pad, y + pad * 1.5, right - pad, y + pad * 1.5), "ink")
        label(kicker, fill="ink")
        text_x, text_fill = x + pad * 2, "ink"
        draw.arc(
            (right - pad * 2, y + pad * 0.35, right - pad, y + pad * 1.25), 0, 320, fill=color("accent"), width=stroke
        )
    elif name == "family":
        # A rounded speech bubble and a hand-drawn sun, distinct from a ticket.
        draw.rounded_rectangle((x, y, right, bottom - pad * 0.5), radius=pad * 2, fill=color("paper"))
        draw.polygon(
            [(x + pad * 2, bottom - pad), (x + pad * 1.5, bottom), (x + pad * 4, bottom - pad)], fill=color("paper")
        )
        label(kicker)
        text_fill = "ink"
        cx, cy = right - pad * 1.5, y + pad
        draw.ellipse((cx - pad * 0.4, cy - pad * 0.4, cx + pad * 0.4, cy + pad * 0.4), fill=color("accent"))
    elif name == "outdoor":
        # Survey plate with topographic contour lines and an expedition rail.
        draw.rectangle((x, y, right, bottom), fill=color("ink", 0.90))
        for n in range(4):
            yy = y + pad * (0.35 + n * 0.3)
            rule(
                [
                    (x + pad, yy),
                    (x + width * 0.35, yy + pad * 0.2),
                    (x + width * 0.7, yy - pad * 0.1),
                    (right - pad, yy + pad * 0.3),
                ],
                "secondary",
            )
        rule((x + pad * 0.5, y + pad * 2, x + pad * 0.5, bottom - pad), "accent", 2)
        label(kicker, x + pad * 2, y + pad * 0.65, "accent")
        outlined = True
    elif name == "portrait":
        # Documentary pull quote, asymmetrical red rule and quiet attribution.
        draw.rectangle((x, y, right, bottom), fill=color("ink", 0.65))
        rule((x + stroke * 2, y, x + stroke * 2, bottom), "accent", 3)
        text((x + pad, y + pad * 0.1), "“", font, "accent")
        label(kicker, x + pad * 4, y + pad * 0.65, "paper")
        text_x = x + pad * 2
    elif name == "retro":
        # Railway ticket with punch holes, double border and a perforated stub.
        draw.rectangle((x, y, right, bottom), fill=color("paper"))
        draw.rectangle(
            (x + stroke * 3, y + stroke * 3, right - stroke * 3, bottom - stroke * 3),
            outline=color("ink"),
            width=stroke,
        )
        for yy in range(round(y + pad * 0.5), round(bottom - pad * 0.5), max(3, stroke * 4)):
            rule((right - pad * 2, yy, right - pad * 2, yy + stroke), "accent")
        for cy in (y + pad, bottom - pad):
            draw.ellipse((x + pad * 0.6, cy - pad * 0.2, x + pad, cy + pad * 0.2), fill=color("ink"))
        label(kicker, x + pad * 2, y + pad * 0.65, "accent")
        text_fill = "ink"
    else:  # tech: no diagonal comic banner, only a measured spec interface.
        draw.rounded_rectangle(
            (x, y, right, bottom), radius=pad * 0.6, fill=color("ink", 0.96), outline=color("secondary"), width=stroke
        )
        draw.rectangle((x + pad, y + pad * 0.6, x + pad * 1.5, y + pad * 1.1), fill=color("accent"))
        label(kicker, x + pad * 2, y + pad * 0.55, "accent")
        rule((x + pad, y + pad * 1.6, right - pad, y + pad * 1.6), "secondary")
        for xx in (x + pad, right - pad):
            rule((xx, bottom - pad * 1.5, xx, bottom - pad * 0.5), "accent")
        text_x, text_fill = x + pad * 2, "paper"

    for n, value in enumerate(lines):
        local = min(1.0, max(0.0, p * 1.5 - n * 0.28))
        if not local:
            continue
        # Each package has a different entrance grammar while the final text
        # and all transient strokes remain inside the measured footprint.
        offset = (1 - local) ** 3 * pad
        tx = text_x + offset if name in {"city", "mission", "tech"} else text_x
        ty = text_y + n * font.size * 1.18 + (offset if name in {"journal", "slow", "portrait", "family"} else 0)
        if name == "city" and n:
            tx += pad * 0.5
        if name == "night":
            text((tx + stroke, ty + stroke), value, font, "secondary")
        if name in {"food", "mission"} and kind in {"impact", "burst", "reaction"}:
            tx += (1 - ease) * pad
        text((tx, ty), value, font, text_fill, outlined)
    if detail:
        fill = "ink" if name in {"journal", "city", "coffee", "family", "retro"} else "paper"
        if name == "journal":
            draw.rectangle(
                (
                    x + pad * 1.6,
                    bottom - pad - small.size - pad * 0.2,
                    x + pad * 2.4 + styled_text_length(detail, small),
                    bottom - pad * 0.6,
                ),
                fill=color("paper"),
            )
        text((x + pad * 2, bottom - pad - small.size), detail, small, fill, name == "slow")
    return bounds
