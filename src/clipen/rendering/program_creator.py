"""Taiwanese YouTuber-style packaging drawn on top of real footage.

Modelled on how channels such as Joeman, The DoDo Men and 壹加壹 actually dress
their videos: information sits as small corner tags or text next to the
subject, real reactions become sticker words, titles are bold display type.

Each template picks a "look" for four component groups (title, tag, sticker,
info) and a motion style, so templates differ in shape and movement, not only
in colour. A component is drawn on its own small canvas, optionally tilted and
popped, then composited at its footprint. Layout returns that footprint, so
collision and safe-area checks cover every frame of the animation (pointer and
circle targets are drawn separately at their target).
"""
import math

from PIL import Image, ImageDraw, ImageFilter

from .titles import TitleRenderError
from .program_styles import load_style_font, styled_text_length, draw_style_text

HERO = {'program_title', 'headline', 'impact', 'chapter', 'burst'}
POP = {'impact', 'burst', 'reaction', 'headline', 'program_title'}
SIZES = {'program_title': .105, 'headline': .082, 'chapter': .038, 'impact': .13, 'burst': .1,
         'reaction': .096, 'location': .036, 'person': .056, 'fact': .062, 'pointer': .052, 'circle': .05}
TILT = {'impact': -4, 'reaction': -5, 'burst': 4}
POP_SLACK = 1.08
GROUPS = {'program_title': 'title', 'headline': 'title', 'location': 'tag', 'chapter': 'tag',
          'impact': 'sticker', 'reaction': 'sticker', 'burst': 'sticker',
          'fact': 'info', 'person': 'info', 'pointer': 'info', 'circle': 'info'}
DEFAULT_LOOK = {'title': 'outline', 'tag': 'shadow', 'sticker': 'sticker', 'info': 'outline'}


def _row(fill, stroke=None, outer=None, track=0, shadow=None):
    return {'fill': fill, 'stroke': stroke, 'outer': outer, 'track': track, 'shadow': shadow}


