"""Render original eight-second channel-package studies with the film renderer.

The twelve sets below are procedural illustrations, never external footage. The
actual overlaid type/labels/motion come from draw_program_component, not mockups.
"""
import json
import math
from pathlib import Path
import subprocess
import tempfile

from PIL import Image, ImageDraw
from clipen.domain import EditTimeline, Rational, TextClip, Track, TrackKind
from clipen.rendering.program_components import draw_program_component
from clipen.rendering.program_styles import load_style_font, draw_style_text
from clipen.rendering.titles import TitleCardRenderer
from clipen.skills.visual_styles import style_presets, font_presets

WIDTH, HEIGHT, DURATION = 960, 540, 8


def illustration(name, palette, frame):
    """A different original set for each package; coordinates use a 640x360 artboard."""
    ink, paper, accent, secondary = (tuple(palette[k]) for k in ('ink', 'paper', 'accent', 'secondary'))
    dark = name in {'night', 'mission', 'outdoor', 'tech', 'portrait'}
    image = Image.new('RGBA', (640, 360), ink if dark else paper)
    d = ImageDraw.Draw(image)
    shift = math.sin(frame/100)*5
    if name == 'journal':
        for y in range(44, 360, 24): d.line((0,y,640,y), fill=secondary, width=1)
        d.line((65,0,65,360), fill=accent, width=2)
        d.polygon([(360,33),(593,52),(575,306),(337,278)],fill=ink)
        d.polygon([(371,40),(583,62),(568,292),(349,271)],fill=paper)
        d.polygon([(385,61),(565,80),(551,235),(369,216)],fill=secondary)
        d.polygon([(369,216),(437,113),(478,175),(514,127),(551,235)], fill=ink)
        d.ellipse((497,93,526,122),fill=accent)
        d.rectangle((452,24,510,48),fill=accent)
        d.arc((470,267,568,326),10,345,fill=accent,width=3)
    elif name == 'city':
        for x in range(320,640,40):d.line((x,0,x,360),fill=secondary)
        for y in range(0,360,40):d.line((300,y,640,y),fill=secondary)
        for n in range(8):
            x=370+n*29;top=122+(n%3)*28
            d.rectangle((x,top,x+23,291),fill=ink)
            for yy in range(top+12,278,19):d.rectangle((x+7,yy,x+14,yy+8),fill=paper)
        d.ellipse((467,51,583,167),outline=accent,width=5)
        d.line((519,168,469,204,402,204),fill=accent,width=4)
        d.rectangle((338,308,620,317),fill=ink)
    elif name == 'slow':
        # Cut-paper landscape, with very slow parallax in the original set.
        d.rectangle((0,0,640,360),fill=(192,205,188,255))
        d.ellipse((446,39,551,144),fill=paper)
        d.polygon([(0,228),(142,127+shift),(280,225),(422,151),(640,238),(640,360),(0,360)],fill=secondary)
        d.polygon([(0,290),(160,218),(270,278),(425,211+shift),(640,294),(640,360),(0,360)],fill=(83,110,96,255))
        d.polygon([(0,332),(194,282),(307,325),(482,264),(640,311),(640,360),(0,360)],fill=ink)
    elif name == 'night':
        d.ellipse((413,32,516,135),fill=secondary)
        for n in range(9):
            x=295+n*42;top=100+(n%4)*26
            d.rectangle((x,top,x+35,350),fill=(23+n*2,30,55+n*2,255))
            for yy in range(top+15,330,28):
                for xx in (x+6,x+22):d.rectangle((xx,yy,xx+6,yy+12),fill=accent if (n+yy)%3 else secondary)
        d.polygon([(368,360),(443,236),(467,236),(615,360)],fill=(60,43,75,255))
        for n in range(5):d.line((465+n*12,258+n*22,485+n*23,258+n*22),fill=secondary,width=2)
    elif name == 'food':
        d.rectangle((0,0,640,360),fill=secondary)
        for x in range(-100,700,60):d.line((320,180,x,0),fill=paper,width=2)
        for y in range(0,361,45):d.line((320,180,640,y),fill=paper,width=2)
        d.ellipse((340,115,614,322),fill=accent)
        d.pieslice((366,142,590,324),0,180,fill=ink)
        d.ellipse((366,142,590,232),fill=paper,outline=ink,width=5)
        for n in range(6):d.arc((389+n*15,152,434+n*15,217),10,300,fill=accent,width=3)
        d.ellipse((467,148,515,190),fill=secondary,outline=ink,width=2)
        d.line((493,103,604,46),fill=ink,width=6);d.line((503,115,618,64),fill=ink,width=6)
    elif name == 'mission':
        for x in range(-300,700,70):d.polygon([(x,0),(x+28,0),(x+198,360),(x+170,360)],fill=(34,35,33,255))
        d.rectangle((395,66,595,290),outline=accent,width=2)
        for n in range(3):
            yy=112+n*54
            d.rectangle((420,yy,441,yy+21),outline=accent,width=2)
            if n<2:d.line((424,yy+11,431,yy+17,440,yy+4),fill=accent,width=3)
            d.rectangle((457,yy+6,566-n*12,yy+12),fill=secondary)
        for xx,yy in ((380,52),(611,52),(380,305),(611,305)):
            d.line((xx-7,yy,xx+7,yy),fill=accent,width=2);d.line((xx,yy-7,xx,yy+7),fill=accent,width=2)
    elif name == 'coffee':
        for xx in range(365,641,28):d.line((xx,0,xx,360),fill=secondary)
        d.ellipse((347,267,617,316),fill=secondary)
        d.ellipse((357,260,607,304),fill=paper,outline=ink,width=2)
        d.rounded_rectangle((404,140,562,278),radius=32,fill=paper,outline=ink,width=4)
        d.arc((520,163,608,241),-90,90,fill=ink,width=7)
        d.ellipse((404,128,562,177),fill=accent,outline=ink,width=3)
        for n in range(3):d.arc((438+n*30,53+shift,462+n*30,126+shift),90,270,fill=ink,width=2)
        d.line((365,332,605,332),fill=accent,width=2)
    elif name == 'family':
        d.rectangle((0,0,640,360),fill=(235,193,181,255))
        for n in range(3):
            xx=346+n*73;yy=55+n*38
            d.rectangle((xx,yy,xx+111,yy+156),fill=paper)
            d.rectangle((xx+10,yy+10,xx+101,yy+112),fill=secondary)
            d.ellipse((xx+43,yy+34,xx+69,yy+60),fill=accent)
            d.rounded_rectangle((xx+29,yy+64,xx+82,yy+113),radius=19,fill=ink)
            d.rectangle((xx+27,yy-8,xx+77,yy+9),fill=accent)
        d.arc((414,269,589,325),5,170,fill=ink,width=3)
    elif name == 'outdoor':
        for n in range(11):
            d.arc((310-n*13,40-n*15,604+n*20,308+n*15),195,565,fill=secondary,width=1)
        d.polygon([(370,287),(462,115),(533,235),(568,172),(628,287)],fill=secondary)
        d.polygon([(430,174),(462,115),(490,165),(469,154),(455,170)],fill=paper)
        path=[(389,312),(432,286),(413,249),(459,230),(447,200),(472,179)]
        d.line(path,fill=accent,width=3)
        for xx,yy in (path[0],path[-1]):d.ellipse((xx-5,yy-5,xx+5,yy+5),fill=paper)
        d.ellipse((540,41,595,96),outline=accent,width=2)
        d.polygon([(568,50),(578,83),(568,76),(557,83)],fill=accent)
    elif name == 'portrait':
        d.rectangle((367,0,640,360),fill=secondary)
        d.ellipse((423,65,555,224),fill=ink)
        d.ellipse((418,72,531,224),fill=(107,94,83,255))
        d.polygon([(428,196),(526,194),(543,236),(594,360),(362,360),(401,237)],fill=ink)
        d.line((390,30,390,330),fill=paper,width=1)
        d.line((390,330,607,330),fill=paper,width=1)
        d.rectangle((583,59,590,141),fill=accent)
    elif name == 'retro':
        d.rectangle((0,0,640,360),fill=(229,200,144,255))
        for n in range(7):d.polygon([(300+n*48,0),(322+n*48,0),(420+n*48,360),(398+n*48,360)],fill=secondary)
        d.rounded_rectangle((375,75,594,293),radius=28,fill=ink)
        d.rounded_rectangle((390,96,579,194),radius=12,fill=paper)
        d.line((485,96,485,194),fill=ink,width=6)
        for xx in (418,548):d.ellipse((xx-12,222,xx+12,246),fill=accent)
        d.line((403,310,577,310),fill=ink,width=7)
        d.line((420,284,386,360),fill=ink,width=4);d.line((550,284,584,360),fill=ink,width=4)
    else:
        for xx in range(0,640,30):d.line((xx,0,xx,360),fill=(33,42,52,255))
        for yy in range(0,360,30):d.line((0,yy,640,yy),fill=(33,42,52,255))
        d.rounded_rectangle((414,43,571,315),radius=24,fill=(30,38,47,255),outline=accent,width=3)
        d.rounded_rectangle((424,55,561,303),radius=17,outline=secondary,width=1)
        d.rounded_rectangle((461,61,524,74),radius=5,fill=ink)
        d.ellipse((438,96,482,140),outline=accent,width=2);d.ellipse((493,96,537,140),outline=secondary,width=2)
        for n in range(5):d.line((442,174+n*20,542-n*8,174+n*20),fill=accent if n==0 else secondary,width=2)
        for xx,yy,side in ((402,100,-1),(582,251,1)):
            d.line((xx,yy,xx+side*29,yy,xx+side*29,yy-32),fill=accent,width=2)
    return image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def render(template, skill, frame):
    _, presets = style_presets(skill, template['visual_style_id'])
    _, presets = font_presets(skill, presets, template['font_style_id'])
    name=template['template_id'];frame=min(DURATION*30-1,frame)
    image=illustration(name,presets['palette'],frame)
    font=TitleCardRenderer._font_path()
    profile={'presets':presets,'component_id':'fact'}
    micro=load_style_font(font,12,profile,role='detail')
    d=ImageDraw.Draw(image)
    dark=name in {'night','mission','outdoor','tech','portrait','slow'}
    ink=tuple(presets['palette']['paper' if dark else 'ink'])
    draw_style_text(d,(32,20),'CLIPEN ORIGINAL / '+name.upper(),micro,ink)
    draw_style_text(d,(32,HEIGHT-26),'原創插畫場景 · 版面與動態示例 · 無聲',micro,ink)
    timeline=EditTimeline('template-sample','template-sample',None,None,None,Rational(30,1),WIDTH,HEIGHT,DURATION*30,(Track('video',TrackKind.VIDEO,()),),production_session_id='template-sample')
    phase, local=divmod(frame,60)
    if phase==0:
        position=presets['component_positions']['program_title']
        elements=[('program_title',template['sample_title']+'\n'+template['sample_detail'],position['x'],position['y'])]
    elif phase==1:
        elements=[('chapter','從這裡開始',.065,.13),('location','下一站的故事',.065,.53)]
    elif phase==2:
        elements=[('fact','看見這個細節\n資訊跟著畫面走',.065,.17),('pointer','看這裡',.065,.49)]
        if name=='food':
            elements=[('fact','日常的一碗\n第一種選擇',.065,.19),('fact','另一種滋味\n第二種選擇',.54,.19)]
        elif name=='tech':
            elements=[('fact','實測項目\n續航與操作手感',.065,.18),('pointer','操作細節',.065,.49)]
        elif name=='coffee':
            elements=[('fact','風味筆記\n從一個細節開始',.065,.24)]
        elif name=='portrait':
            elements=[('person','畫面裡的他\n從一句原話開始',.065,.43)]
    else:
        elements=[('impact','就是這一刻',.065,.18),('person','故事裡的主角',.065,.55)]
        if name=='slow':elements=[('headline','不急著到達',.28,.43)]
        elif name=='family':elements=[('reaction','今天也很忙',.12,.22),('person','我們家的日常',.12,.55)]
    layer=Image.new('RGBA',image.size);pen=ImageDraw.Draw(layer)
    for kind,text,x,y in elements:
        profile={'kind':'program_component','component_id':kind,'presets':presets,'x':x,'y':y}
        if kind=='pointer':profile['target']={'x':.78,'y':.47,'radius':.055}
        draw_program_component(pen,font,timeline,TextClip('sample',phase*60,60,text,'sample'),profile,local)
    return Image.alpha_composite(image,layer).convert('RGB')


def main():
    root=Path(__file__).resolve().parents[1]
    skill={name:json.loads((root/(name+'.json')).read_text()) for name in ('presets','visual_styles','font_styles')}
    for template in json.loads((root/'templates.json').read_text()):
        render(template,skill,32).save(root/template['preview_resource'],optimize=True)
        with tempfile.TemporaryDirectory(prefix='clipen-template-') as tmp:
            for n in range(DURATION*12):render(template,skill,round(n*30/12)).save(Path(tmp)/f'{n:03}.png')
            subprocess.run(['ffmpeg','-v','error','-y','-framerate','12','-i',tmp+'/%03d.png','-vf','fps=30','-c:v','libx264','-preset','fast','-crf','22','-pix_fmt','yuv420p','-movflags','+faststart','-an',str(root/template['sample_resource'])],check=True)
        print(template['template_id'],flush=True)


if __name__=='__main__':main()
