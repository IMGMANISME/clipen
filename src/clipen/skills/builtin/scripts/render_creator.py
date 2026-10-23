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


def _bokeh(pen, width, height, colors, count, seed, size=(.03, .08)):
    import random
    rng = random.Random(seed)
    for _ in range(count):
        r = rng.uniform(*size) * height
        x, y = rng.uniform(0, width), rng.uniform(0, height * .7)
        pen.ellipse((x - r, y - r, x + r, y + r), fill=rng.choice(colors) + (rng.randint(60, 150),))


def _iceland_scene(width, height):
    scene = _gradient((width, height), [(0, (60, 86, 120)), (.45, (170, 196, 214)), (.55, (214, 222, 226)),
                                        (.555, (88, 112, 128)), (1, (40, 56, 66))])
    pen = ImageDraw.Draw(scene, 'RGBA')
    pen.polygon(_hills(width, height, height * .55, height * .22, 1.2), fill=(120, 134, 150, 230))
    pen.polygon(_hills(width, height, height * .56, height * .12, 3.4), fill=(70, 84, 98, 240))
    for n in range(14):  # snow caps
        x = width * (.1 + n * .065)
        pen.polygon([(x, height * .4), (x + width * .03, height * .36), (x + width * .06, height * .41)], fill=(236, 240, 244, 150))
    pen.rectangle((0, height * .56, width, height), fill=(96, 122, 138, 255))
    for n in range(20):
        y = height * (.58 + n * .02)
        pen.line((0, y, width, y), fill=(160, 186, 200, 60), width=2)
    return _grain(scene.filter(ImageFilter.GaussianBlur(radius=height * .008)))


def _restaurant_scene(width, height):
    scene = _gradient((width, height), [(0, (70, 44, 30)), (.5, (120, 78, 50)), (.52, (92, 58, 36)), (1, (60, 36, 22))])
    pen = ImageDraw.Draw(scene, 'RGBA')
    _bokeh(pen, width, height * .7, [(255, 196, 120), (255, 160, 90), (255, 220, 170)], 26, 3)
    cx, cy, r = width * .66, height * .66, height * .26
    pen.ellipse((cx - r * 1.1, cy - r * .55 + r * .12, cx + r * 1.1, cy + r * .55 + r * .25), fill=(30, 18, 10, 120))
    pen.ellipse((cx - r, cy - r * .5, cx + r, cy + r * .5), fill=(244, 240, 232, 255))
    pen.ellipse((cx - r * .82, cy - r * .38, cx + r * .82, cy + r * .38), fill=(176, 92, 52, 255))
    for n in range(7):  # sliced beef
        x = cx - r * .5 + n * r * .16
        pen.ellipse((x, cy - r * .18, x + r * .24, cy + r * .02), fill=(214, 120, 120, 255))
    for n in range(5):  # scallions
        pen.ellipse((cx + r * (n * .1 - .2), cy + r * .08, cx + r * (n * .1 - .14), cy + r * .14), fill=(120, 190, 90, 255))
    return _grain(scene.filter(ImageFilter.GaussianBlur(radius=height * .006)))


def _home_scene(width, height):
    scene = _gradient((width, height), [(0, (236, 226, 212)), (.6, (244, 236, 226)), (.61, (200, 168, 130)), (1, (176, 140, 104))])
    pen = ImageDraw.Draw(scene, 'RGBA')
    pen.rectangle((width * .06, height * .08, width * .38, height * .52), fill=(255, 252, 240, 255))  # window light
    pen.line((width * .22, height * .08, width * .22, height * .52), fill=(220, 210, 196, 255), width=6)
    for n in range(6):  # plant leaves
        x = width * (.82 + .02 * math.sin(n))
        pen.ellipse((x - width * .05, height * (.3 + n * .04), x + width * .02, height * (.34 + n * .04)), fill=(96, 140, 90, 255))
    pen.rectangle((width * .8, height * .5, width * .86, height * .62), fill=(200, 120, 90, 255))
    cx, cy = width * .55, height * .6  # cake
    pen.ellipse((cx - width * .14, cy + height * .1, cx + width * .14, cy + height * .16), fill=(120, 90, 60, 90))
    pen.rectangle((cx - width * .12, cy - height * .08, cx + width * .12, cy + height * .12), fill=(250, 236, 220, 255))
    pen.ellipse((cx - width * .12, cy - height * .13, cx + width * .12, cy - height * .03), fill=(255, 246, 236, 255))
    pen.rectangle((cx - width * .12, cy + height * .01, cx + width * .12, cy + height * .03), fill=(240, 150, 160, 255))
    for n in range(5):
        pen.ellipse((cx + width * (n * .045 - .09), cy - height * .12, cx + width * (n * .045 - .07), cy - height * .09), fill=(214, 60, 70, 255))
    return _grain(scene.filter(ImageFilter.GaussianBlur(radius=height * .006)), .035)


