"""Bounded dialogue-driven envelopes and inspectable stems for skill productions."""
from array import array
from fractions import Fraction
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

from clipen.domain import MediaClip
from clipen.projects import ProjectError


def dialogue_regions(projects, project_id, timeline):
    from .evidence_jobs import local_cues
    from .production_media import transcript_cues
    tools=SimpleNamespace(repository=SimpleNamespace(projects=projects),project_id=project_id,run_id=timeline.production_session_id)
    local,_=local_cues(tools,timeline,include_drafts=True)
    legacy,_=transcript_cues(projects,project_id,timeline,include_drafts=True)
    fps=timeline.frame_rate.numerator/timeline.frame_rate.denominator
    intervals=[]
    for cue in local+legacy:
        intervals.extend((w['start_frame']/fps,w['end_frame']/fps) for w in cue.get('words',[]))
    # Legacy long ASR windows are deliberately not treated as speech activity.
    # Missing activity leaves music at its chosen gain instead of permanently
    # compressing it under noise. Report that limitation in the mix evidence.
    merged=[]
    for start,end in sorted(intervals):
        if merged and start<=merged[-1][1]+.18:
            merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        elif end>start:merged.append((start,end))
    return merged


def duck_expression(regions, *, attenuation_db=7, attack=.12, release=.4):
    if not regions:return '1'
    pieces=[f"min(1,max(0,(t-{start-attack:.6f})/{attack}))*min(1,max(0,({end+release:.6f}-t)/{release}))" for start,end in regions]
    return f"pow(10,-{attenuation_db}*min(1,({'+'.join(pieces)}))/20)"


def measure_stem(path):
    windows=[];total=0;count=0;peak=0
    with wave.open(str(path),'rb') as source:
        if source.getsampwidth()!=2:raise ValueError('stem requires PCM16')
        block=max(1,source.getframerate()//2)
        while chunk:=source.readframes(block):
            samples=array('h');samples.frombytes(chunk)
            if sys.byteorder!='little':samples.byteswap()
            power=sum(v*v for v in samples)
            rms=math.sqrt(power/max(1,len(samples)))/32768
            windows.append(round(20*math.log10(rms),2) if rms else None)
            total+=power;count+=len(samples);peak=max(peak,max((abs(v) for v in samples),default=0))
    rms=math.sqrt(total/max(1,count))/32768
    return {'rms_dbfs':round(20*math.log10(rms),2) if rms else None,
            'peak_dbfs':round(20*math.log10(peak/32768),2) if peak else None,
            'window_seconds':.5,'window_rms_dbfs':windows}


def mix_program_audio(media, project_id, timeline, base, output, cancel):
    from clipen.finishing.audio import open_asset, run_process
    from .production_media import AgentAudioStore, atomic_json
    fps=timeline.frame_rate.numerator/timeline.frame_rate.denominator
    seconds=timeline.duration_frames/fps
    layers=[(i,l) for t in timeline.tracks for i in t.items if isinstance(i,MediaClip)
            for e in i.effects if e.effect_id=='audio.layers' for l in e.parameters['layers']]
    if len(layers)>24:raise ProjectError('agent_audio_limit','單次最多 24 個混音片段。',status_code=422)
    regions=dialogue_regions(media.projects,project_id,timeline)
    if len(regions)>(1024 if seconds>75 else 256):raise ProjectError('audio_envelope_limit','語音包絡過多，請縮小預覽區間。',status_code=422)
    store=AgentAudioStore(media.projects,project_id)
    music=output.with_suffix('.music.wav');camera=output.with_suffix('.camera.wav');final=output.with_suffix('.mix.wav')
    command=['ffmpeg','-v','error','-nostdin','-y','-filter_complex_threads','2','-f','lavfi','-i',f'anullsrc=r=48000:cl=stereo:d={seconds}']
    filters=[];labels=['[0:a]'];duck_labels=[];opened=[];settings=[]
    try:
        for index,(clip,layer) in enumerate(layers,1):
            context=store.context(layer['asset_id']);fd,asset=open_asset(context,layer['asset_id']);opened.append(fd)
            duration=layer['duration_frames']/fps;start=(clip.start_frame+layer['start_frame'])/fps
            if start+duration>seconds+.0001 or layer['source_offset_seconds']+duration>asset['duration_seconds']+.0001:
                raise ProjectError('agent_audio_out_of_bounds','混音範圍超出來源或影片。',status_code=422)
            command+=['-ss',str(layer['source_offset_seconds']),'-t',str(duration),'-i',f'/dev/fd/{fd}']
            normalization=max(-18,min(18,-18-asset['rms_db']))
            chain=f'[{index}:{asset["stream_index"]}]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,volume={normalization+layer["gain_db"]:.4f}dB'
            fade=min(layer['fade_seconds'],duration/2)
            if fade:chain+=f',afade=t=in:d={fade:.6f},afade=t=out:st={duration-fade:.6f}:d={fade:.6f}'
            chain+=f',adelay={round(start*1000)}|{round(start*1000)}'
            chain+=f'[layer{index}]';filters.append(chain)
            (duck_labels if layer['duck'] else labels).append(f'[layer{index}]')
            settings.append({'layer_id':layer['layer_id'],'start_seconds':start,'duration_seconds':duration,
                             'gain_db':layer['gain_db'],'normalization_db':normalization,'duck':layer['duck'],
                             'duck_attenuation_db':7 if layer['duck'] and regions else 0})
        if duck_labels:
            prefix=''.join(duck_labels)
            if len(duck_labels)>1:prefix+=f'amix=inputs={len(duck_labels)}:duration=longest:normalize=0,'
            filters.append(prefix+f"volume='{duck_expression(regions)}':eval=frame[ducked]")
            labels.append('[ducked]')
        filters.append(''.join(labels)+f'amix=inputs={len(labels)}:duration=first:normalize=0[music]')
        run_process(command+['-filter_complex',';'.join(filters),'-map','[music]','-c:a','pcm_s16le',str(music)],cancel,pass_fds=tuple(opened),timeout=300)
    finally:
        for fd in opened:os.close(fd)
    run_process(['ffmpeg','-v','error','-nostdin','-y','-i',str(base),'-i',str(music),
                 '-filter_complex','[0:a][1:a]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.750:level=0[a]',
                 '-map','0:v','-map','[a]','-c:v','copy','-c:a','aac','-t',str(seconds),'-movflags','+faststart',str(output)],cancel,timeout=300)
    for source,destination in ((base,camera),(output,final)):
        run_process(['ffmpeg','-v','error','-nostdin','-y','-i',str(source),'-vn','-ac','2','-ar','48000','-c:a','pcm_s16le',str(destination)],cancel,timeout=120)
    measured={name:measure_stem(path) for name,path in (('camera',camera),('music',music),('mix',final))}
    issues=[]
    if measured['music']['peak_dbfs'] is None or measured['music']['peak_dbfs'] < -50:
        issues.append({'code':'music_effectively_silent','message':'渲染後的配樂 stem 幾乎無聲，請調整來源與音量。'})
    atomic_json(output.with_suffix('.mix.json'), {'measurements':measured,'layers':settings,
        'dialogue_regions_seconds':regions,'duck_basis':'aligned_source_words' if regions else 'unavailable_no_attenuation',
        'issues':issues,'audio_listened':False,'scope':'Rendered stems and numeric levels; audibility and musical taste still require listening.'})
