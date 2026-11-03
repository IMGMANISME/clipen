"""Original broadcast graphics: bounded layouts and frame-driven entrance motion."""
import math

from .titles import TitleRenderError
from .program_styles import skin, style_font, load_style_font, styled_text_length, draw_styled_graphic, draw_style_text, type_outline


# Each family owns its composition, rather than stretching the small label style.
FAMILIES = {
    'program_title': (.125, .80, .29, 24, 8),
    'headline': (.135, .82, .24, 20, 7),
    'impact': (.145, .74, .27, 16, 6),
    'chapter': (.075, .78, .18, 22, 8),
    'burst': (.108, .62, .27, 20, 6),
}


def motion_frames(kind, profile=None):
    appearance=(profile or {}).get('presets',{}).get('appearance',{})
    return (appearance.get('hero_entry_frames',FAMILIES[kind][3]),
            appearance.get('hero_exit_frames',FAMILIES[kind][4]))


def layout(draw, font_path, timeline, item, profile):
    if skin(profile) == 'studio':
        from .program_studio import layout as studio_layout
        return studio_layout(draw, font_path, timeline, item, profile)
    scale, max_width, height, _, _ = FAMILIES[profile['component_id']]
    font_path=style_font(font_path,profile)
    scale*=profile['presets'].get('appearance',{}).get('hero_scale',1.)
    w, h = timeline.width, timeline.height
    x, y = round(w*profile['x']), round(h*profile['y'])
    pad = max(8, round(h*.025))
    primary, _, detail = item.text.partition('\n')
    limit = min(w*max_width, w*.945-x)
    size = max(14, round(h*scale))
    minimum = max(14, round(h*scale*.70))
    while True:
        font = load_style_font(font_path, size,profile)
        # Room for outline, shadow, skew and framing, including transient motion.
        width = styled_text_length(primary,font)+pad*4
        if width <= limit or size <= minimum:
            break
        size -= 1
    small = load_style_font(font_path,max(12,round(h*.027)),profile,role='detail')
    width = max(width, styled_text_length(detail,small)+pad*4 if detail else 0, w*.30)
    bottom = y+round(h*height)
    if width > limit or bottom > h*.77:
        raise TitleRenderError('program_component_overflow')
    return (x, y, x+math.ceil(width), bottom), font, pad


