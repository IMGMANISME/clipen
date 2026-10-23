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
            'reaction':('真的好吃！',None),
            'program_title':('城市慢遊計畫','本集主題｜風格示範'),
            'headline':('巷弄裡的新發現','主標設計示範'),
            'impact':('這也太驚喜！',None),
            'chapter':('下一站，街角咖啡','章節設計示範'),
            'burst':('有驚喜！',None)}
    for component in components:
        kind=component['component_id']
        image=Image.new('RGBA',(640,360),(88,107,115,255));draw=ImageDraw.Draw(image)
        # A schematic surface is explicitly a style example, never footage.
        draw.rectangle((0,230,640,360),fill=(61,73,76,255))
        draw.rectangle((420,145,565,280),fill=(161,176,168,255))
        draw.ellipse((448,158,534,244),fill=(230,210,155,255))
        background=image.copy();image=Image.new('RGBA',(640,360));draw=ImageDraw.Draw(image)
        label,detail=labels[kind]
        profile={'kind':'program_component','component_id':kind,'presets':presets,
                 **component['default_position']}
        if component['requires_target']:profile['target']={'x':.77,'y':.56,'radius':.09}
        item=TextClip('example',0,120,label+('\n'+detail if detail else ''),'example')
        draw_program_component(draw,font_path,timeline,item,profile,28)
        image=Image.alpha_composite(background,image);draw=ImageDraw.Draw(image)
        small=ImageFont.truetype(str(font_path),12)
        draw.text((18,331),'STYLE EXAMPLE · 風格示範，非來源素材',font=small,fill=(255,255,255,255))
        image.convert('RGB').save(directory/(kind+'.png'),optimize=True)


    from clipen.skills.visual_styles import style_presets
    styles=json.loads((root/'visual_styles.json').read_text())
    for style in styles:
        if style['visual_style_id'].startswith('tpl-'):continue
        identifier,selected=style_presets({'presets':presets,'visual_styles':styles},style['visual_style_id'])
        image=Image.new('RGBA',(640,360),(88,107,115,255));draw=ImageDraw.Draw(image)
        draw.rectangle((0,230,640,360),fill=(61,73,76,255))
        draw.rectangle((420,145,565,280),fill=(161,176,168,255))
        background=image.copy();image=Image.new('RGBA',(640,360));draw=ImageDraw.Draw(image)
        for kind,text,x,y in [('program_title','城市慢遊\n街角咖啡篇',.075,.15),
                              ('fact','本集主題\n街角咖啡',.06,.58),('reaction','出發！',.62,.52)]:
            profile={'kind':'program_component','component_id':kind,'presets':selected,'x':x,'y':y}
            draw_program_component(draw,font_path,timeline,TextClip('example',0,120,text,'example'),profile,35)
        Image.alpha_composite(background,image).convert('RGB').save(directory/('style-'+identifier+'.png'),optimize=True)

    from clipen.skills.visual_styles import font_presets
    from clipen.rendering.program_styles import load_style_font, draw_style_text
    fonts=json.loads((root/'font_styles.json').read_text())
    for style in fonts:
        if 'preview_resource' not in style:continue
        _,selected=font_presets({'font_styles':fonts},presets,style['font_style_id'])
        profile={'presets':selected,'component_id':'program_title'}
        image=Image.new('RGBA',(640,280),(255,248,229,255));draw=ImageDraw.Draw(image)
        labels=ImageFont.truetype(str(font_path),14)
        for role,title,value,size,y in [('title','主標','咖啡・餐桌時光',45,28),
                                       ('impact','重點字','先拍，還是先吃？',34,115),
                                       ('information','資訊','酪梨吐司　街角小日常',26,202)]:
            draw.text((20,y+6),title,font=labels,fill=(107,113,104,255))
            selected_font=load_style_font(font_path,size,profile,role=role)
            draw_style_text(draw,(95,y),value,selected_font,(22,43,48,255))
        image.convert('RGB').save(root/style['preview_resource'],optimize=True)

    import runpy
    runpy.run_path(str(root/'scripts'/'render_templates.py'),run_name='__main__')


if __name__=='__main__':main()