# Row styles are (primary, detail). Colours name palette entries, optionally
# with an alpha multiplier. A look may add a shape behind the text, extras
# drawn around single rows, a tilt and a row arrangement.
LOOKS = {
    ('title', 'outline'): {'rows': (_row('paper', ('ink', .13)), _row('accent', ('ink', .14))), 'split': True},
    ('title', 'block'): {'rows': (_row('paper', ('ink', .05)), _row('ink')), 'shape': 'block'},
    ('title', 'serif'): {'rows': (_row('paper', track=.16, shadow='soft'), _row(('paper', .88), track=.22, shadow='soft')),
                         'extras': ('rule_below',), 'detail_scale': .4, 'row_gap': .24},
    ('title', 'pill'): {'rows': (_row('paper'), _row(('paper', .92))), 'shape': 'pill'},
    ('title', 'doodle'): {'rows': (_row('paper', ('ink', .05), shadow='hard:accent'), _row('paper', ('ink', .08))),
                          'extras': ('squiggle',), 'split': True, 'row_gap': .26},
    ('title', 'panel'): {'rows': (_row('paper'), _row('accent')), 'shape': 'panel'},
    ('title', 'bar'): {'rows': (_row('paper', ('ink', .1)), _row('paper', ('ink', .12))), 'extras': ('highlight',)},
    ('title', 'rules'): {'rows': (_row('accent', track=.06, shadow='deep'), _row('paper', track=.3, shadow='deep')),
                         'extras': ('rules',), 'detail_scale': .4},
    ('title', 'meme'): {'rows': (_row('paper', ('secondary', .15), ('ink', .09)), _row('accent', ('ink', .14)))},
    ('tag', 'shadow'): {'rows': (_row('paper', ('ink', .06), shadow='drop'),), 'join': '  |  '},
    ('tag', 'tape'): {'rows': (_row('ink'),), 'join': '  ', 'shape': 'tape', 'tilt': -2},
    ('tag', 'label'): {'rows': (_row('paper'),), 'join': '  ', 'shape': 'label'},
    ('tag', 'rules'): {'rows': (_row('paper', track=.24, shadow='deep'),), 'join': '   ', 'extras': ('rules',)},
    ('tag', 'dot'): {'rows': (_row('paper', ('ink', .1)),), 'join': '｜', 'extras': ('dot',)},
    ('tag', 'keycap'): {'rows': (_row('ink'), _row('paper')), 'inline': True, 'boxes': ('accent', ('ink', .82)),
                        'detail_scale': 1.},
    ('tag', 'chip'): {'rows': (_row('ink'),), 'join': '  ', 'shape': 'chip'},
    ('tag', 'flag'): {'rows': (_row('ink'),), 'join': '｜', 'shape': 'flag'},
    ('tag', 'meme'): {'rows': (_row('ink'),), 'join': ' ', 'shape': 'memebox', 'tilt': -2},
    ('sticker', 'sticker'): {'rows': (_row('accent', ('ink', .10), ('paper', .24)), _row('paper', ('ink', .14)))},
    ('sticker', 'burst'): {'rows': (_row('paper', ('ink', .1)), _row('ink')), 'shape': 'burst'},
    ('sticker', 'quiet'): {'rows': (_row('paper', track=.1, shadow='soft'), _row(('paper', .85), track=.2, shadow='soft')),
                           'tilt': 0},
    ('sticker', 'bubble'): {'rows': (_row('ink'), _row(('ink', .7))), 'shape': 'bubble', 'tilt': -3},
    ('sticker', 'doodle'): {'rows': (_row('accent', ('paper', .1), shadow='soft'), _row('paper', shadow='soft')),
                            'extras': ('sparkles',)},
    ('sticker', 'pill'): {'rows': (_row('ink'), _row(('ink', .75))), 'shape': 'pill', 'tilt': 0},
    ('sticker', 'stamp'): {'rows': (_row('accent'), _row('accent')), 'shape': 'stamp', 'tilt': -7},
    ('sticker', 'glow'): {'rows': (_row('accent', track=.08, shadow='glow'), _row('paper', track=.16, shadow='deep')),
                          'tilt': 0},
    ('sticker', 'meme'): {'rows': (_row('accent', ('ink', .11), ('paper', .2), shadow='hard:secondary'),
                                   _row('paper', ('ink', .14)))},
    ('info', 'outline'): {'rows': (_row('accent', ('ink', .12)), _row('paper', ('ink', .12)))},
    ('info', 'label'): {'rows': (_row('paper'), _row('ink')), 'boxes': ('ink', 'accent')},
    ('info', 'quiet'): {'rows': (_row('paper', track=.06, shadow='soft'), _row(('paper', .82), shadow='soft')),
                        'extras': ('vline',)},
    ('info', 'price'): {'rows': (_row('ink'), _row('accent')), 'shape': 'price'},
    ('info', 'doodle'): {'rows': (_row('paper', ('ink', .05), shadow='hard:accent'), _row('paper', ('ink', .1)))},
    ('info', 'callout'): {'rows': (_row('paper'), _row('accent')), 'shape': 'panel'},
    ('info', 'verdict'): {'rows': (_row('paper', ('ink', .12)), _row('paper', ('ink', .12))), 'extras': ('icon',)},
    ('info', 'name'): {'rows': (_row('accent', track=.04, shadow='deep'), _row('paper', shadow='deep')),
                       'extras': ('underline',), 'row_gap': .2},
    ('info', 'meme'): {'rows': (_row('paper', ('ink', .14)), _row('accent', ('ink', .14)))},
}
# A verdict reads as a drawback when the label starts like one.
CONS = ('缺點', '缺', '扣分', '不推', '踩雷')


def is_creator(profile):
    return profile.get('presets', {}).get('appearance', {}).get('skin') == 'creator'


