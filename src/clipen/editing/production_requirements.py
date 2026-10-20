"""Actual production contents and explicit user requirements, not aesthetic scoring."""
from clipen.domain import TrackKind
from clipen.domain import EditTimeline
import json
from .production_media import AgentAudioStore


def production_contents(projects,project_id,timeline):
    fps=timeline.frame_rate.numerator/timeline.frame_rate.denominator
    videos=[i for t in timeline.tracks if t.kind is TrackKind.VIDEO for i in t.items]
    directory=projects.root/project_id/'agent-audio'
    assets={a['asset_id']:a for a in AgentAudioStore(projects,project_id).public_assets()} if directory.exists() else {}
    music=[];transitions=[];motion=[]
    for n,item in enumerate(videos):
        for effect in item.effects:
            if effect.effect_id=='audio.layers':
                for layer in effect.parameters['layers']:
                    asset=assets.get(layer['asset_id'],{});provenance=asset.get('provenance',{})
                    licensed=all(provenance.get(k) for k in ('title','creator','source_url','license_url','license')) and provenance.get('license')!='unknown'
                    music.append({'asset_id':layer['asset_id'],'name':asset.get('name','未取得音訊來源'),'role':asset.get('role','unknown'),
                        'start_seconds':(item.start_frame+layer['start_frame'])/fps,'duration_seconds':layer['duration_frames']/fps,
                        'gain_db':layer['gain_db'],'duck':layer['duck'],'licensed':bool(licensed),'provenance':provenance})
            elif effect.effect_id=='video.transition' and n>0 and effect.parameters['kind']!='none':
                transitions.append({'item_id':item.item_id,'at_seconds':item.start_frame/fps,**effect.parameters})
            elif effect.effect_id in {'video.punch_in','video.slow_push','video.reframe','video.speed_ramp'}:
                motion.append({'item_id':item.item_id,'effect_id':effect.effect_id})
    return {'clip_count':len(videos),'cut_count':max(0,len(videos)-1),'music':music,'transitions':transitions,'motion':motion,
        'scope':'Actual timeline contents; placement and metadata do not prove listening or aesthetic quality.'}


def requirement_issues(tools):
    direction=tools.repository.creative_direction(tools.project_id,tools.run_id)
    required=direction.get('production_requirements',{})
    if not any(required.values()):return []
    contents=production_contents(tools.repository.projects,tools.project_id,tools._timeline());issues=[]
    if required.get('music') and not any(m['role']=='music' and m['licensed'] and m['duration_seconds']>0 and m['gain_db']>-60 for m in contents['music']):
        issues.append({'code':'production_music_required','message':'使用者要求配樂。請搜尋或選擇有作者、來源與授權的公開音樂，實際用 place_audio 放入並調整音量／人聲壓低；只有原音或讀取曲目清單不算完成。'})
    if required.get('transitions') and not contents['transitions']:
        issues.append({'code':'production_transition_required','message':'使用者要求轉場。請在有敘事用途的剪點使用 set_transition；只有片尾淡出不算剪點轉場。'})
    if required.get('editing'):
        with tools.repository.projects.connect(tools.project_id) as db:
            first=db.execute('SELECT payload_json FROM edit_agent_revisions WHERE run_id=? AND ordinal=0',(tools.run_id,)).fetchone()
        def sequence(timeline):return [(i.source.span.to_storage(),i.duration_frames) for t in timeline.tracks if t.kind is TrackKind.VIDEO for i in t.items]
        if contents['cut_count']<1 or (first and sequence(EditTimeline.from_payload(json.loads(first['payload_json'])))==sequence(tools._timeline())):
            issues.append({'code':'production_editing_required','message':'請實際選材、精剪及安排至少一個剪點，不能只在原版加字幕。'})
    return issues
