"""One-frame editing canvas using real source pixels and native text renderers.

This is a static editing view, not approval of transitions, motion or sound.
"""
import base64
from dataclasses import replace
from fractions import Fraction
from io import BytesIO
import math
from pathlib import Path
import tempfile
import threading
from PIL import Image, ImageDraw, ImageFont
from clipen.domain import TrackKind, EffectRequest
from clipen.media import MediaPreviewRepository
from clipen.projects import ProjectError
from clipen.finishing.audio import run_process
from clipen.rendering.renderer import FfmpegPreviewRenderer
from clipen.rendering.production_effects import grade_filters
from clipen.rendering.titles import TitleCardRenderer, TitleRenderError
from .production_media import load_text_styles


def frame(repository, project_id, run_id, expected_revision_id, frame_number, text_override=None):
    """One still of the current version; text_override ({element_id, text}) previews unsaved wording."""
    revision=repository.current_revision(project_id,run_id);timeline=revision.timeline
    if revision.revision_id!=expected_revision_id:raise ProjectError('revision_conflict','影片已更新，正在重新讀取編輯畫面。',status_code=409)
    if isinstance(frame_number,bool) or not isinstance(frame_number,int) or not 0<=frame_number<timeline.duration_frames:
        raise ProjectError('frame_invalid','預覽時間不在影片內。',status_code=422)
    items=[i for t in timeline.tracks if t.kind is TrackKind.TITLE for i in t.items]
    if text_override is not None:
        if not isinstance(text_override,dict) or not isinstance(text_override.get('element_id'),str) or not isinstance(text_override.get('text'),str) or len(text_override['text'])>200:
            raise ProjectError('frame_invalid','預覽文字格式不正確。',status_code=422)
        if not text_override['text'].strip():raise ProjectError('component_text_empty','元件的主要文字不能空白。',status_code=422)
        items=[replace(i,text=text_override['text']) if i.item_id==text_override['element_id'] else i for i in items]
    styles=load_text_styles(repository.projects,project_id)
    clip=next((i for t in timeline.tracks if t.kind is TrackKind.VIDEO for i in t.items if i.start_frame<=frame_number<i.start_frame+i.duration_frames),None)
    if clip is None:raise ProjectError('frame_invalid','這個時間沒有畫面片段。',status_code=422)
    fps=Fraction(timeline.frame_rate.numerator,timeline.frame_rate.denominator)
    relative=frame_number-clip.start_frame
    span=clip.source.span;unit=Fraction(span.time_base.numerator,span.time_base.denominator)
    duration=span.duration_pts*unit
    elapsed=Fraction(relative,1)/fps
    speed=FfmpegPreviewRenderer._speed_profile(clip)
    if speed:
        outside,center=speed;first=float(duration)*.4/outside;middle=float(duration)*.2/center;t=float(elapsed)
        elapsed=Fraction(str(t*outside if t<first else float(duration)*.4+(t-first)*center if t<first+middle else float(duration)*.6+(t-first-middle)*outside))
    seconds=int(span.start_pts)*unit+min(max(Fraction(0),elapsed),max(Fraction(0),duration-Fraction(1,1)/fps))
    media=MediaPreviewRepository(repository.projects);source=media.open(project_id,span.version_id)
    try:
        if span.stream_index not in source.video_stream_indices:raise ProjectError('frame_invalid','來源沒有指定的影像串流。',status_code=422)
        effects=[]
        for effect in clip.effects:
            if effect.effect_id=='video.slow_push':
                p=effect.parameters;scale=p.get('scale_from',1)+(p.get('scale_to',1.08)-p.get('scale_from',1))*relative/max(1,clip.duration_frames-1)
                effects.append(EffectRequest('video.punch_in',{'scale':scale,'center_x':.5,'center_y':.5}))
            else:effects.append(effect)
        still_clip=replace(clip,effects=tuple(effects))
        filters=f'scale={timeline.width}:{timeline.height}:force_original_aspect_ratio=decrease,pad={timeline.width}:{timeline.height}:(ow-iw)/2:(oh-ih)/2'
        filters+=FfmpegPreviewRenderer._video_effect_filters(still_clip,timeline)+grade_filters(clip)
        with tempfile.TemporaryDirectory(prefix='clipen-edit-frame-') as temporary:
            output=Path(temporary)/'source.png'
            run_process(['ffmpeg','-v','error','-nostdin','-seek_timestamp','1','-ss',f'{float(seconds):.9f}','-i',f'/dev/fd/{source.descriptor}','-map',f'0:{span.stream_index}','-frames:v','1','-vf',filters,'-threads','1',str(output)],threading.Event(),pass_fds=(source.descriptor,),timeout=30)
            with Image.open(output) as pixels:image=pixels.convert('RGBA')
    finally:source.close()
    # Match the native mosaic geometry for this exact instant.
    for effect in clip.effects:
        if effect.effect_id!='video.mosaic':continue
        for region in effect.parameters['regions']:
            if not region['start_frame']<=relative<region['start_frame']+region['duration_frames']:continue
            width=max(2,min(timeline.width,round(timeline.width*region['width'])//2*2));height=max(2,min(timeline.height,round(timeline.height*region['height'])//2*2))
            x=min(timeline.width-width,round(timeline.width*region['x'])//2*2);y=min(timeline.height-height,round(timeline.height*region['y'])//2*2)
            pixels=image.crop((x,y,x+width,y+height));pixels=pixels.resize((max(1,math.ceil(width/region['block_size'])),max(1,math.ceil(height/region['block_size']))),Image.Resampling.BOX).resize((width,height),Image.Resampling.NEAREST);image.paste(pixels,(x,y))
    font=TitleCardRenderer._font_path()
    if font is None:raise ProjectError('preview_title_font_missing','找不到預覽字型。',status_code=422)
    overlay=Image.new('RGBA',image.size,(0,0,0,0));draw=ImageDraw.Draw(overlay);renderer=TitleCardRenderer(styles)
    try:
        for item in items:
            if item.start_frame<=frame_number<item.start_frame+item.duration_frames:
                renderer._draw_card(draw,ImageFont,font,timeline,item,frame_number-item.start_frame)
    except TitleRenderError as error:raise ProjectError(error.code,'文字超出版面或無法顯示，請縮短文字或調整位置。',status_code=422) from error
    image=Image.alpha_composite(image,overlay).convert('RGB');image.thumbnail((960,960))
    encoded=BytesIO();image.save(encoded,format='JPEG',quality=88)
    return {'revision_id':revision.revision_id,'frame':frame_number,'source_item_id':clip.item_id,'image_data_url':'data:image/jpeg;base64,'+base64.b64encode(encoded.getvalue()).decode(),'scope':'editing_still'}