def _appearance(profile):
    return profile['presets'].get('appearance', {})


def _look(profile):
    group = GROUPS[profile['component_id']]
    name = _appearance(profile).get('looks', {}).get(group, DEFAULT_LOOK[group])
    if (group, name) not in LOOKS:
        raise TitleRenderError('program_component_unknown_look')
    return group, name, LOOKS[group, name]


def _motion(profile):
    return _appearance(profile).get('motion_style', 'pop')


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


def _color(palette, spec):
    if spec is None:
        return None
    name, alpha = (spec, 1.) if isinstance(spec, str) else spec
    return _rgba(palette, name, alpha)


def _stroke(font, ratio):
    return max(1, round(font.size * ratio)) if ratio else 0


def _length(value, font, track):
    if not track:
        return styled_text_length(value, font)
    return sum(styled_text_length(c, font) for c in value) + font.size * track * (len(value) - 1)


def _draw_text(pen, x, y, value, font, fill, stroke=0, stroke_fill=None, track=0):
    if not track:
        draw_style_text(pen, (x, y), value, font, fill, stroke, stroke_fill)
        return
    # Keep one baseline for the line; each glyph would otherwise sit on its own top edge.
    top = font.getbbox(value, anchor='ls')[1]
    for character in value:
        lift = 0 if character.isspace() else font.getbbox(character, anchor='ls')[1] - top
        draw_style_text(pen, (x, y + lift), character, font, fill, stroke, stroke_fill)
        x += styled_text_length(character, font) + font.size * track


def _row_image(value, font, spec, palette):
    """One line of type with its outline, outer ring and shadow, plus the margin used."""
    stroke_color, stroke_ratio = spec['stroke'] or (None, 0)
    outer_color, outer_ratio = spec['outer'] or (None, 0)
    inner, ring = _stroke(font, stroke_ratio), _stroke(font, stroke_ratio) + _stroke(font, outer_ratio)
    shadow = spec['shadow']
    blur = round(font.size * {'glow': .16, 'deep': .1, 'soft': .07}[shadow]) if shadow in {'soft', 'deep', 'glow'} else 0
    offset = round(font.size * (.07 if shadow.startswith('hard') else .04 if shadow in {'soft', 'deep'} else .06)) if shadow else 0
    margin = ring + blur * 3 + offset + 2
    width, height = _length(value, font, spec['track']), font.size * 1.18
    image = Image.new('RGBA', (math.ceil(width + margin * 2), math.ceil(height + margin * 2)))
    if shadow:
        colour = {'drop': _rgba(palette, 'ink', .47), 'soft': _rgba(palette, 'ink', .6), 'deep': _rgba(palette, 'ink', .9),
                  'glow': _rgba(palette, 'accent', .75)}.get(shadow) or _rgba(palette, shadow.split(':')[1])
        layer = Image.new('RGBA', image.size)
        dx = 0 if shadow == 'glow' else offset
        _draw_text(ImageDraw.Draw(layer), margin + dx, margin + dx, value, font, colour, ring, colour, spec['track'])
        if blur:
            layer = layer.filter(ImageFilter.GaussianBlur(blur))
        image.alpha_composite(layer)
    pen = ImageDraw.Draw(image)
    fill = _color(palette, spec['fill'])
    if outer_color:
        outer = _color(palette, outer_color)
        _draw_text(pen, margin, margin, value, font, outer, ring, outer, spec['track'])
    _draw_text(pen, margin, margin, value, font, fill, inner, _color(palette, stroke_color), spec['track'])
    return image, margin, width, height


def _texts(item, look, group):
    primary, _, detail = item.text.partition('\n')
    if 'join' in look:
        return [primary + (look['join'] + detail if detail else '')], False
    rows = primary.split('・', 1) if look.get('split') and '・' in primary else [primary]
    return [r for r in rows if r] + ([detail] if detail else []), bool(detail)