def _laptop_scene(width, height):
    scene = _gradient((width, height), [(0, (34, 38, 46)), (.6, (46, 50, 60)), (.61, (64, 58, 54)), (1, (42, 38, 34))])
    pen = ImageDraw.Draw(scene, 'RGBA')
    pen.polygon([(width * .36, height * .82), (width * .9, height * .82), (width * .96, height * .9), (width * .3, height * .9)], fill=(160, 164, 172, 255))
    pen.rectangle((width * .4, height * .26, width * .86, height * .8), fill=(20, 22, 26, 255))
    pen.rectangle((width * .42, height * .29, width * .84, height * .77), fill=(40, 70, 120, 255))
    pen.rectangle((width * .44, height * .32, width * .58, height * .74), fill=(28, 44, 76, 255))  # sidebar
    for n in range(4):
        pen.rectangle((width * .6, height * (.34 + n * .1), width * .82, height * (.4 + n * .1)), fill=(80, 130, 200, 255))
    pen.rectangle((width * .6, height * .66, width * .7, height * .72), fill=(80, 214, 255, 255))  # the button the demo points at
    return _grain(scene.filter(ImageFilter.GaussianBlur(radius=height * .004)), .04)


def _studio_scene(width, height):
    scene = _gradient((width, height), [(0, (96, 100, 108)), (.7, (150, 154, 160)), (1, (120, 124, 130))])
    pen = ImageDraw.Draw(scene, 'RGBA')
    pen.ellipse((width * .42, height * .55, width * .9, height * .95), fill=(250, 250, 255, 40))  # soft light pool
    pen.rectangle((width * .6, height * .62, width * .7, height * .86), fill=(40, 42, 46, 255))  # stand
    pen.rounded_rectangle((width * .48, height * .34, width * .82, height * .62), radius=round(height * .03), fill=(28, 30, 34, 255))  # camera body
    pen.rounded_rectangle((width * .5, height * .28, width * .6, height * .34), radius=round(height * .01), fill=(34, 36, 40, 255))
    cx, cy = width * .65, height * .48
    for r, color in ((.12, (18, 18, 20)), (.09, (54, 58, 66)), (.06, (20, 26, 40)), (.025, (120, 140, 180))):
        pen.ellipse((cx - height * r, cy - height * r, cx + height * r, cy + height * r), fill=color + (255,))
    return _grain(scene.filter(ImageFilter.GaussianBlur(radius=height * .004)), .035)


def _party_scene(width, height):
    scene = _gradient((width, height), [(0, (40, 26, 30)), (.6, (90, 56, 52)), (1, (60, 36, 34))])
    pen = ImageDraw.Draw(scene, 'RGBA')
    _bokeh(pen, width, height, [(255, 214, 150), (255, 180, 120), (255, 236, 200), (250, 170, 170)], 60, 7, (.015, .05))
    for n in range(24):  # string lights
        x = width * n / 23
        y = height * (.12 + .05 * math.sin(n / 23 * math.pi))
        pen.ellipse((x - 6, y - 6, x + 6, y + 6), fill=(255, 232, 170, 230))
    pen.rectangle((0, height * .78, width, height), fill=(250, 246, 240, 255))  # table cloth
    for n in range(3):  # flowers
        x = width * (.3 + n * .2)
        pen.ellipse((x - width * .05, height * .66, x + width * .05, height * .8), fill=(250, 200, 210, 255))
    return _grain(scene.filter(ImageFilter.GaussianBlur(radius=height * .01)))


