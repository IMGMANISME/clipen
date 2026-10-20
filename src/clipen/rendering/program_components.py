"""Deterministic in-picture graphics for versioned programme skill packages."""
import math

from PIL import ImageFont
from .titles import TitleRenderError


def animation_boundaries(item, profile):
    motion = profile["presets"]["motion"]
    entry = min(motion["entry_frames"], item.duration_frames)
    exit_frames = min(motion["exit_frames"], item.duration_frames)
    return {item.start_frame+n for n in
            (*range(entry+1), *range(item.duration_frames-exit_frames, item.duration_frames+1))}


def component_bounds(draw, font_path, timeline, item, profile):
    typography=profile['presets'].get('typography',{})
    scale = typography.get('reaction',.064) if profile["component_id"] == "reaction" else typography.get('component_primary',.038)
    font = ImageFont.truetype(str(font_path), max(14, round(timeline.height*scale)))
    primary,_,detail=item.text.partition('\n')
    detail_font=ImageFont.truetype(str(font_path),max(12,round(timeline.height*typography.get('component_detail',.024))))
    width = max(draw.textlength(primary, font=font), draw.textlength(detail,font=detail_font) if detail else 0)
    pad = max(8, round(timeline.height*.016))
    x, y = round(timeline.width*profile["x"]), round(timeline.height*profile["y"])
    box_width=max(math.ceil(width)+pad*2,round(timeline.width*.15))
    box_height=round(timeline.height*scale*1.65)+pad
    if profile['component_id']!='reaction':box_height+=round(timeline.height*.025)
    if detail:box_height+=round(timeline.height*.038)
    if box_width > timeline.width*.58 or x+box_width > timeline.width*.95 or y+box_height > timeline.height*.77:
        raise TitleRenderError("program_component_overflow")
    return (x, y, x+box_width, y+box_height), font, pad


def draw_program_component(draw, font_path, timeline, item, profile, relative_frame=None):
    bounds, font, pad = component_bounds(draw, font_path, timeline, item, profile)
    motion = profile["presets"]["motion"]
    relative = motion["entry_frames"] if relative_frame is None else relative_frame
    entering = min(1., max(0., (relative+1)/motion["entry_frames"]))
    leaving = min(1., (item.duration_frames-relative)/motion["exit_frames"])
    opacity = min(entering, leaving)
    eased = 1-(1-entering)**3
    dx = round((1-eased)*timeline.width*motion["entry_distance_ratio"])
    x, y, right, bottom = bounds
    x -= dx
    right -= dx
    palette = profile["presets"]["palette"]

    def color(name):
        r, g, b, a = palette[name]
        return (r, g, b, round(a*opacity))

    kind = profile["component_id"]
    primary,_,detail=item.text.partition('\n')
    stroke = max(2, round(timeline.height*.004))
    if kind == "reaction":
        draw.text((x+pad, y+pad//2), primary, font=font, fill=color("accent"),
                  stroke_width=stroke*2, stroke_fill=color("ink"))
        if detail:
            detail_font=ImageFont.truetype(str(font_path),max(12,round(timeline.height*.024)))
            draw.text((x+pad,y+pad+round(timeline.height*.075)),detail,font=detail_font,fill=color('paper'),
                      stroke_width=stroke,stroke_fill=color('ink'))
        # A short pair of ticks gives an actual reaction a shared programme
        # accent without turning every dialogue line into a reaction graphic.
        draw.line((x-pad//2,y+pad,x-pad,y-pad//2),fill=color('accent'),width=stroke)
        draw.line((right+pad//2,y+pad,right+pad,y-pad//2),fill=color('accent'),width=stroke)
    else:
        # Narrow editorial labels sit inside the footage, with no full-screen
        # background. A shared accent edge and typography form programme identity.
        draw.rectangle((x+3, y+4, right+3, bottom+4), fill=(0, 0, 0, round(75*opacity)))
        draw.rectangle((x, y, right, bottom), fill=color("ink"))
        draw.rectangle((x, y, x+stroke*2, bottom), fill=color("secondary" if kind == "person" else "accent"))
        typography=profile['presets'].get('typography',{})
        kicker_font=ImageFont.truetype(str(font_path),max(10,round(timeline.height*typography.get('component_kicker',.017))))
        kicker=profile.get('eyebrow') or {'location':'現場','person':'人物','fact':'資訊','pointer':'看這裡','circle':'細節'}[kind]
        draw.text((x+pad,y+pad//2),kicker,font=kicker_font,fill=color('secondary' if kind=='person' else 'accent'))
        primary_y=y+pad//2+round(timeline.height*.027)
        draw.text((x+pad,primary_y),primary,font=font,fill=color("paper"))
        if detail:
            detail_font=ImageFont.truetype(str(font_path),max(12,round(timeline.height*typography.get('component_detail',.024))))
            draw.text((x+pad,primary_y+round(timeline.height*.052)),detail,font=detail_font,fill=color('paper'))
        draw.line((x+pad,bottom-pad//2,right-pad,bottom-pad//2),fill=color("accent"),width=max(1,stroke//2))
    if kind not in {"pointer", "circle"}:
        return bounds
    target = profile["target"]
    tx, ty = target["x"]*timeline.width, target["y"]*timeline.height
    radius = target["radius"]*min(timeline.width, timeline.height)
    if kind == "circle":
        rect = (tx-radius, ty-radius, tx+radius, ty+radius)
        draw.arc(rect, -90, -90+max(2, round(360*eased)), fill=color("accent"), width=stroke*2)
    else:
        origin = (right, (y+bottom)/2)
        angle = math.atan2(ty-origin[1], tx-origin[0])
        tip = (origin[0]+(tx-origin[0])*eased, origin[1]+(ty-origin[1])*eased)
        draw.line((origin, tip), fill=color("ink"), width=stroke*3)
        draw.line((origin, tip), fill=color("accent"), width=stroke)
        size = max(9, timeline.height*.025)
        points = [tip, (tip[0]-size*math.cos(angle-.45), tip[1]-size*math.sin(angle-.45)),
                  (tip[0]-size*math.cos(angle+.45), tip[1]-size*math.sin(angle+.45))]
        draw.polygon(points, fill=color("accent"))
    return bounds