def _assemble(item, profile, font_path, size, look, group):
    """Lay rows out (stacked or side by side) and return the canvas with each row's text box."""
    palette = profile['presets']['palette']
    kind = profile['component_id']
    texts, has_detail = _texts(item, look, group)
    font = load_style_font(font_path, size, profile)
    default_scale = .62 if kind in {'program_title', 'headline'} else .7
    small = load_style_font(font_path, max(10, round(size * look.get('detail_scale', default_scale))), profile, role='detail')
    primary_spec, detail_spec = look['rows'][0], look['rows'][-1]
    rows = []
    for index, value in enumerate(texts):
        is_detail = has_detail and index == len(texts) - 1
        rows.append(_row_image(value, small if is_detail else font, detail_spec if is_detail else primary_spec, palette))
    boxes = look.get('boxes')
    pad_x, pad_y = (round(size * .34), round(size * .12)) if boxes else (0, 0)
    gap = (pad_x * 2 + round(size * .2)) if look.get('inline') else pad_y * 2 + round(size * look.get('row_gap', 0))
    outer = max(margin for _, margin, _, _ in rows)
    if look.get('inline'):
        width = sum(w for _, _, w, _ in rows) + gap * (len(rows) - 1)
        height = max(h for _, _, _, h in rows)
    else:
        width = max(w for _, _, w, _ in rows)
        height = sum(h for _, _, _, h in rows) + gap * (len(rows) - 1)
    edge = outer + max(pad_x, pad_y)
    canvas = Image.new('RGBA', (math.ceil(width + edge * 2), math.ceil(height + edge * 2)))
    rects, cursor = [], edge
    for image, margin, w, h in rows:
        x, y = (cursor, edge + (height - h) / 2) if look.get('inline') else (edge, cursor)
        rects.append((x, y, x + w, y + h))
        cursor += (w if look.get('inline') else h) + gap
    if boxes:
        pen = ImageDraw.Draw(canvas)
        for (x0, y0, x1, y1), colour, (_, _, _, h) in zip(rects, boxes, rows):
            radius = round(h * (.22 if look.get('inline') else .08))
            pen.rounded_rectangle((x0 - pad_x, y0 - pad_y, x1 + pad_x, y1 + pad_y * .4), radius, fill=_color(palette, colour))
    for (image, margin, _, _), (x, y, _, _) in zip(rows, rects):
        canvas.alpha_composite(image, (round(x - margin), round(y - margin)))
    return canvas, rects, font.size, has_detail