def _night_street_scene(width, height):
    scene = _gradient((width, height), [(0, (18, 20, 44)), (.6, (40, 30, 70)), (1, (20, 16, 30))])
    pen = ImageDraw.Draw(scene, 'RGBA')
    for n in range(6):  # buildings either side
        pen.rectangle((0, height * (.1 + n * .05), width * (.18 + .03 * (n % 2)), height), fill=(26, 24, 40, 255))
        pen.rectangle((width * (.8 - .03 * (n % 2)), height * (.12 + n * .05), width, height), fill=(26, 24, 40, 255))
    _bokeh(pen, width, height, [(255, 90, 160), (90, 220, 255), (255, 214, 90), (255, 255, 255)], 40, 11, (.01, .035))
    for n in range(8):  # neon signs
        x = width * (.04 if n % 2 else .82)
        pen.rectangle((x, height * (.2 + n * .08), x + width * .12, height * (.23 + n * .08)), fill=[(255, 90, 160), (90, 220, 255)][n % 2] + (220,))
    pen.rectangle((width * .25, height * .7, width * .75, height), fill=(36, 34, 52, 255))  # street
    return _grain(scene.filter(ImageFilter.GaussianBlur(radius=width * .008)))


SCENES = {'travel-vlog': _travel_scene, 'product-unboxing': _desk_scene, 'travel-highlight': _iceland_scene,
          'food-review': _restaurant_scene, 'daily-vlog': _home_scene, 'product-demo': _laptop_scene,
          'product-review': _studio_scene, 'event-recap': _party_scene, 'shorts-highlight': _night_street_scene}
SIZE = {'shorts-highlight': (540, 960)}
LETTERBOX = {'travel-highlight'}


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
    'travel-highlight': [
        ('program_title', None, 10, 110, None, None),
        ('location', '斯奈菲爾半島\nSNAEFELLSNES', 130, 110, None, None),
        ('reaction', '太美了吧', 190, 48, {'x': .58, 'y': .22}, None),
    ],
    'food-review': [
        ('program_title', None, 4, 84, None, None),
        ('location', '阿村牛肉湯\n台南', 70, 170, None, None),
        ('fact', '牛肉湯\n$150', 104, 80, {'x': .08, 'y': .48}, None),
        ('reaction', '好甜！', 192, 46, {'x': .58, 'y': .18}, None),
    ],
    'daily-vlog': [
        ('program_title', None, 4, 90, None, None),
        ('chapter', '下午\n廚房', 84, 156, None, None),
        ('pointer', '草莓', 120, 64, {'x': .26, 'y': .26}, {'x': .55, 'y': .48, 'radius': .05}),
        ('impact', '成功了！', 196, 42, {'x': .1, 'y': .2}, None),
    ],
    'product-demo': [
        ('program_title', None, 4, 84, None, None),
        ('chapter', 'STEP 1\n匯入素材', 76, 164, None, None),
        ('pointer', '點這裡', 112, 76, {'x': .1, 'y': .34}, {'x': .65, 'y': .69, 'radius': .045}),
        ('fact', '快捷鍵\nCmd + I', 112, 76, {'x': .08, 'y': .52}, None),
        ('reaction', '超簡單！', 196, 42, {'x': .54, 'y': .12}, None),
    ],
    'product-review': [
        ('program_title', None, 4, 84, None, None),
        ('fact', '優點\n對焦很快', 80, 64, None, None),
        ('fact', '缺點\n機身有點重', 136, 56, None, None),
        ('headline', '值得買嗎？', 196, 44, {'x': .1, 'y': .2}, None),
    ],
    'event-recap': [
        ('program_title', None, 10, 110, None, None),
        ('location', '台北\n晚宴', 130, 110, None, None),
        ('person', '新郎 阿哲\n致詞', 150, 70, {'x': .06, 'y': .46}, None),
    ],
    'shorts-highlight': [
        ('headline', '這也太扯了吧', 4, 70, None, None),
        ('location', '西門町\n台北', 70, 170, None, None),
        ('reaction', '真的假的', 120, 56, None, None),
        ('impact', '一分鐘看完', 186, 54, None, None),
    ],
}


