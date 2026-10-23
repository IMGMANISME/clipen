"""Taiwanese YouTuber-style packaging drawn on top of real footage.

Modelled on how channels such as Joeman, The DoDo Men and 壹加壹 actually dress
their videos: almost no boxes or lower-third cards. Information sits as small
corner tags or outlined text next to the subject; real reactions become sticker
words with thick black and white outlines; titles are bold outlined type.

Each component is drawn on its own small canvas, optionally tilted and popped,
then composited at its footprint. Layout returns that footprint, so collision
and safe-area checks cover every frame of the animation (pointer and circle
targets are drawn separately at their target).
"""
import math

from PIL import Image, ImageDraw

from .titles import TitleRenderError
from .program_styles import load_style_font, styled_text_length, draw_style_text

HERO = {'program_title', 'headline', 'impact', 'chapter', 'burst'}
POP = {'impact', 'burst', 'reaction', 'headline', 'program_title'}
SIZES = {'program_title': .105, 'headline': .082, 'chapter': .038, 'impact': .13, 'burst': .1,
         'reaction': .096, 'location': .036, 'person': .056, 'fact': .062, 'pointer': .052, 'circle': .05}
TILT = {'impact': -4, 'reaction': -5, 'burst': 4}
POP_SLACK = 1.08


def is_creator(profile):
    return profile.get('presets', {}).get('appearance', {}).get('skin') == 'creator'


def _appearance(profile):
    return profile['presets'].get('appearance', {})


def frames(profile):
    motion = profile['presets']['motion']
    appearance = _appearance(profile)
    hero = profile['component_id'] in HERO
    entry = appearance.get('hero_entry_frames', motion['entry_frames']) if hero else motion['entry_frames']
    exit_frames = appearance.get('hero_exit_frames', motion['exit_frames']) if hero else motion['exit_frames']
    return max(1, entry), max(1, exit_frames)


def _rgba(palette, name, alpha=1.):
    r, g, b, a = palette[name]
    return r, g, b, round(a * alpha)


def _rows(item, profile, font_path, size):
    """Text rows as (value, font, fill, stroke colour, stroke ratio, outer colour, outer ratio)."""
    kind = profile['component_id']
    palette = profile['presets']['palette']
    primary, _, detail = item.text.partition('\n')
    paper, ink, accent = (_rgba(palette, n) for n in ('paper', 'ink', 'accent'))
    font = load_style_font(font_path, size, profile)
    small = load_style_font(font_path, max(10, round(size * (.62 if kind in {'program_title', 'headline'} else .7))), profile, role='detail')
    if kind == 'program_title':
        lines = primary.split('・', 1) if '・' in primary else [primary]
        rows = [(line, font, paper, ink, .13, None, 0) for line in lines if line]
        return rows + ([(detail, small, accent, ink, .14, None, 0)] if detail else [])
    if kind in {'impact', 'reaction'}:
        # Sticker words: yellow fill, black outline, white outer ring.
        return [(primary, font, accent, ink, .10, paper, .24)] + ([(detail, small, paper, ink, .14, None, 0)] if detail else [])
    if kind == 'burst':
        return [(primary, font, paper, accent, .12, ink, .22)] + ([(detail, small, paper, ink, .14, None, 0)] if detail else [])
    if kind == 'headline':
        return [(primary, font, paper, ink, .15, None, 0)] + ([(detail, small, accent, ink, .14, None, 0)] if detail else [])
    if kind in {'location', 'chapter'}:
        # Corner tag: "羅馬 | ROME". The detail joins the same line after a bar.
        return [(primary + ('  |  ' + detail if detail else ''), font, paper, ink, .06, None, 0)]
    # fact, person, pointer, circle: outlined name in the accent colour, detail in white.
    return [(primary, font, accent, ink, .12, None, 0)] + ([(detail, small, paper, ink, .12, None, 0)] if detail else [])


