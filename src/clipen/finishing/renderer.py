"""Deterministic local Vlog compositor; every filter is owned by Clipen."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile

from clipen.domain import EditTimeline, EffectRequest
from clipen.rendering.renderer import FfmpegPreviewRenderer
from clipen.rendering.titles import TitleCardRenderer
from clipen.projects import ProjectError

from .audio import open_asset, run_process
from .models import compile_context, validate_plan


class FinishingRenderer:
    def __init__(self, media):
        self.media = media

    def render(self, project_id, context, plan, output, cancel, progress):
        plan = validate_plan(plan, context)
        context = compile_context(context, plan)
        timeline = EditTimeline.from_payload(context['timeline'])
        fps = timeline.frame_rate.numerator/timeline.frame_rate.denominator
        seconds = timeline.duration_frames/fps
        clips = [i for t in timeline.tracks if t.kind.value=='video' for i in t.items]
        with tempfile.TemporaryDirectory(prefix='.finishing-', dir=output.parent) as tmp:
            directory = Path(tmp)
            motions = {m['clip_id']:m for m in plan['motion']}
            transitions = {t['clip_id']:t for t in plan['transitions']}
            modes = {a['clip_id']:a['mode'] for a in plan['source_audio']}
            for index, clip in enumerate(clips):
                source = self.media.open(project_id, clip.source.span.version_id)
                try:
                    duration = clip.duration_frames/fps
                    start = float(clip.source.span.start_pts)*clip.source.span.time_base.numerator/clip.source.span.time_base.denominator
                    command = ['ffmpeg','-v','error','-nostdin','-y','-filter_complex_threads','2','-seek_timestamp','1','-ss',f'{start:.9f}','-t',f'{duration:.9f}','-i',f'/dev/fd/{source.descriptor}']
                    if not source.video_stream_indices:
                        raise ProjectError('finishing_video_missing','毛片缺少影像串流。', status_code=409)
                    video_index = clip.source.span.stream_index
                    if video_index not in source.video_stream_indices:
                        raise ProjectError('finishing_video_missing','毛片影像串流與方向樣片不一致。', status_code=409)
                    chain = (f'[{0}:{video_index}]setpts=PTS-STARTPTS,scale={timeline.width}:{timeline.height}:force_original_aspect_ratio=decrease,'
                             f'pad={timeline.width}:{timeline.height}:(ow-iw)/2:(oh-ih)/2,fps={timeline.frame_rate.numerator}/{timeline.frame_rate.denominator}')
                    # The direction proof's demo effects do not stack with the selected finish.
                    effect = motions.get(clip.item_id)
                    effects = ()
                    if effect:
                        kind = effect['kind']
                        params = {'scale':effect['amount'],'center_x':effect['x'],'center_y':effect['y']} if kind=='punch_in' else {'scale_from':1.,'scale_to':effect['amount']} if kind=='slow_push' else {'x':effect['x'],'y':effect['y']}
                        effects = (EffectRequest('video.'+kind,params),)
                    modified = replace(clip,effects=effects)
                    filters = []
                    if effect and effect['kind']=='speed_ramp':
                        modified = replace(clip,effects=(EffectRequest('video.speed_ramp',{'rate':effect['amount']}),))
                        filters.append(chain+'[vbase]')
                        label = FfmpegPreviewRenderer._retime_video(filters,'vbase',modified,duration,index)
                        chain = f'[{label}]null'
                    else:
                        chain += FfmpegPreviewRenderer._video_effect_filters(modified,timeline)
                    grade = plan['grade']
                    chain += f",eq=contrast={grade['contrast']}:saturation={grade['saturation']}:brightness={grade['brightness']}"
                    incoming = transitions.get(clip.item_id)
                    outgoing = transitions.get(clips[index+1].item_id) if index+1 < len(clips) else None
                    if incoming:
                        chain += f",fade=t=in:st=0:d={incoming['duration_frames']/fps/2:.6f}:color={'white' if incoming['kind']=='dip_white' else 'black'}"
                    if outgoing:
                        fade = outgoing['duration_frames']/fps/2
                        chain += f",fade=t=out:st={max(0,duration-fade):.6f}:d={fade:.6f}:color={'white' if outgoing['kind']=='dip_white' else 'black'}"
                    filters.append(chain+',setsar=1,format=yuv420p[v]')
                    requested_audio = context.get('audio_streams',{}).get(clip.item_id,source.audio_stream_indices[0] if source.audio_stream_indices else None)
                    if requested_audio is not None and requested_audio not in source.audio_stream_indices:
                        raise ProjectError('finishing_audio_missing','口語所用的音訊串流已不存在。',status_code=409)
                    if requested_audio is not None and modes[clip.item_id]!='mute':
                        audio = f'[0:{requested_audio}]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo'
                        if modes[clip.item_id]=='dialogue': audio += ',highpass=f=85,lowpass=f=11000,acompressor=threshold=0.125:ratio=2'
                        if effect and effect['kind']=='speed_ramp':
                            filters.append(audio+'[abase]')
                            label = FfmpegPreviewRenderer._retime_audio(filters,'abase',modified,duration,index)
                            audio = f'[{label}]anull'
                        filters.append(audio+f',apad,atrim=duration={duration:.9f}[a]')
                    else:
                        filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={duration:.9f}[a]')
                    command += ['-filter_complex',';'.join(filters),'-map','[v]','-map','[a]','-t',f'{duration:.9f}',
                                '-c:v','libx264','-preset','veryfast','-crf','20','-threads','2','-c:a','pcm_s16le',str(directory/f'clip-{index}.mkv')]
                    run_process(command,cancel,pass_fds=(source.descriptor,),timeout=120)
                finally:
                    source.close()
                progress(.55*(index+1)/len(clips))
            concat = directory/'clips.ffconcat'
            concat.write_text('ffconcat version 1.0\n'+''.join(f"file 'clip-{i}.mkv'\n" for i in range(len(clips))))
            overlays = render_overlays(context,plan,timeline,directory,cancel)
            progress(.62)
            opened = []
            try:
                music_fd, music = open_asset(context,plan['music']['asset_id'])
                opened.append(music_fd)
                command = ['ffmpeg','-v','error','-nostdin','-y','-filter_complex_threads','2','-f','concat','-safe','0','-i',str(concat),
                           '-f','concat','-safe','0','-i',str(overlays),'-ss',str(plan['music']['offset_seconds']),'-t',str(seconds),'-i',f'/dev/fd/{music_fd}']
                music_gain = min(18., max(-48., -24.-music['rms_db']+plan['music']['gain_db']))
                envelope = music_envelope(plan['music']['regions'],fps)
                filters = [
                    '[0:v][1:v]overlay=eof_action=repeat:format=auto,format=yuv420p[v]',
                    '[0:a]asplit=2[original][sidechain]',
                    f"[2:{music['stream_index']}]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,volume={music_gain:.3f}dB{envelope},afade=t=in:d=0.5,afade=t=out:st={seconds-1:.6f}:d=1[bgm]",
                    '[bgm][sidechain]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=350[ducked]',
                ]
                labels = ['[original]','[ducked]']
                for i, effect in enumerate(plan['sound_effects']):
                    fd, asset = open_asset(context,effect['asset_id']); opened.append(fd)
                    command += ['-t',str(effect['duration_frames']/fps),'-i',f'/dev/fd/{fd}']
                    delay = round(effect['start_frame']/fps*1000)
                    gain = min(18.,max(-48.,-20.-asset['rms_db']+effect['gain_db']))
                    filters.append(f"[{i+3}:{asset['stream_index']}]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,volume={gain:.3f}dB,afade=t=out:st={max(0,effect['duration_frames']/fps-.08):.6f}:d=0.08,adelay={delay}|{delay}[sfx{i}]")
                    labels.append(f'[sfx{i}]')
                filters.append(''.join(labels)+f'amix=inputs={len(labels)}:duration=first:normalize=0,loudnorm=I=-16:LRA=9:TP=-1.5,aresample=48000,alimiter=limit=0.891:level=0[a]')
                command += ['-filter_complex',';'.join(filters),'-map','[v]','-map','[a]','-t',str(seconds),'-r',f'{timeline.frame_rate.numerator}/{timeline.frame_rate.denominator}',
                            '-c:v','libx264','-preset','veryfast','-crf','20','-threads','4','-c:a','aac','-b:a','192k','-movflags','+faststart',str(output)]
                run_process(command,cancel,pass_fds=tuple(opened),timeout=300)
            finally:
                for fd in opened: os.close(fd)
            progress(.92)
            verify_output(output,timeline,cancel)
            progress(1.)


def music_envelope(regions,fps):
    """Narrative music levels with short interpolation instead of abrupt gain steps."""
    filters = ''
    previous = regions[0]['gain_db']
    for region in regions:
        start = region['start_frame']/fps
        end = (region['start_frame']+region['duration_frames'])/fps
        target = region['gain_db']
        ramp = min(.15,region['duration_frames']/fps)
        expression = f'if(gte(t,{start:.6f})*lt(t,{end:.6f}),pow(10,({previous}+({target-previous})*min(1,(t-{start:.6f})/{ramp:.6f}))/20),1)'
        filters += f",volume='{expression}':eval=frame"
        previous = target
    return filters


def render_overlays(context, plan, timeline, directory, cancel):
    """Pillow CJK overlays work even on FFmpeg builds without libass/drawtext."""
    from PIL import Image, ImageDraw, ImageFont
    font_path = TitleCardRenderer._font_path()
    if font_path is None:
        raise ProjectError('preview_title_font_missing','缺少可顯示繁體中文的字型。',status_code=409)
    fps = timeline.frame_rate.numerator/timeline.frame_rate.denominator
    width,height = timeline.width,timeline.height
    cues = {c['cue_id']:c for c in context['cues']}
    captions = [{**cues[c['cue_id']],**c} for c in plan['captions']]
    boundaries = {0,timeline.duration_frames}
    for event in captions+plan['cards']:
        start,end = event['start_frame'],event['start_frame']+event['duration_frames']
        boundaries.update((start,end))
        for step in range(1,min(5,event['duration_frames'])): boundaries.add(start+step)
    boundaries = sorted(boundaries)
    lines = ['ffconcat version 1.0']
    for index,(frame,end) in enumerate(zip(boundaries,boundaries[1:])):
        if cancel.is_set(): raise ProjectError('user_cancelled','已取消製作。',status_code=409)
        image = Image.new('RGBA',(width,height),(0,0,0,0))
        draw = ImageDraw.Draw(image)
        active_cards = sorted((c for c in plan['cards'] if c['start_frame'] <= frame < c['start_frame']+c['duration_frames']),
                              key=lambda c:(c['start_frame'], c['purpose'] != 'chapter'))
        # Fit all active cards in the upper half, keeping the caption area free.
        # Preserve their text and timing instead of asking AI to fix local layout.
        size = max(1,round(height*.048))
        while True:
            font = ImageFont.truetype(str(font_path),size)
            rows = [TitleCardRenderer._wrap(draw,('▍' if c['purpose']=='chapter' else '• ')+c['text'],font,round(width*.82)) for c in active_cards]
            line_height = max(1,round(size*1.6))
            gap = max(1,round(size*.4))
            if sum(len(r)*line_height+gap for r in rows) <= height*.45 or size == 1: break
            size -= 1
        x,y=round(width*.07),round(height*.09)
        for wrapped in rows:
            for row in wrapped:
                box = draw.textbbox((x,y),row,font=font)
                draw.rounded_rectangle((x-size*.3,y-size*.15,box[2]+size*.4,box[3]+size*.25),radius=size*.2,fill=(22,45,37,230))
                draw.text((x,y),row,font=font,fill=(255,255,245,255)); y+=line_height
            y+=gap
        for cap in captions:
            if cap['start_frame'] <= frame < cap['start_frame']+cap['duration_frames']:
                layer = Image.new('RGBA',(width,height),(0,0,0,0)); pen = ImageDraw.Draw(layer)
                size = round(height*.043)
                font = ImageFont.truetype(str(font_path),size)
                wrapped = TitleCardRenderer._wrap(pen,cap['text'],font,round(width*.82))
                if len(wrapped)>2:
                    raise ProjectError('finishing_caption_overflow','字幕超過兩行，請縮短文案。',status_code=422)
                y = round(height*.88)-round(size*1.35)*len(wrapped)
                cursor = 0
                emphasis_start = cap['text'].find(cap['emphasis']) if cap['emphasis'] else -1
                for row in wrapped:
                    text_width = pen.textlength(row,font=font)
                    x=(width-text_width)/2
                    pen.rounded_rectangle((x-size*.4,y,x+text_width+size*.4,y+size*1.38),radius=size*.2,fill=(15,20,18,220))
                    for character in row:
                        highlighted = emphasis_start <= cursor < emphasis_start+len(cap['emphasis']) if emphasis_start>=0 else False
                        pen.text((x,y),character,font=font,fill=(255,216,85,255) if highlighted else (255,255,250,255),stroke_width=1,stroke_fill=(10,15,12,255))
                        x+=pen.textlength(character,font=font); cursor+=1
                    y+=round(size*1.4)
                scale = .96+.04*min(1,(frame-cap['start_frame'])/4)
                if scale<1:
                    small=layer.resize((round(width*scale),round(height*scale)),Image.Resampling.LANCZOS)
                    image.alpha_composite(small,(round((width-small.width)/2),height-small.height))
                else: image.alpha_composite(layer)
        name=f'overlay-{index}.png'; image.save(directory/name)
        lines.extend((f"file '{name}'",f'option framerate {timeline.frame_rate.numerator}/{timeline.frame_rate.denominator}',f'duration {(end-frame)/fps:.9f}'))
    lines.extend((f"file 'overlay-{len(boundaries)-2}.png'",f'option framerate {timeline.frame_rate.numerator}/{timeline.frame_rate.denominator}'))
    path=directory/'overlays.ffconcat';path.write_text('\n'.join(lines)+'\n')
    return path


def verify_output(path,timeline,cancel):
    raw=run_process(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],cancel,timeout=20)
    value=json.loads(raw)
    videos=[s for s in value['streams'] if s['codec_type']=='video']
    audios=[s for s in value['streams'] if s['codec_type']=='audio']
    fps=timeline.frame_rate.numerator/timeline.frame_rate.denominator
    duration=timeline.duration_frames/fps
    if len(videos)!=1 or len(audios)!=1 or (videos[0]['width'],videos[0]['height'])!=(timeline.width,timeline.height):
        raise ProjectError('finishing_verification_failed','成片的影像或聲音規格不完整。',status_code=409)
    from fractions import Fraction
    if videos[0]['codec_name']!='h264' or audios[0]['codec_name']!='aac' or Fraction(videos[0]['avg_frame_rate']) != Fraction(timeline.frame_rate.numerator,timeline.frame_rate.denominator) or abs(int(videos[0].get('nb_frames',0))-timeline.duration_frames)>1:
        raise ProjectError('finishing_verification_failed',f'成片的編碼、格率或格數與時間軸不一致：{videos[0]["codec_name"]}/{audios[0]["codec_name"]}，格率 {videos[0]["avg_frame_rate"]}，格數 {videos[0].get("nb_frames",0)}（預期 {timeline.duration_frames}）。',status_code=409)
    if any(abs(float(s.get('duration',0))-duration)>max(.1,2/fps) for s in (videos[0],audios[0])):
        raise ProjectError('finishing_verification_failed','成片的影音長度與時間軸不一致。',status_code=409)
    run_process(['ffmpeg','-v','error','-xerror','-nostdin','-i',str(path),'-f','null','-'],cancel,timeout=90)