# (text, start frame, duration frames, emphasised words)
SUBTITLES = {
    'travel-vlog': [('今天要去東京最熱門的地方', 8, 80, ['最熱門']), ('這就是傳說中的雷門', 100, 84, ['雷門']),
                    ('等一下 這人潮也太誇張', 190, 50, ['太誇張'])],
    'product-unboxing': [('今天來開這副新耳機', 8, 80, []), ('盒子裡面有充電盒跟線', 100, 92, ['充電盒']),
                         ('拿起來比想像中小很多', 196, 44, ['小很多'])],
    'travel-highlight': [('這是我看過最美的海岸', 150, 80, ['最美'])],
    'food-review': [('清晨五點就來排隊', 8, 70, ['五點']), ('一碗一百五 湯是現熬的', 96, 90, ['一百五']),
                    ('哇 這個湯好甜', 190, 50, ['好甜'])],
    'daily-vlog': [('今天要第一次做蛋糕', 8, 80, ['第一次']), ('最後放上草莓', 100, 90, ['草莓']), ('竟然成功了', 196, 44, ['成功'])],
    'product-demo': [('先把素材匯入', 80, 70, ['匯入']), ('點右下角這個按鈕就好', 152, 70, ['右下角']), ('就這麼簡單', 200, 40, [])],
    'product-review': [('這台相機我用了一個月', 8, 76, ['一個月']), ('對焦真的很快', 84, 56, ['很快']),
                       ('但是機身有點重', 140, 52, ['有點重']), ('到底值不值得買', 196, 44, ['值不值得'])],
    'event-recap': [('謝謝大家今天來參加', 150, 80, ['謝謝大家'])],
    'shorts-highlight': [('等一下 這是什麼', 70, 50, []), ('真的假的', 120, 56, ['真的假的']), ('一分鐘帶你看完', 186, 54, ['一分鐘'])],
}
# A composed still for the gallery cover: (component, text, position override, target) all at rest.
COVERS = {
    'travel-vlog': ([('program_title', None, None, None), ('location', '淺草寺\nTOKYO', None, None),
                     ('reaction', '人也太多了吧！', {'x': .58, 'y': .16}, None)], '這就是傳說中的雷門', ['雷門']),
    'product-unboxing': ([('program_title', None, None, None), ('chapter', '開箱\nUNBOXING', None, None),
                          ('fact', '無線耳機\nUSB-C 充電', {'x': .06, 'y': .56}, None),
                          ('reaction', '比想像中小！', {'x': .56, 'y': .12}, None)], '拿起來比想像中小很多', ['小很多']),
    'travel-highlight': ([('program_title', None, None, None), ('location', '斯奈菲爾半島\nSNAEFELLSNES', None, None)],
                         '這是我看過最美的海岸', ['最美']),
    'food-review': ([('program_title', None, None, None), ('location', '阿村牛肉湯\n台南', None, None),
                     ('fact', '牛肉湯\n$150', {'x': .08, 'y': .56}, None), ('reaction', '好甜！', {'x': .6, 'y': .16}, None)],
                    '一碗一百五 湯是現熬的', ['一百五']),
    'daily-vlog': ([('program_title', None, None, None), ('chapter', '下午\n廚房', None, None),
                    ('impact', '成功了！', {'x': .6, 'y': .14}, None)], '竟然成功了', ['成功']),
    'product-demo': ([('program_title', None, None, None), ('chapter', 'STEP 1\n匯入素材', None, None),
                      ('pointer', '點這裡', {'x': .1, 'y': .56}, {'x': .65, 'y': .69, 'radius': .045})], '點右下角這個按鈕就好', ['右下角']),
    'product-review': ([('program_title', None, None, None), ('fact', '優點\n對焦很快', {'x': .06, 'y': .5}, None),
                        ('fact', '缺點\n機身有點重', {'x': .3, 'y': .5}, None),
                        ('reaction', '值得嗎？', {'x': .6, 'y': .14}, None)], '對焦真的很快', ['很快']),
    'event-recap': ([('program_title', None, None, None), ('location', '台北\n晚宴', None, None)], '謝謝大家今天來參加', ['謝謝大家']),
    'shorts-highlight': ([('headline', '這也太扯了吧', None, None), ('location', '西門町\n台北', None, None),
                          ('reaction', '真的假的', {'x': .3, 'y': .6}, None)], '真的假的', ['真的假的']),
}


