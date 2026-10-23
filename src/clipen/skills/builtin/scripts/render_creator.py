"""Render covers and eight-second samples for the creator-style editing templates.

Backgrounds are procedural, softly blurred stand-ins for real footage, so the
preview shows how the overlays sit on a video. Every overlay is drawn by the
same draw_program_component the film renderer uses.

    python src/clipen/skills/builtin/scripts/render_creator.py [template_id ...]
"""
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFilter
from clipen.domain import EditTimeline, Rational, TextClip, Track, TrackKind
from clipen.rendering.program_components import draw_program_component
from clipen.rendering.program_subtitles import draw_program_subtitle
from clipen.rendering.program_styles import load_style_font, draw_style_text
from clipen.rendering.titles import TitleCardRenderer
from clipen.skills.visual_styles import style_presets, font_presets

WIDTH, HEIGHT, FPS, DURATION = 960, 540, 30, 8
COVER_FRAME = 150


def _gradient(size, stops):
    """Vertical gradient through (position, rgb) stops."""
    width, height = size
    image = Image.new('RGB', size)
    pen = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(1, height - 1)
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                k = (t - p0) / max(1e-6, p1 - p0)
                pen.line((0, y, width, y), fill=tuple(round(a + (b - a) * k) for a, b in zip(c0, c1)))
                break
    return image


def _grain(scene, amount=.05):
    """Film grain dithers gradient banding and reads more like footage."""
    noise = Image.effect_noise(scene.size, 40).convert('RGB')
    return Image.blend(scene, noise, amount)


def _hills(width, height, base, depth, phase):
    points = [(0, height)]
    for n in range(81):
        x = width * n / 80
        y = base - depth * (.55 + .25 * math.sin(n * .21 + phase) + .2 * math.sin(n * .53 + phase * 2))
        points.append((x, y))
    points.append((width, height))
    return points


def _travel_scene(width, height):
    scene = _gradient((width, height), [(0, (52, 86, 150)), (.38, (226, 146, 128)), (.56, (255, 198, 146)),
                                        (.575, (96, 138, 172)), (1, (30, 58, 88))])
    pen = ImageDraw.Draw(scene, 'RGBA')
    sun = (width * .7, height * .5)
    for radius, alpha in ((170, 30), (100, 70), (46, 235)):
        pen.ellipse((sun[0] - radius, sun[1] - radius, sun[0] + radius, sun[1] + radius), fill=(255, 228, 176, alpha))
    pen.polygon(_hills(width, height, height * .565, height * .09, .4), fill=(120, 96, 120, 150))
    pen.polygon(_hills(width, height, height * .575, height * .05, 2.1), fill=(70, 62, 92, 210))
    pen.rectangle((0, height * .575, width, height), fill=(70, 112, 150, 255))
    for n in range(30):  # light glinting on the water
        y = height * (.59 + n * .011)
        span = width * (.03 + n * .005)
        pen.line((sun[0] - span, y, sun[0] + span, y), fill=(255, 216, 168, max(16, 160 - n * 5)), width=2)
    pen.rectangle((0, height * .9, width, height), fill=(34, 30, 36, 255))  # promenade railing in the foreground
    pen.rectangle((0, height * .835, width, height * .848), fill=(34, 30, 36, 255))
    for x in range(0, width, round(width * .07)):
        pen.rectangle((x, height * .835, x + 7, height * .9), fill=(34, 30, 36, 255))
    return _grain(scene.filter(ImageFilter.GaussianBlur(radius=height * .01)))