def _stroke(font, ratio):
    return max(1, round(font.size * ratio)) if ratio else 0


def _content(item, profile, font_path, size):
    rows = _rows(item, profile, font_path, size)
    margin = max(_stroke(f, so) + _stroke(f, oo) for _, f, _, _, so, _, oo in rows) + 2
    widths = [styled_text_length(value, f) for value, f, *_ in rows]
    heights = [f.size * 1.18 for _, f, *_ in rows]
    width, height = math.ceil(max(widths) + margin * 2), math.ceil(sum(heights) + margin * 2)
    image = Image.new('RGBA', (width, height))
    pen = ImageDraw.Draw(image)
    y = margin
    shadow = profile['component_id'] in {'location', 'chapter'}
    for (value, font, fill, stroke_fill, stroke_ratio, outer, outer_ratio), line_height in zip(rows, heights):
        inner = _stroke(font, stroke_ratio)
        if outer:
            draw_style_text(pen, (margin, y), value, font, outer, inner + _stroke(font, outer_ratio), outer)
        if shadow:
            offset = max(1, round(font.size * .06))
            draw_style_text(pen, (margin + offset, y + offset), value, font, stroke_fill[:3] + (120,))
        draw_style_text(pen, (margin, y), value, font, fill, inner, stroke_fill)
        y += line_height
    return image


def _tilted(image, kind):
    angle = TILT.get(kind, 0)
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True) if angle else image


def _base(timeline):
    """Type scales with the short side, so vertical video keeps landscape proportions."""
    return min(timeline.width, timeline.height)


def _size(timeline, profile, scale):
    kind = profile['component_id']
    appearance = _appearance(profile)
    # type_scale enlarges every component (short-form video); hero_scale only the big ones.
    factor = appearance.get('type_scale', 1.) * (appearance.get('hero_scale', 1.) if kind in HERO else 1.)
    return max(11, round(_base(timeline) * SIZES[kind] * factor * scale))


def _fit(font_path, timeline, item, profile):
    kind = profile['component_id']
    w, h = timeline.width, timeline.height
    x, y = round(w * profile['x']), round(h * profile['y'])
    limit = min(w * (.86 if kind in HERO else .62), w * .95 - x)
    scale = 1.
    while True:
        content = _tilted(_content(item, profile, font_path, _size(timeline, profile, scale)), kind)
        slack = POP_SLACK if kind in POP else 1.
        width, height = math.ceil(content.width * slack), math.ceil(content.height * slack + (_base(timeline) * .012 if kind not in POP else 0))
        if (width <= limit and y + height <= h * .77) or scale <= .6:
            break
        scale -= .05
    if width > limit or y + height > h * .77:
        raise TitleRenderError('program_component_overflow')
    return (x, y, x + width, y + height), content, scale


def layout(draw, font_path, timeline, item, profile):
    box, _, scale = _fit(font_path, timeline, item, profile)
    pad = max(6, round(_base(timeline) * .016))
    return box, load_style_font(font_path, _size(timeline, profile, scale), profile), pad


def _pop(progress):
    """Settle from 70% to full size with an overshoot capped by the footprint slack."""
    if progress >= 1:
        return 1.
    c1 = 1.70158
    t = progress - 1
    back = 1 + (c1 + 1) * t ** 3 + c1 * t ** 2
    return max(.7, min(POP_SLACK, .7 + .3 * back))