def _shape(canvas, rects, size, look, palette, has_detail=False, con=False):
    """Draw the look's shape behind the text on a larger canvas."""
    name = look.get('shape')
    extras = look.get('extras', ())
    if not name and not extras:
        return canvas
    grow = round(size * 1.1)
    out = Image.new('RGBA', (canvas.width + grow * 2, canvas.height + grow * 2))
    pen = ImageDraw.Draw(out)
    rects = [(x0 + grow, y0 + grow, x1 + grow, y1 + grow) for x0, y0, x1, y1 in rects]
    x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
    paper, ink, accent = (_rgba(palette, n) for n in ('paper', 'ink', 'accent'))
    s = size
    if name == 'block':
        skew = (y1 - y0) * .2
        bx0, by0, bx1, by1 = x0 - s * .3, y0 - s * .04, x1 + s * .3 + skew, y1 + s * .02
        poly = lambda dx, dy: [(bx0 + skew + dx, by0 + dy), (bx1 + dx, by0 + dy), (bx1 - skew + dx, by1 + dy), (bx0 + dx, by1 + dy)]
        pen.polygon(poly(s * .09, s * .09), fill=ink)
        pen.polygon(poly(0, 0), fill=accent)
    elif name == 'pill':
        box = (x0 - s * .42, y0 - s * .06, x1 + s * .42, y1 + s * .02)
        radius = (box[3] - box[1]) / 2
        pen.rounded_rectangle(tuple(v + s * .07 for v in box), radius, fill=ink)
        pen.rounded_rectangle(box, radius, fill=accent)
    elif name == 'panel':
        box = (x0 - s * .55, y0 - s * .16, x1 + s * .45, y1 + s * .08)
        pen.rounded_rectangle(box, round(s * .22), fill=_rgba(palette, 'ink', .8))
        pen.rounded_rectangle((box[0] + s * .16, box[1] + s * .2, box[0] + s * .26, box[3] - s * .2), round(s * .05), fill=accent)
    elif name == 'tape':
        bx0, by0, bx1, by1 = x0 - s * .4, y0 - s * .08, x1 + s * .4, y1 + s * .02
        teeth, depth = 5, s * .12
        left = [(bx0 + (depth if n % 2 else 0), by0 + (by1 - by0) * n / teeth) for n in range(teeth + 1)]
        right = [(bx1 - (depth if n % 2 else 0), by1 - (by1 - by0) * n / teeth) for n in range(teeth + 1)]
        pen.polygon(left + right, fill=_rgba(palette, 'paper', .88))
    elif name == 'label':
        box = (x0 - s * .55, y0 - s * .1, x1 + s * .35, y1 + s * .02)
        pen.rectangle(box, fill=ink)
        pen.rectangle((box[0] + s * .16, box[1] + s * .32, box[0] + s * .34, box[3] - s * .3), fill=accent)
    elif name == 'chip':
        box = (x0 - s * .55, y0 - s * .1, x1 + s * .35, y1 + s * .02)
        pen.rounded_rectangle(box, round(s * .25), fill=paper)
        pen.rounded_rectangle((box[0] + s * .16, box[1] + s * .28, box[0] + s * .3, box[3] - s * .26), round(s * .07), fill=accent)
    elif name == 'flag':
        bx0, by0, bx1, by1 = x0 - s * .35, y0 - s * .1, x1 + s * .7, y1 + s * .02
        pen.polygon([(bx0, by0), (bx1, by0), (bx1 - s * .32, (by0 + by1) / 2), (bx1, by1), (bx0, by1)], fill=accent)
    elif name == 'memebox':
        pen.rectangle((x0 - s * .22, y0 - s * .02, x1 + s * .22, y1 - s * .02), fill=accent)
    elif name == 'burst':
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2 - s * .04
        rx, ry = (x1 - x0) / 2 + s * .62, (y1 - y0) / 2 + s * .5
        def star(scale):
            points = []
            for n in range(28):
                r = (1 if n % 2 == 0 else .76 + .06 * math.sin(n * 1.7)) * scale
                a = -math.pi / 2 + math.pi * n / 14
                points.append((cx + rx * r * math.cos(a), cy + ry * r * math.sin(a)))
            return points
        pen.polygon(star(1.07), fill=ink)
        pen.polygon(star(1.), fill=accent)
    elif name == 'bubble':
        box = (x0 - s * .45, y0 - s * .14, x1 + s * .45, y1 + s * .06)
        line = s * .07
        tail = [(box[0] + s * .5, box[3] - s * .1), (box[0] + s * .1, box[3] + s * .5), (box[0] + s * 1.05, box[3] - s * .1)]
        radius = round((box[3] - box[1]) * .42)
        pen.rounded_rectangle(tuple(v + d for v, d in zip(box, (-line, -line, line, line))), radius + line, fill=ink)
        pen.polygon([(tail[0][0] - line, tail[0][1]), (tail[1][0] - line * .8, tail[1][1] + line * 1.4), (tail[2][0] + line, tail[2][1])], fill=ink)
        pen.rounded_rectangle(box, radius, fill=paper)
        pen.polygon(tail, fill=paper)
    elif name == 'stamp':
        pen.rectangle((x0 - s * .38, y0 - s * .16, x1 + s * .38, y1 + s * .08), outline=accent, width=max(2, round(s * .08)))
        pen.rectangle((x0 - s * .24, y0 - s * .05, x1 + s * .24, y1 - s * .03), outline=accent, width=max(1, round(s * .03)))
    elif name == 'price':
        bx0, by0, bx1, by1 = x0 - s * .3, y0 - s * .12, x1 + s * .4, y1 + s * .04
        tip = bx0 - s * .5
        outline = [(tip, (by0 + by1) / 2), (bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)]
        line = s * .05
        pen.polygon([(tip - line * 1.5, (by0 + by1) / 2), (bx0 - line * .4, by0 - line), (bx1 + line, by0 - line),
                     (bx1 + line, by1 + line), (bx0 - line * .4, by1 + line)], fill=ink)
        pen.polygon(outline, fill=paper)
        hole = (bx0 - s * .12, (by0 + by1) / 2)
        pen.ellipse((hole[0] - s * .08, hole[1] - s * .08, hole[0] + s * .08, hole[1] + s * .08), fill=ink)
    out.alpha_composite(canvas, (grow, grow))
    _extras(pen, rects, extras, s, palette, out, has_detail, con)
    box = out.getchannel('A').getbbox()
    return out.crop(box) if box else out


