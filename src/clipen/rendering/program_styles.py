"""Distinct, trusted art directions for the same semantic programme components."""
import os
import hashlib
from importlib.resources import files
from pathlib import Path
from functools import lru_cache
from PIL import ImageFont


def skin(profile):
    return profile.get('presets',{}).get('appearance',{}).get('skin','variety')


def style_font(font_path, profile):
    if os.environ.get('CLIPEN_TITLE_FONT') or profile['presets'].get('appearance',{}).get('font_family')!='serif':
        return font_path
    for path in ('/System/Library/Fonts/Supplemental/Songti.ttc',
                 '/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc',
                 'C:/Windows/Fonts/simsun.ttc'):
        if Path(path).is_file():return Path(path)
    return font_path



@lru_cache(maxsize=32)
def _font_face(path, direction):
    preferred='Light' if direction=='cinema' else 'Regular'
    fallback=None
    for index in range(16):
        try:family,weight=ImageFont.truetype(path,12,index=index).getname()
        except OSError:break
        if family in {'Songti TC','Noto Serif CJK TC','NotoSerifCJKtc'}:
            if weight==preferred:return index
            if fallback is None:fallback=index
    return fallback or 0


def font_role(profile):
    kind=profile.get('component_id')
    return 'impact' if kind in {'impact','burst','reaction'} else 'title' if kind in {'program_title','headline','chapter'} else 'information'


@lru_cache(maxsize=64)
def _role_face(path, weight):
    preferred={'bold':('Bold','W6','Heavy','Black'), 'medium':('Medium','W6','Regular'),
               'light':('Light','W3','Regular'), 'regular':('Regular','W3','Medium')}[weight]
    faces=[]
    for index in range(32):
        try:family,face=ImageFont.truetype(path,12,index=index).getname()
        except OSError:break
        # Prefer Traditional Chinese faces when a collection contains both.
        if ' SC' not in family and 'Interface' not in family and family!='STSong':
            faces.append((index,face))
    for desired in preferred:
        for index,face in faces:
            if face==desired:return index
    return faces[0][0] if faces else 0


@lru_cache(maxsize=16)
def _verify_font(path, expected, modified_ns, length):
    from .titles import TitleRenderError
    if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=expected:
        raise TitleRenderError('program_font_unavailable')
    return path


def bundled_font(specification):
    from .titles import TitleRenderError
    identifier=specification['bundled_font']
    if identifier not in {'chenyu','iansui'}:
        raise TitleRenderError('program_font_unavailable')
    path=Path(str(files('clipen.rendering').joinpath('fonts',identifier+'.ttf')))
    try:
        stat=path.stat()
        return _verify_font(str(path),specification['sha256'],stat.st_mtime_ns,stat.st_size)
    except (OSError,KeyError) as error:
        raise TitleRenderError('program_font_unavailable') from error


def type_outline(profile, font, fallback):
    specification=profile['presets'].get('font_roles',{}).get(font_role(profile),{})
    ratio=specification.get('outline_em')
    return fallback if ratio is None else max(1,round(font.size*ratio))


def load_style_font(font_path, size, profile, role=None):
    specification=profile['presets'].get('font_roles',{}).get(role or font_role(profile))
    if specification:
        if specification.get('bundled_font') and not os.environ.get('CLIPEN_TITLE_FONT'):
            return ImageFont.truetype(bundled_font(specification),size)
        family=specification['family']
        if family=='handwriting':family='sans'
        candidates={
            'gothic':('/System/Library/Fonts/Hiragino Sans GB.ttc','/System/Library/Fonts/STHeiti Medium.ttc'),
            'sans':('/System/Library/Fonts/STHeiti Medium.ttc','/System/Library/Fonts/Hiragino Sans GB.ttc'),
            'serif':('/System/Library/Fonts/Supplemental/Songti.ttc',),
        }[family]
        candidates+=('/usr/share/fonts/opentype/noto/Noto'+('Serif' if family=='serif' else 'Sans')+'CJK-Regular.ttc',
                     'C:/Windows/Fonts/'+('simsun.ttc' if family=='serif' else 'msjh.ttc'))
        # The explicit local font override still has priority. No arbitrary font
        # paths or remote URLs can be supplied through the editing MCP tools.
        path=str(font_path) if os.environ.get('CLIPEN_TITLE_FONT') else next((p for p in candidates if Path(p).is_file()),str(font_path))
        return ImageFont.truetype(path,size,index=_role_face(path,specification['weight']))
    path=str(style_font(font_path,profile))
    face=_font_face(path,skin(profile)) if profile['presets'].get('appearance',{}).get('font_family')=='serif' else 0
    return ImageFont.truetype(path,size,index=face)



@lru_cache(maxsize=2048)
def _has_glyph(path, size, index, character):
    if character.isspace():return True
    font=ImageFont.truetype(path,size,index=index)
    glyph=font.getmask(character)
    missing=font.getmask('\U0010ffff')
    return glyph.getbbox() is not None and (glyph.size!=missing.size or bytes(glyph)!=bytes(missing))


def _text_runs(value, font):
    from .titles import TitleCardRenderer,TitleRenderError
    fallback=None;runs=[]
    for char in value:
        selected=font
        if not _has_glyph(str(font.path),font.size,font.index,char):
            if fallback is None:
                path=TitleCardRenderer._font_path()
                if path is None:raise TitleRenderError('program_component_missing_glyph')
                fallback=ImageFont.truetype(str(path),font.size)
            if not _has_glyph(str(fallback.path),fallback.size,fallback.index,char):
                raise TitleRenderError('program_component_missing_glyph')
            selected=fallback
        if runs and runs[-1][1] is selected:runs[-1]=(runs[-1][0]+char,selected)
        else:runs.append((char,selected))
    return runs


