"""Regenerate original style examples using the same trusted production renderer."""
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from clipen.domain import EditTimeline, Rational, TextClip, Track, TrackKind
from clipen.rendering.program_components import draw_program_component
from clipen.rendering.titles import TitleCardRenderer


def main():
    root=Path(__file__).resolve().parents[1]
    presets=json.loads((root/'presets.json').read_text())
    components=json.loads((root/'components.json').read_text())
    directory=root/'assets';directory.mkdir(exist_ok=True)
    font_path=TitleCardRenderer._font_path()
    if font_path is None:raise RuntimeError('CJK font is required to render style examples')
    timeline=EditTimeline('style-example','style-example',None,None,None,Rational(30,1),640,360,120,
                          (Track('video',TrackKind.VIDEO,()),),production_session_id='style-example')
    labels={'location':('示範街區','地點資訊融入正在發生的畫面'),
            'person':('人物名稱','有依據的角色介紹'),
            'fact':('招牌餐點','價格與品名以來源為準'),
            'pointer':('看這個細節',None),'circle':('畫面重點',None),
            'reaction':('真的好吃！',None)}
    for component in components:
        kind=component['component_id']
        image=Image.new('RGBA',(640,360),(88,107,115,255));draw=ImageDraw.Draw(image)
        # A schematic surface is explicitly a style example, never footage.
        draw.rectangle((0,230,640,360),fill=(61,73,76,255))
        draw.rectangle((420,145,565,280),fill=(161,176,168,255))
        draw.ellipse((448,158,534,244),fill=(230,210,155,255))
        label,detail=labels[kind]
        profile={'kind':'program_component','component_id':kind,'presets':presets,
                 **component['default_position']}
        if component['requires_target']:profile['target']={'x':.77,'y':.56,'radius':.09}
        item=TextClip('example',0,120,label+('\n'+detail if detail else ''),'example')
        draw_program_component(draw,font_path,timeline,item,profile,8)
        small=ImageFont.truetype(str(font_path),12)
        draw.text((18,331),'STYLE EXAMPLE · 風格示範，非來源素材',font=small,fill=(255,255,255,255))
        image.convert('RGB').save(directory/(kind+'.png'),optimize=True)


if __name__=='__main__':main()