def _desk_scene(width, height):
    scene = _gradient((width, height), [(0, (226, 222, 216)), (.5, (238, 235, 230)), (.505, (186, 160, 132)), (1, (150, 122, 96))])
    pen = ImageDraw.Draw(scene, 'RGBA')
    for n in range(9):  # wood grain
        y = height * (.54 + n * .052)
        pen.line((0, y, width, y + height * .01), fill=(130, 100, 74, 40), width=2)
    # Product box standing behind, slightly left of centre.
    pen.ellipse((width * .38, height * .76, width * .7, height * .84), fill=(60, 40, 24, 80))
    pen.rectangle((width * .42, height * .3, width * .66, height * .8), fill=(246, 246, 244, 255))
    pen.rectangle((width * .42, height * .3, width * .66, height * .36), fill=(226, 226, 224, 255))
    pen.rectangle((width * .46, height * .46, width * .62, height * .5), fill=(40, 44, 52, 255))
    pen.rectangle((width * .48, height * .53, width * .58, height * .545), fill=(160, 164, 172, 255))
    # Open earbud case in front, right.
    pen.ellipse((width * .62, height * .78, width * .82, height * .84), fill=(60, 40, 24, 90))
    pen.rounded_rectangle((width * .64, height * .58, width * .79, height * .8), radius=round(height * .05), fill=(250, 250, 252, 255))
    pen.rounded_rectangle((width * .645, height * .5, width * .785, height * .6), radius=round(height * .04), fill=(232, 232, 236, 255))
    pen.ellipse((width * .665, height * .62, width * .705, height * .7), fill=(214, 216, 222, 255))
    pen.ellipse((width * .725, height * .62, width * .765, height * .7), fill=(214, 216, 222, 255))
    return _grain(scene.filter(ImageFilter.GaussianBlur(radius=height * .005)), .035)


SCENES = {'travel-vlog': _travel_scene, 'product-unboxing': _desk_scene}

# (component, text, start frame, duration frames, position override, optional target)
TIMELINES = {
    'travel-vlog': [
        ('program_title', None, 4, 90, None, None),
        ('location', '淺草寺\nTOKYO', 70, 170, None, None),
        ('fact', '雷門\n免費參觀', 112, 66, {'x': .56, 'y': .44}, None),
        ('reaction', '人也太多了吧！', 192, 46, None, None),
    ],
    'product-unboxing': [
        ('program_title', None, 4, 90, None, None),
        ('chapter', '開箱\nUNBOXING', 76, 164, None, None),
        ('pointer', '充電盒', 118, 66, {'x': .3, 'y': .36}, {'x': .715, 'y': .66, 'radius': .05}),
        ('fact', '無線耳機\nUSB-C 充電', 118, 66, {'x': .06, 'y': .5}, None),
        ('reaction', '比想像中小！', 196, 42, {'x': .52, 'y': .14}, None),
    ],
}


# (text, start frame, duration frames, emphasised words)
SUBTITLES = {
    'travel-vlog': [('今天要去東京最熱門的地方', 8, 80, ['最熱門']), ('這就是傳說中的雷門', 100, 84, ['雷門']),
                    ('等一下 這人潮也太誇張', 190, 50, ['太誇張'])],
    'product-unboxing': [('今天來開這副新耳機', 8, 80, []), ('盒子裡面有充電盒跟線', 100, 92, ['充電盒']),
                         ('拿起來比想像中小很多', 196, 44, ['小很多'])],
}
# A composed still for the gallery cover: (component, text, position override, target) all at rest.
COVERS = {
    'travel-vlog': ([('program_title', None, None, None), ('location', '淺草寺\nTOKYO', None, None),
                     ('reaction', '人也太多了吧！', {'x': .58, 'y': .16}, None)], '這就是傳說中的雷門', ['雷門']),
    'product-unboxing': ([('program_title', None, None, None), ('chapter', '開箱\nUNBOXING', None, None),
                          ('fact', '無線耳機\nUSB-C 充電', {'x': .06, 'y': .56}, None),
                          ('reaction', '比想像中小！', {'x': .56, 'y': .12}, None)], '拿起來比想像中小很多', ['小很多']),
}


def _presets(template, skill):
    _, presets = style_presets(skill, template['visual_style_id'])
    return font_presets(skill, presets, template['font_style_id'])[1]


