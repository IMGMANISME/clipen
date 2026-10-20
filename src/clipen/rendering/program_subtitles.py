"""Outlined programme captions with in-sentence emphasis and bounded animation.

Rendered as native RGBA events, so the pipeline does not depend on libass being
installed in the user's FFmpeg build. No text box, chapter card or logo is drawn.
"""
from PIL import ImageFont


def animation_boundaries(item):
    return {item.start_frame+n for n in (0,2,4,7,item.duration_frames-2,item.duration_frames) if 0<=n<=item.duration_frames}


def draw_program_subtitle(draw,font_path,timeline,item,profile,relative_frame=None):
    relative=7 if relative_frame is None or profile.get('motion')=='none' else relative_frame
    scale=.88 if relative<2 else 1.06 if relative<4 else .98 if relative<7 else 1.
    size=max(16,round(timeline.height*profile.get('font_size_ratio',.052)*scale))
    index=0
    # Hiragino's W6 face is heavier than W3. Other families use their configured
    # face and a small foreground stroke rather than assuming a TTC index.
    try:
        candidate=ImageFont.truetype(str(font_path),size,index=2)
        if 'W6' in candidate.getname()[1]:index=2
    except OSError:pass
    font=ImageFont.truetype(str(font_path),size,index=index)
    layout_font=ImageFont.truetype(str(font_path),max(16,round(timeline.height*profile.get('font_size_ratio',.052)*1.06)),index=index)
    text=item.text.replace('\n',' ');maximum=timeline.width*.86
    lines=[];line=''
    for character in text:
        if line and draw.textlength(line+character,font=layout_font)>maximum:
            lines.append(line);line=''
        line+=character
    if line:lines.append(line)
    if len(lines)>2:
        from .titles import TitleRenderError
        raise TitleRenderError('program_subtitle_overflow')
    highlighted=set()
    for word in profile.get('emphasis',[]):
        start=0
        while (start:=text.find(word,start))>=0:
            highlighted.update(range(start,start+len(word)));start+=len(word)
    opacity=255 if item.duration_frames-relative>2 else 180
    stroke=max(2,round(size*.095));line_height=round(size*1.35)
    baseline=round(timeline.height*.90)-(len(lines)-1)*line_height
    offset=0
    for line in lines:
        x=(timeline.width-draw.textlength(line,font=font))/2
        for character in line:
            color=(255,217,55,opacity) if offset in highlighted else (255,255,255,opacity)
            draw.text((x+2,baseline+3),character,font=font,anchor='ls',fill=(0,0,0,110),stroke_width=stroke+1,stroke_fill=(0,0,0,110))
            draw.text((x,baseline),character,font=font,anchor='ls',fill=color,stroke_width=stroke,stroke_fill=(10,10,14,opacity))
            x+=draw.textlength(character,font=font);offset+=1
        baseline+=line_height