def draw_graphic(draw, font_path, timeline, item, profile, relative_frame=None):
    kind = profile['component_id']
    (x, y, right, bottom), content, _ = _fit(font_path, timeline, item, profile)
    entry, exit_frames = frames(profile)
    relative = entry if relative_frame is None else relative_frame
    progress = min(1., max(0., (relative + 1) / entry))
    leaving = min(1., max(0., (item.duration_frames - relative) / exit_frames))
    ease = 1 - (1 - progress) ** 3
    opacity = min(min(1., progress * (2.5 if kind in POP else 1.4)), leaving)
    if kind in POP:
        scale = _pop(progress)
        sized = content.resize((max(1, round(content.width * scale)), max(1, round(content.height * scale))), Image.Resampling.BICUBIC)
        left = x + ((right - x) - sized.width) // 2
        top = y + ((bottom - y) - sized.height) // 2
    else:
        sized = content
        left = x
        top = y + round((1 - ease) * _base(timeline) * .012)  # a short rise within the footprint
    if opacity < 1:
        alpha = sized.getchannel('A').point(lambda value: round(value * opacity))
        sized = sized.copy()
        sized.putalpha(alpha)
    canvas = getattr(draw, '_image', None)
    if canvas is not None:
        canvas.alpha_composite(sized, (max(0, left), max(0, top)))
    if kind in {'pointer', 'circle'} and 'target' in profile:
        _target(draw, timeline, profile, (x, y, right, bottom), ease, opacity)
    return x, y, right, bottom


def _brush(draw, points, radius, fill):
    """A round-capped stroke through points, like a marker on the frame."""
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        draw.line((x0, y0, x1, y1), fill=fill, width=round(radius * 2))
    for x, y in (points[0], points[-1]):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def _marker(draw, strokes, width, paper, ink):
    """Outline every stroke first so overlapping strokes join without seams."""
    for points in strokes:
        _brush(draw, points, width * 1.3, ink)
    for points in strokes:
        _brush(draw, points, width * .66, paper)


def _target(draw, timeline, profile, bounds, ease, opacity):
    palette = profile['presets']['palette']
    paper, ink = _rgba(palette, 'paper', opacity), _rgba(palette, 'ink', opacity)
    target = profile['target']
    tx, ty = target['x'] * timeline.width, target['y'] * timeline.height
    radius = target['radius'] * min(timeline.width, timeline.height)
    width = max(4., _base(timeline) * .0105)
    if profile['component_id'] == 'circle':
        # A slightly uneven hand-drawn loop rather than a perfect circle.
        steps = max(2, round(48 * ease))
        loop = [(tx + radius * (1 + .06 * math.sin(n * .7)) * math.cos(-math.pi / 2 + 2.15 * math.pi * n / 48),
                 ty + radius * (1 + .06 * math.sin(n * .7)) * math.sin(-math.pi / 2 + 2.15 * math.pi * n / 48)) for n in range(steps + 1)]
        _marker(draw, [loop], width, paper, ink)
        return
    x, y, right, bottom = bounds
    # Leave from the label side facing the target, and bow the path like a quick hand stroke.
    start = (right + width * 2, (y + bottom) / 2) if tx >= (x + right) / 2 else (x - width * 2, (y + bottom) / 2)
    gap = radius * .9
    distance = math.hypot(tx - start[0], ty - start[1]) or 1.
    end = (tx - (tx - start[0]) / distance * gap, ty - (ty - start[1]) / distance * gap)
    bend = .22 * distance
    control = ((start[0] + end[0]) / 2 + (end[1] - start[1]) / distance * bend,
               (start[1] + end[1]) / 2 - (end[0] - start[0]) / distance * bend)
    steps = max(2, round(32 * ease))
    curve = [((1 - t) ** 2 * start[0] + 2 * (1 - t) * t * control[0] + t ** 2 * end[0],
              (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * control[1] + t ** 2 * end[1])
             for t in (n / 32 for n in range(steps + 1))]
    strokes = [curve]
    if ease > .85:
        tip = curve[-1]
        angle = math.atan2(tip[1] - curve[-3][1], tip[0] - curve[-3][0])
        length = width * 5.5
        strokes += [[tip, (tip[0] - length * math.cos(angle + side), tip[1] - length * math.sin(angle + side))] for side in (-.6, .6)]
    _marker(draw, strokes, width, paper, ink)