def _extras(pen, rects, extras, s, palette, out, has_detail, con):
    paper, ink, accent = (_rgba(palette, n) for n in ('paper', 'ink', 'accent'))
    first, last = rects[0], rects[-1]
    titles = rects[:-1] if has_detail else rects
    x0, y0, x1, y1 = min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects)
    for extra in extras:
        if extra == 'rule_below' and len(rects) > 1:
            y = first[3] + s * .02
            pen.line((x0 + s * .04, y, x0 + (x1 - x0) * .3, y), fill=paper, width=max(1, round(s * .03)))
        elif extra == 'rules':
            r = last
            y = (r[1] + r[3]) / 2 + s * .02
            length, gap = s * .9, s * .3
            width = max(1, round(s * .025))
            colour = accent if len(rects) > 1 else paper
            pen.line((r[0] - gap - length, y, r[0] - gap, y), fill=colour, width=width)
            pen.line((r[2] + gap, y, r[2] + gap + length, y), fill=colour, width=width)
        elif extra == 'highlight':
            # A marker stroke behind the lower half of each title row.
            layer = Image.new('RGBA', out.size)
            mark = ImageDraw.Draw(layer)
            for r in titles:
                h = r[3] - r[1]
                mark.rectangle((r[0] - s * .12, r[1] + h * .52, r[2] + s * .12, r[1] + h * .95), fill=accent)
            layer.alpha_composite(out)
            out.paste(layer)
        elif extra == 'squiggle':
            target = titles[-1]
            y, amplitude, period = target[3] + s * .02, s * .07, s * .55
            points = [(target[0] + t, y + amplitude * math.sin(t / period * 2 * math.pi))
                      for t in range(0, max(2, round(target[2] - target[0])), 3)]
            pen.line(points, fill=accent, width=max(2, round(s * .08)), joint='curve')
        elif extra == 'underline':
            y = first[3] + s * .02
            pen.line((first[0], y, first[2], y), fill=accent, width=max(1, round(s * .04)))
        elif extra == 'dot':
            cx, cy, r = first[0] - s * .42, (first[1] + first[3]) / 2 + s * .02, s * .17
            pen.ellipse((cx - r, cy - r, cx + r, cy + r), fill=accent)
        elif extra == 'vline':
            x = x0 - s * .35
            pen.line((x, y0 + s * .12, x, y1 - s * .05), fill=paper, width=max(1, round(s * .04)))
        elif extra == 'sparkles':
            for (cx, cy), angles in (((x1 + s * .05, y0 + s * .05), (-75, -40, -5)), ((x0 - s * .05, y1 - s * .1), (105, 140, 175))):
                for angle in angles:
                    a = math.radians(angle)
                    start = (cx + s * .18 * math.cos(a), cy + s * .18 * math.sin(a))
                    end = (cx + s * .5 * math.cos(a), cy + s * .5 * math.sin(a))
                    _brush(pen, [start, end], s * .045, ink)
                    _brush(pen, [start, end], s * .022, paper)
        elif extra == 'icon':
            # A check for a strength, a cross for a drawback.
            r = first
            h = r[3] - r[1]
            box = (r[0] - h * 1.05, r[1] + h * .04, r[0] - h * .2, r[1] + h * .89)
            pen.rounded_rectangle(box, round(h * .16), fill=_rgba(palette, 'secondary' if con else 'accent'), outline=ink,
                                  width=max(1, round(s * .05)))
            bx0, by0, bx1, by1 = box
            w = max(2, round(s * .11))
            if con:
                inset = (bx1 - bx0) * .28
                pen.line((bx0 + inset, by0 + inset, bx1 - inset, by1 - inset), fill=paper, width=w)
                pen.line((bx0 + inset, by1 - inset, bx1 - inset, by0 + inset), fill=paper, width=w)
            else:
                pen.line([(bx0 + (bx1 - bx0) * .24, by0 + (by1 - by0) * .52), (bx0 + (bx1 - bx0) * .43, by0 + (by1 - by0) * .72),
                          (bx0 + (bx1 - bx0) * .78, by0 + (by1 - by0) * .3)], fill=paper, width=w, joint='curve')


