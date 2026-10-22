"""Observable editorial structure and caption omissions, without aesthetic scores."""
from fractions import Fraction
import json
import unicodedata


def pacing(timeline):
    from clipen.domain import TrackKind
    fps=Fraction(timeline.frame_rate.numerator,timeline.frame_rate.denominator)
    runs=[]
    for track in timeline.tracks:
        if track.kind is not TrackKind.VIDEO:continue
        previous=None
        for item in track.items:
            span=item.source.span
            unit=Fraction(span.time_base.numerator,span.time_base.denominator)
            left,right=int(span.start_pts)*unit,int(span.end_pts)*unit
            contiguous=(previous is not None and previous['version_id']==span.version_id
                        and previous['stream_index']==span.stream_index
                        and abs(previous['source_end']-left)<=1/fps
                        and previous['end_frame']==item.start_frame)
            if contiguous:
                previous['item_ids'].append(item.item_id)
                previous['end_frame']=item.start_frame+item.duration_frames
                previous['source_end']=right
            else:
                previous={'version_id':span.version_id,'stream_index':span.stream_index,'item_ids':[item.item_id],
                          'start_frame':item.start_frame,'end_frame':item.start_frame+item.duration_frames,'source_end':right}
                runs.append(previous)
    return {'duration_seconds':float(timeline.duration_frames/fps),
            'source_runs':[{k:v for k,v in r.items() if k!='source_end'} |
                           {'duration_seconds':float((r['end_frame']-r['start_frame'])/fps)} for r in runs],
            'scope':'Adjacent spans from the same source are grouped even when split into clips. This is source continuity, not visual scene detection or an aesthetic verdict.'}


def caption_coverage(tools, speech):
    from .production_media import load_text_styles
    timeline=tools._timeline();styles=load_text_styles(tools.repository.projects,tools.project_id)
    def words(text):
        return ''.join(c.casefold() for c in unicodedata.normalize('NFKC',text) if unicodedata.category(c)[0] not in 'PZC')
    rendered={}
    for track in timeline.tracks:
        if track.track_id!='agent-subtitles':continue
        for item in track.items:
            profile=styles.get(item.style_id,{})
            key=(profile.get('anchor_item_id'),profile.get('cue_id'))
            rendered.setdefault(key,[]).append(item)
    missing=[]
    for cue in speech['cues']:
        expected=words(cue['text'])
        if not expected:continue
        pages=sorted(rendered.get((cue['item_id'],cue['cue_id']),[]),key=lambda i:i.start_frame)
        actual=words(''.join(i.text for i in pages))
        if actual!=expected:
            missing.append({'item_id':cue['item_id'],'cue_id':cue['cue_id'],
                            'start_frame':cue['start_frame'],'duration_frames':cue['duration_frames']})
    # A partially selected legacy ASR segment can disappear from cues entirely.
    # It still needs repair unless a newer successful local job covered that cut.
    with tools.repository.projects.connect(tools.project_id) as db:
        local=[json.loads(r['result_json'])['source'] for r in db.execute(
            "SELECT result_json FROM production_evidence_jobs WHERE run_id=? AND state='succeeded' AND kind IN ('transcribe','align')",(tools.run_id,))]
    fps=Fraction(timeline.frame_rate.numerator,timeline.frame_rate.denominator)
    def locally_analyzed(item):
        span=item.source.span;unit=Fraction(span.time_base.numerator,span.time_base.denominator)
        left,right=int(span.start_pts)*unit,int(span.end_pts)*unit
        return any(s['version_id']==span.version_id and
                   int(s['start_pts'])*Fraction(s['time_base']['num'],s['time_base']['den'])<=left+1/fps and
                   int(s['end_pts'])*Fraction(s['time_base']['num'],s['time_base']['den'])>=right-1/fps for s in local)
    shots={i.item_id:i for i in tools._video().items}
    unresolved=sorted({v['item_id'] for v in speech['limitations'] if
                      (v.get('reason') in {'partial_utterance','partial_utterance_requires_alignment'} or v.get('partial'))
                      and v['item_id'] in shots and (v.get('reason')=='partial_utterance_requires_alignment' or not locally_analyzed(shots[v['item_id']]))})
    return {'complete':not missing and not unresolved,'missing_cues':missing,'unresolved_item_ids':unresolved}