def _timeline():
    return EditTimeline('template-sample', 'template-sample', None, None, None, Rational(FPS, 1), WIDTH, HEIGHT,
                        DURATION * FPS, (Track('video', TrackKind.VIDEO, ()),), production_session_id='template-sample')


def _frame(scene, frame):
    # A slow push-in stands in for handheld footage.
    zoom = 1.04 + .03 * frame / (FPS * DURATION)
    cw, ch = WIDTH / zoom, HEIGHT / zoom
    left, top = (scene.width - cw) * (.4 + .1 * frame / (FPS * DURATION)), (scene.height - ch) / 2
    return scene.crop((round(left), round(top), round(left + cw), round(top + ch))).resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS).convert('RGBA')


def _overlay(image, template, presets, elements, subtitle, frame):
    font = TitleCardRenderer._font_path()
    timeline = _timeline()
    layer = Image.new('RGBA', image.size)
    pen = ImageDraw.Draw(layer)
    positions = presets['component_positions']
    for kind, text, start, length, position, target in elements:
        text = text or template['sample_title'] + '\n' + template['sample_detail']
        profile = {'kind': 'program_component', 'component_id': kind, 'presets': presets, **(position or positions[kind])}
        if target:
            profile['target'] = target
        draw_program_component(pen, font, timeline, TextClip('sample', start, length, text, 'sample'), profile, frame - start)
    if subtitle:
        text, start, length, emphasis = subtitle
        draw_program_subtitle(pen, font, timeline, TextClip('caption', start, length, text, 'caption'),
                              {'font_size_ratio': .05, 'emphasis': emphasis, 'emphasis_color': presets['palette']['accent']}, frame - start)
    image = Image.alpha_composite(image, layer)
    note = load_style_font(font, 11, {'presets': presets, 'component_id': 'fact'}, role='detail')
    draw_style_text(ImageDraw.Draw(image), (24, HEIGHT - 22), '示意畫面 · 版面與動態示例 · 無聲', note, (255, 255, 255, 150))
    return image.convert('RGB')


def render(template, skill, scene, frame):
    presets = _presets(template, skill)
    identifier = template['template_id']
    elements = [entry for entry in TIMELINES[identifier] if entry[2] <= frame < entry[2] + entry[3]]
    subtitle = next((entry for entry in SUBTITLES[identifier] if entry[1] <= frame < entry[1] + entry[2]), None)
    return _overlay(_frame(scene, frame), template, presets, elements, subtitle, frame)


def cover(template, skill, scene):
    presets = _presets(template, skill)
    parts, text, emphasis = COVERS[template['template_id']]
    rest = 60  # every element shown after its entrance has settled
    elements = [(kind, value, 0, 999, position, target) for kind, value, position, target in parts]
    return _overlay(_frame(scene, COVER_FRAME), template, presets, elements, (text, 0, 999, emphasis), rest)


def main(selected):
    root = Path(__file__).resolve().parents[1]
    skill = {name: json.loads((root / (name + '.json')).read_text()) for name in ('presets', 'visual_styles', 'font_styles')}
    for template in json.loads((root / 'templates.json').read_text()):
        if template['template_id'] not in SCENES or (selected and template['template_id'] not in selected):
            continue
        scene = SCENES[template['template_id']](round(WIDTH * 1.12), round(HEIGHT * 1.12))
        cover(template, skill, scene).save(root / template['preview_resource'], optimize=True)
        with tempfile.TemporaryDirectory(prefix='clipen-template-') as tmp:
            for frame in range(DURATION * FPS):
                render(template, skill, scene, frame).save(Path(tmp) / f'{frame:03}.png')
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-framerate', str(FPS), '-i', tmp + '/%03d.png', '-c:v', 'libx264',
                            '-preset', 'slow', '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-an',
                            str(root / template['sample_resource'])], check=True)
        print(template['template_id'], flush=True)


if __name__ == '__main__':
    main(set(sys.argv[1:]))