def draw_motion_graphic(draw, font_path, timeline, item, profile, relative_frame=None):
    bounds, font, pad = layout(draw, font_path, timeline, item, profile)
    if skin(profile)!='variety':
        return draw_styled_graphic(draw,font_path,timeline,item,profile,bounds,font,pad,relative_frame)
    kind = profile['component_id']
    entry, exit_frames = motion_frames(kind,profile)
    relative = entry if relative_frame is None else relative_frame
    progress = min(1., max(0., (relative+1)/entry))
    ease = 1-(1-progress)**3
    opacity = min(1., max(0., (item.duration_frames-relative)/exit_frames))
    palette = profile['presets']['palette']

    def color(name, alpha=1.):
        r, g, b, a = palette[name]
        return r, g, b, round(a*opacity*alpha)

    x, y, right, bottom = bounds
    width, height = right-x, bottom-y
    stroke = max(2, round(timeline.height*.004))
    primary, _, detail = item.text.partition('\n')
    small = load_style_font(font_path, max(12, round(timeline.height*.027)),profile,role='detail')
    kicker = load_style_font(font_path, max(10, round(timeline.height*.020)),profile,role='detail')

    def text(position, value, *, fill='paper', selected_font=font, outline=True):
        draw_style_text(draw,position,value,selected_font,color(fill),type_outline(profile,selected_font,stroke*2) if outline else 0,color('ink'))
        # Embolden the glyph itself; CJK collections may expose a light default face.
        if not profile['presets'].get('font_roles'):
            draw_style_text(draw,position,value,selected_font,color(fill),max(1,stroke//2) if selected_font is font else 0,color(fill))

    if kind == 'program_title':
        # Two framing strokes reveal first; the title rises into the open frame.
        reveal = width*ease
        draw.rectangle((x, y, x+reveal, y+stroke*2), fill=color('accent'))
        draw.rectangle((right-reveal, bottom-stroke, right, bottom), fill=color('secondary'))
        draw.rectangle((x, y+stroke*2, x+stroke, bottom), fill=color('accent', ease))
        draw.rectangle((x+pad, y+pad*.5, x+pad+max(width*.25, timeline.width*.16)*ease, y+pad*2.0), fill=color('accent'))
        draw.text((x+pad*1.2, y+pad*.74), 'ON LOCATION', font=kicker, fill=color('ink'), anchor='lt')
        tx, ty = x+pad*1.4, y+height*.32+(1-ease)*pad
        text((tx+stroke*2, ty+stroke*2), primary, fill='ink')
        text((tx, ty), primary)
    elif kind == 'headline':
        # Word-sized punches arrive left-to-right over an expanding slanted band.
        band_y = y+height*.10
        draw.polygon([(x,band_y+height*.12),(x+width*ease,band_y),
                      (x+width*ease-pad, y+height*.76),(x,y+height*.76)],fill=color('accent'))
        cursor=x+pad*1.5
        for n, char in enumerate(primary):
            local=min(1.,max(0.,progress*1.8-n/max(1,len(primary))*.8))
            if local:
                text((cursor,y+height*.19+(1-local)**2*pad),char,fill='ink',outline=False)
            cursor+=draw.textlength(char,font=font)
    elif kind == 'impact':
        # A slam contracts from a displaced shadow into solid outlined type.
        hit = min(1., progress*1.8)
        tx, ty = x+pad*2, y+height*.20
        offset=round((1-hit)*pad*1.5)+stroke*2
        text((tx+offset,ty+offset),primary,fill='secondary')
        text((tx,ty),primary,fill='accent')
        for side in (-1,1):
            edge=x+pad*.8 if side<0 else right-pad*.8
            draw.line((edge,y+height*.2,edge+side*pad*.5*ease,y+height*.05),fill=color('accent'),width=stroke*2)
            draw.line((edge,y+height*.48,edge+side*pad*.5*ease,y+height*.56),fill=color('paper'),width=stroke)
    elif kind == 'chapter':
        # A tab and panel wipe establish a new segment while footage stays visible.
        end=x+width*ease
        draw.polygon([(x,y),(end,y),(end-pad,y+height*.78),(x,y+height*.78)],fill=color('ink'))
        draw.rectangle((x,y,x+pad*.65,y+height*.78),fill=color('secondary'))
        draw.line((x+pad,bottom-stroke,x+width*ease,bottom-stroke),fill=color('accent'),width=stroke*2)
        if progress > .28:
            text((x+pad*1.5,y+height*.16),primary)
    else:  # burst: an angular outlined badge with outward growing rays.
        cx,cy=(x+right)/2,y+height*.43
        rx,ry=width*.44*ease,height*.39*ease
        points=[]
        for n in range(20):
            angle=math.tau*n/20
            radius=1. if n%2==0 else .80
            points.append((cx+math.cos(angle)*rx*radius,cy+math.sin(angle)*ry*radius))
        draw.polygon(points,fill=color('ink'),outline=color('accent'),width=stroke*2)
        if progress>.35:
            text((x+pad*2,y+height*.27),primary,fill='accent')
        for angle in (0,math.pi*.20,math.pi*.80,math.pi):
            start=(cx+math.cos(angle)*width*.46,cy+math.sin(angle)*height*.44)
            end=(start[0]+math.cos(angle)*pad*.6*ease,start[1]+math.sin(angle)*pad*.5*ease)
            draw.line((start,end),fill=color('secondary'),width=stroke*2)
    if detail:
        # Secondary line has reserved space; never lies on top of the main title.
        text((x+pad*1.5,bottom-pad*1.9),detail,selected_font=small)
    return bounds