def _content(item, profile, font_path, size):
    group, name, look = _look(profile)
    if (group, name) == ('sticker', 'sticker') and profile['component_id'] == 'burst':
        look = {'rows': (_row('paper', ('accent', .12), ('ink', .22)), _row('paper', ('ink', .14)))}
    canvas, rects, font_size, has_detail = _assemble(item, profile, font_path, size, look, group)
    con = item.text.lstrip().startswith(CONS)
    return _shape(canvas, rects, font_size, look, profile['presets']['palette'], has_detail, con), look


def _tilted(image, kind, look):
    angle = look.get('tilt', TILT.get(kind, 0) if GROUPS[kind] == 'sticker' else 0)
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


def _pops(profile):
    return profile['component_id'] in POP and _motion(profile) in {'pop', 'punch', 'bounce', 'stamp'}


def _fit(font_path, timeline, item, profile):
    kind = profile['component_id']
    w, h = timeline.width, timeline.height
    x, y = round(w * profile['x']), round(h * profile['y'])
    limit = min(w * (.86 if kind in HERO else .62), w * .95 - x)
    scale = 1.
    while True:
        image, look = _content(item, profile, font_path, _size(timeline, profile, scale))
        content = _tilted(image, kind, look)
        slack = POP_SLACK if _pops(profile) else 1.
        rise = 0 if _pops(profile) else _base(timeline) * .012
        width, height = math.ceil(content.width * slack), math.ceil(content.height * slack + rise)
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


def _scaled(content, sx, sy):
    return content.resize((max(1, round(content.width * sx)), max(1, round(content.height * sy))), Image.Resampling.BICUBIC)


def _faded(image, opacity):
    if opacity >= 1:
        return image
    alpha = image.getchannel('A').point(lambda value: round(value * opacity))
    image = image.copy()
    image.putalpha(alpha)
    return image


def _wiped(image, progress, steps=None):
    """Reveal from the left; stepped reveals read like writing stroke by stroke."""
    if progress >= 1:
        return image
    amount = math.floor(progress * steps) / steps if steps else progress
    mask = Image.new('L', image.size, 0)
    ImageDraw.Draw(mask).rectangle((0, 0, round(image.width * amount), image.height), fill=255)
    alpha = Image.composite(image.getchannel('A'), mask, mask)
    image = image.copy()
    image.putalpha(alpha)
    return image


