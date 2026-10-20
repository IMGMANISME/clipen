"""Shot coverage and revision-bound, explicitly model-authored editorial findings.

Coverage proves which stills were delivered, never that motion or sound was
understood. Findings expose the model's decisions rather than inventing a score.
"""
import json
from fractions import Fraction

from clipen.domain import TrackKind
from clipen.media import MediaPreviewRepository
from clipen.projects import ProjectError
from .agent_models import ToolResult
from .production_media import sample_frames, transcript_cues
from .production_preview import _event, inspect_preview


def shot_offsets(item, fps):
    # First/last real frames, not the following shot at the outgoing boundary.
    return [item.start_frame/fps, (item.start_frame+(item.duration_frames-1)/2)/fps,
            (item.start_frame+item.duration_frames-1)/fps]


def coverage_targets(timeline):
    fps=timeline.frame_rate.numerator/timeline.frame_rate.denominator
    return {item.item_id:shot_offsets(item,fps) for t in timeline.tracks if t.kind is TrackKind.VIDEO for item in t.items}


def text_targets(timeline):
    fps=timeline.frame_rate.numerator/timeline.frame_rate.denominator
    return {item.item_id:(item.start_frame+(item.duration_frames-1)/2)/fps
            for t in timeline.tracks if t.kind is TrackKind.TITLE for item in t.items}


def inspect_cut(tools,args):
    revision=tools.repository.current_revision(tools.project_id,tools.run_id)
    timeline=revision.timeline;targets=coverage_targets(timeline)
    clips={i.item_id:i for i in tools._video().items}
    if any(i not in clips for i in args['item_ids']):
        raise ProjectError('timeline_item_not_found','指定鏡頭不在目前剪輯。',status_code=404)
    fps=Fraction(timeline.frame_rate.numerator,timeline.frame_rate.denominator)
    speech=tools._production_list_transcripts({'include_drafts':True}).payload
    cues,limitations=speech['cues'],speech['limitations']
    images=[];shots=[]
    for identifier in args['item_ids']:
        item=clips[identifier]
        texts=[i for t in timeline.tracks if t.kind is TrackKind.TITLE for i in t.items
               if i.start_frame<item.start_frame+item.duration_frames and item.start_frame<i.start_frame+i.duration_frames]
        if args.get('render_id'):
            offsets=targets[identifier]+[text_targets(timeline)[i.item_id] for i in texts[:1]]
            # Text can span shots; its midpoint must still be inspected, once.
            offsets=sorted(set(offsets))
            for start in range(0,len(offsets),8):
                result=inspect_preview(tools,{'render_id':args['render_id'],'offsets_seconds':offsets[start:start+8]})
                if result.payload['render']['state']!='succeeded' or result.payload['stale']:
                    return ToolResult(result.payload)
                images.extend(result.images)
            samples=[{'timeline_seconds':o} for o in offsets]
        else:
            span=item.source.span;unit=Fraction(span.time_base.numerator,span.time_base.denominator)
            duration=span.duration_pts*unit
            last=min(Fraction(item.duration_frames-1,1)/fps,duration-Fraction(1,1)/fps)
            offsets=[0,float(max(0,last)/2),float(max(0,last))]
            media=getattr(tools,'media',None) or MediaPreviewRepository(tools.repository.projects)
            paths,samples=sample_frames(media,tools.project_id,span,offsets,tools._workdir('samples'),tools._cancel_event())
            images.extend(paths)
        shots.append({'item_id':identifier,'source':item.source.span.to_storage(),'samples':samples,
            'image_count':len(samples),'texts':[{'item_id':i.item_id,'text':i.text,'start_frame':i.start_frame,'duration_frames':i.duration_frames} for i in texts],
            'dialogue':[c for c in cues if c['item_id']==identifier],
            'dialogue_problems':[c for c in limitations if c['item_id']==identifier]})
    return ToolResult({'revision_id':revision.revision_id,'shots':shots,
        'image_order':'shots in requested order; frames in sample order',
        'coverage':'start/middle/end stills; continuous motion and audio remain unreviewed',
        'text_coverage':'At most one additional text midpoint per shot; validate_sequence lists remaining text samples for inspect_preview.',
        'editorial_questions':['Does this show the event or only preparation/empty wall?',
            'Does the scene establish a place and then deliver action/reaction?',
            'Is each location/day assertion supported by this exact shot?',
            'Does retained speech finish naturally? Repair source spans before captions.']},tuple(images))


def review_cut(tools,args):
    from .production_preview import finish_review_status
    revision=tools.repository.current_revision(tools.project_id,tools.run_id)
    status=finish_review_status(tools)
    if not status['render_coverage_complete']:
        raise ProjectError('agent_preview_review_required','請先檢視每個鏡頭的頭、中、尾及所有文字顯示畫面。',status_code=409)
    expected=set(coverage_targets(revision.timeline));texts=set(text_targets(revision.timeline))
    shots=args['shots'];checks=args['text_checks']
    if len(shots)!=len(expected) or {s['item_id'] for s in shots}!=expected or len(checks)!=len(texts) or {c['item_id'] for c in checks}!=texts:
        raise ProjectError('agent_review_incomplete','審片紀錄必須逐一列出目前所有鏡頭與文字，不可重複或遺漏。',status_code=422)
    if status['unresolved']:
        still_unresolved={s['item_id'] for s in shots if s['decision']=='revise'}|{c['item_id'] for c in checks if not c['matches']}
        if not set(status['unresolved'])<=still_unresolved:
            raise ProjectError('agent_revision_required','這個版本已記錄待修問題；請先實際修改時間軸，再輸出並檢視新版，不能只改評語清除問題。',status_code=409)
    from .production_preview import render_recipe_id
    report={'revision_id':revision.revision_id,'recipe_id':render_recipe_id(),'authority':'model_authored',**args}
    _event(tools,'editorial_review',report)
    return ToolResult({'assessment':report,'unresolved':[s['item_id'] for s in shots if s['decision']=='revise']+[c['item_id'] for c in checks if not c['matches']],
        'human_review':'pending','quality_claim':'This records model findings, not independent approval.'})