def styled_text_length(value,font):
    return sum(selected.getlength(part) for part,selected in _text_runs(value,font))


def draw_style_text(draw,position,value,font,fill,stroke_width=0,stroke_fill=None):
    if not value:return
    # Share a real baseline across font runs so a fallback middle dot stays in
    # the middle of CJK type rather than jumping to the top of the line.
    baseline=position[1]-font.getbbox(value,anchor='ls')[1]
    x=position[0]
    for part,selected in _text_runs(value,font):
        draw.text((x,baseline),part,font=selected,fill=fill,anchor='ls',
                  stroke_width=stroke_width,stroke_fill=stroke_fill)
        x+=selected.getlength(part)


def draw_styled_graphic(draw, font_path, timeline, item, profile, bounds, font, pad, relative_frame=None):
    if skin(profile) == 'studio':
        from .program_studio import draw_graphic
        return draw_graphic(draw,font_path,timeline,item,profile,bounds,font,pad,relative_frame)
    kind=profile['component_id'];hero=kind in {'program_title','headline','impact','chapter','burst'}
    appearance=profile['presets'].get('appearance',{})
    motion=profile['presets']['motion']
    entry=appearance.get('hero_entry_frames',18) if hero else motion['entry_frames']
    exit_frames=appearance.get('hero_exit_frames',8) if hero else motion['exit_frames']
    relative=entry if relative_frame is None else relative_frame
    progress=min(1.,max(0.,(relative+1)/entry));ease=1-(1-progress)**3
    opacity=min(progress,min(1.,max(0.,(item.duration_frames-relative)/exit_frames)))
    x,y,right,bottom=bounds;w=right-x;h=bottom-y
    # The full entrance remains within the validated static footprint.
    y+=round((1-ease)*pad*.35)
    primary,_,detail=item.text.partition('\n')
    font_path=style_font(font_path,profile)
    small=load_style_font(font_path,max(12,round(timeline.height*.024)),profile,role='detail')
    line=max(1,round(timeline.height*.002))
    palette=profile['presets']['palette']
    def color(name,alpha=1.):
        r,g,b,a=palette[name]
        return r,g,b,round(a*opacity*alpha)
    def text(position,value,selected=font,fill='ink',shadow=False):
        if shadow:
            draw_style_text(draw,(position[0]+line,position[1]+line),value,selected,color('ink'),line,color('ink'))
        draw_style_text(draw,position,value,selected,color(fill))
    if skin(profile)=='editorial':
        # Paper, ink and seal geometry: no slanted yellow panels or comic strokes.
        draw.rounded_rectangle((x,y,right,bottom),radius=max(2,pad//3),fill=color('paper'),outline=color('ink'),width=line)
        if kind=='burst':
            cx,cy=(x+right)/2,y+h*.43;radius=min(w*.45,h*.40)
            draw.ellipse((cx-radius,cy-radius,cx+radius,cy+radius),outline=color('accent'),width=line*2)
            draw.ellipse((cx-radius+line*4,cy-radius+line*4,cx+radius-line*4,cy+radius-line*4),outline=color('accent'),width=line)
        elif kind=='program_title':
            draw.rectangle((x+pad,y+pad*.65,x+pad*2,y+pad*1.5),fill=color('accent'))
            draw.line((x+pad*2.5,y+pad*1.1,x+w*ease-pad,y+pad*1.1),fill=color('ink'),width=line)
        elif kind in {'headline','impact'}:
            draw.line((x+pad,bottom-pad*.6,x+(w-pad)*ease,bottom-pad*.6),fill=color('accent'),width=line*3)
        else:
            draw.rectangle((x,y,x+line*3,bottom),fill=color('accent'))
        ty=y+h*(.31 if kind=='program_title' else .23 if hero else .22)
        text((x+pad*1.5,ty),primary,fill='accent' if kind in {'impact','burst'} else 'ink')
        if detail:text((x+pad*1.5,bottom-pad*1.9),detail,small)
    else:
        # Quiet lower-third grammar, hairline brackets and serif type. The small
        # translucent backing preserves contrast on bright source footage.
        draw.rectangle((x,y,right,bottom),fill=color('ink',.30))
        length=min(pad*2,w*.16)*ease
        if kind=='chapter':
            draw.line((x,y+pad*.5,x+pad*.8,bottom-pad*.5),fill=color('accent'),width=line*2)
        elif kind in {'program_title','burst'}:
            for cx,cy,sx,sy in ((x,y,1,1),(right,bottom,-1,-1)):
                draw.line((cx,cy,cx+sx*length,cy),fill=color('accent'),width=line)
                draw.line((cx,cy,cx,cy+sy*length),fill=color('accent'),width=line)
        else:
            draw.line((x+pad,bottom-pad*.6,x+(w-pad)*ease,bottom-pad*.6),fill=color('accent'),width=line)
        ty=y+h*(.30 if kind=='program_title' else .23 if hero else .22)
        text((x+pad*1.5,ty),primary,fill='paper',shadow=True)
        if detail:text((x+pad*1.5,bottom-pad*1.9),detail,small,fill='paper',shadow=True)
    return bounds