def draw_graphic(draw, font_path, timeline, item, profile, relative_frame=None):
    kind = profile['component_id']
    motion = _motion(profile)
    (x, y, right, bottom), content, _ = _fit(font_path, timeline, item, profile)
    entry, exit_frames = frames(profile)
    relative = entry if relative_frame is None else relative_frame
    progress = min(1., max(0., (relative + 1) / entry))
    leaving = min(1., max(0., (item.duration_frames - relative) / exit_frames))
    ease = 1 - (1 - progress) ** 3
    pops = _pops(profile)
    opacity = min(min(1., progress * (2.5 if pops else 1.4)), leaving)
    if motion == 'fade':
        opacity = min(ease, leaving)
    if pops:
        if motion == 'bounce':
            # Drop in squashed, stretch, then settle, all inside the footprint slack.
            stretch = 1 + .08 * math.sin(progress * math.pi * 1.5) * (1 - progress) if progress < 1 else 1.
            sized = _scaled(content, min(POP_SLACK, 2 - stretch) * (.75 + .25 * ease), min(POP_SLACK, stretch) * (.75 + .25 * ease))
        elif motion == 'stamp':
            # Thud down from slightly large to rest.
            sized = _scaled(content, *(POP_SLACK - (POP_SLACK - 1) * ease,) * 2)
        else:
            sized = _scaled(content, *(_pop(progress),) * 2)
        left = x + ((right - x) - sized.width) // 2
        top = y + ((bottom - y) - sized.height) // 2
        if motion == 'punch' and progress < 1:
            # A short shake that decays inside the slack.
            room = ((right - x) - sized.width) // 2
            left += round(math.sin(relative * 2.6) * room * (1 - progress))
    else:
        sized = content
        left = x
        top = y + round((1 - ease) * _base(timeline) * .012)  # a short rise within the footprint
        if motion == 'write':
            sized = _wiped(sized, progress, steps=max(4, len(item.text)))
        elif motion == 'slide':
            sized = _wiped(sized, ease)
    sized = _faded(sized, opacity)
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
    clean = _appearance(profile).get('arrow') == 'clean'
    paper, ink = _rgba(palette, 'paper', opacity), _rgba(palette, 'ink', opacity)
    if clean:
        # Interface-style callouts: one accent line without a dark outline.
        paper, ink = _rgba(palette, 'accent', opacity), _rgba(palette, 'accent', opacity)
    target = profile['target']
    tx, ty = target['x'] * timeline.width, target['y'] * timeline.height
    radius = target['radius'] * min(timeline.width, timeline.height)
    width = max(4., _base(timeline) * (.007 if clean else .0105))
    if profile['component_id'] == 'circle':
        # A slightly uneven hand-drawn loop, or a true circle for clean callouts.
        wobble, turn = (0, 2.) if clean else (.06, 2.15)
        steps = max(2, round(48 * ease))
        loop = [(tx + radius * (1 + wobble * math.sin(n * .7)) * math.cos(-math.pi / 2 + turn * math.pi * n / 48),
                 ty + radius * (1 + wobble * math.sin(n * .7)) * math.sin(-math.pi / 2 + turn * math.pi * n / 48)) for n in range(steps + 1)]
        _marker(draw, [loop], width, paper, ink)
        return
    x, y, right, bottom = bounds
    # Leave from the label side facing the target, and bow the path like a quick hand stroke.
    start = (right + width * 2, (y + bottom) / 2) if tx >= (x + right) / 2 else (x - width * 2, (y + bottom) / 2)
    gap = radius * .9
    distance = math.hypot(tx - start[0], ty - start[1]) or 1.
    end = (tx - (tx - start[0]) / distance * gap, ty - (ty - start[1]) / distance * gap)
    bend = 0 if clean else .22 * distance
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
        length = width * (4 if clean else 5.5)
        strokes += [[tip, (tip[0] - length * math.cos(angle + side), tip[1] - length * math.sin(angle + side))] for side in (-.6, .6)]
    _marker(draw, strokes, width, paper, ink)
