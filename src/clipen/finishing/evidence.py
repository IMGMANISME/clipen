"""Bounded visual snapshots for an explicitly consented finishing attempt."""

from __future__ import annotations

from decimal import Decimal

from .audio import run_process


def snapshot_frames(media, project_id, context, workspace, cancel):
    clips = [item for track in context['timeline']['tracks'] if track['kind']=='video' for item in track['items']]
    count = min(8,len(clips))
    indices = sorted({round(i*(len(clips)-1)/max(1,count-1)) for i in range(count)})
    images, manifest = [], []
    for index in indices:
        clip = clips[index]
        span = clip['source']
        base = Decimal(span['time_base']['num'])/Decimal(span['time_base']['den'])
        pts = (Decimal(span['start_pts'])+Decimal(span['end_pts']))/2
        at = pts*base
        source = media.open(project_id,span['version_id'])
        path = workspace/f'shot-{index:02d}.png'
        try:
            run_process(['ffmpeg','-v','error','-nostdin','-seek_timestamp','1','-ss',format(at,'f'),
                         '-i',f'/dev/fd/{source.descriptor}','-map',f"0:{span['stream_index']}",
                         '-frames:v','1','-vf','scale=512:-2','-threads','1',str(path)],
                        cancel,pass_fds=(source.descriptor,),timeout=30)
        finally:
            source.close()
        path.chmod(0o400)
        images.append(path)
        manifest.append({'image':path.name,'clip_id':clip['item_id'],'source_pts':str(pts),
                         'time_base':span['time_base'],'coverage':'single_midpoint_frame'})
    return tuple(images), manifest