def _presets(template, skill):
    _, presets = style_presets(skill, template['visual_style_id'])
    return font_presets(skill, presets, template['font_style_id'])[1]


def _timeline(size):
    width, height = size
    return EditTimeline('template-sample', 'template-sample', None, None, None, Rational(FPS, 1), width, height,
                        DURATION * FPS, (Track('video', TrackKind.VIDEO, ()),), production_session_id='template-sample')


def _frame(scene, frame, size):
    # A slow push-in stands in for handheld footage.
    width, height = size
    zoom = 1.04 + .03 * frame / (FPS * DURATION)
    cw, ch = width / zoom, height / zoom
    left, top = (scene.width - cw) * (.4 + .1 * frame / (FPS * DURATION)), (scene.height - ch) / 2
    return scene.crop((round(left), round(top), round(left + cw), round(top + ch))).resize(size, Image.Resampling.LANCZOS).convert('RGBA')


def _overlay(image, template, presets, elements, subtitle, frame):
    font = TitleCardRenderer._font_path()
    timeline = _timeline(image.size)
    if template['template_id'] in LETTERBOX:  # 2.35:1 bars for the cinematic montage
        bar = round((image.height - image.width / 2.35) / 2)
        ImageDraw.Draw(image).rectangle((0, 0, image.width, bar), fill=(0, 0, 0, 255))
        ImageDraw.Draw(image).rectangle((0, image.height - bar, image.width, image.height), fill=(0, 0, 0, 255))
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
                              {'font_size_ratio': .05 if image.width > image.height else .04, 'emphasis': emphasis,
                               'emphasis_color': presets['palette']['accent']}, frame - start)
    image = Image.alpha_composite(image, layer)
    note = load_style_font(font, 11, {'presets': presets, 'component_id': 'fact'}, role='detail')
    draw_style_text(ImageDraw.Draw(image), (24, image.height - 22), '示意畫面 · 版面與動態示例 · 無聲', note, (255, 255, 255, 150))
    return image.convert('RGB')


def render(template, skill, scene, frame):
    presets = _presets(template, skill)
    identifier = template['template_id']
    elements = [entry for entry in TIMELINES[identifier] if entry[2] <= frame < entry[2] + entry[3]]
    subtitle = next((entry for entry in SUBTITLES[identifier] if entry[1] <= frame < entry[1] + entry[2]), None)
    return _overlay(_frame(scene, frame, SIZE.get(identifier, (WIDTH, HEIGHT))), template, presets, elements, subtitle, frame)


def cover(template, skill, scene):
    presets = _presets(template, skill)
    identifier = template['template_id']
    parts, text, emphasis = COVERS[identifier]
    rest = 60  # every element shown after its entrance has settled
    elements = [(kind, value, 0, 999, position, target) for kind, value, position, target in parts]
    size = SIZE.get(identifier, (WIDTH, HEIGHT))
    still = _overlay(_frame(scene, COVER_FRAME, size), template, presets, elements, (text, 0, 999, emphasis), rest)
    if size == (WIDTH, HEIGHT):
        return still
    # Vertical templates sit on a blurred copy of themselves in the 16:9 gallery card.
    card = still.resize((WIDTH, round(WIDTH * still.height / still.width))).crop((0, 0, WIDTH, HEIGHT)).filter(ImageFilter.GaussianBlur(24))
    frame = still.resize((round(HEIGHT * still.width / still.height), HEIGHT), Image.Resampling.LANCZOS)
    card.paste(frame, ((WIDTH - frame.width) // 2, 0))
    return card


def main(selected):
    root = Path(__file__).resolve().parents[1]
    skill = {name: json.loads((root / (name + '.json')).read_text()) for name in ('presets', 'visual_styles', 'font_styles')}
    for template in json.loads((root / 'templates.json').read_text()):
        if template['template_id'] not in SCENES or (selected and template['template_id'] not in selected):
            continue
        width, height = SIZE.get(template['template_id'], (WIDTH, HEIGHT))
        scene = SCENES[template['template_id']](round(width * 1.12), round(height * 1.12))
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
