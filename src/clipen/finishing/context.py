"""Freeze the confirmed story, visual evidence and source-timed speech for AI finishing."""

from __future__ import annotations

from fractions import Fraction
import json
import math

from clipen.editing.style_proof import is_style_proof
from clipen.projects import ProjectError

from .models import digest


def build_context(projects, agents, project_id, library):
    run = agents.latest_successful(project_id)
    if run is None:
        raise ProjectError("finishing_direction_required", "請先完成 AI 剪輯方向樣片。", status_code=409)
    revision = agents.final_revision(project_id, run.run_id)
    timeline = revision.timeline
    fps = Fraction(timeline.frame_rate.numerator, timeline.frame_rate.denominator)
    if not is_style_proof(timeline) or not 30 <= timeline.duration_frames/fps <= 75:
        raise ProjectError("finishing_direction_required", "請先完成 AI 剪輯方向樣片。", status_code=409)
    clips = [i for t in timeline.tracks if t.kind.value == 'video' for i in t.items]
    if len([t for t in timeline.tracks if t.kind.value=='video']) != 1 or any(t.kind.value in {'audio','graphic'} and t.items for t in timeline.tracks):
        raise ProjectError("finishing_layout_unsupported", "完成度製作目前需要單一畫面軌與鏡頭原音。", status_code=409)
    cursor = 0
    for clip in clips:
        if clip.start_frame != cursor:
            raise ProjectError("finishing_layout_unsupported", "方向樣片畫面需連續，不能有空白或重疊。", status_code=409)
        cursor += clip.duration_frames
    if cursor != timeline.duration_frames:
        raise ProjectError("finishing_layout_unsupported", "方向樣片結尾缺少畫面。", status_code=409)
    if timeline.width*timeline.height > 1920*1080 or fps not in {24,25,30,60}:
        raise ProjectError("finishing_layout_unsupported", "完成度製作目前支援最高 1080p、24／25／30／60 fps。", status_code=409)
    cues = []
    audio_streams = {}
    speech_limitations = []
    with projects.connect(project_id) as db:
        current = db.execute(
            "SELECT pr.plan_id,pr.revision,pr.status,pr.content_hash FROM current_plan cp "
            "JOIN plan_revisions pr ON pr.plan_id=cp.plan_id AND pr.revision=cp.revision WHERE cp.slot=1"
        ).fetchone()
        if current is None or (current['plan_id'],current['revision'],current['content_hash']) != (timeline.plan_id,timeline.plan_revision,timeline.plan_content_hash) or current['status'] not in {'confirmed','executing','reviewed','accepted'}:
            raise ProjectError("finishing_direction_stale", "剪輯計畫已改變，請先完成新版方向樣片。", status_code=409)
        for clip in clips:
            span = clip.source.span
            video = db.execute("SELECT details_json FROM media_streams WHERE version_id=? AND stream_index=? AND kind='video'",(span.version_id,span.stream_index)).fetchone()
            if video is None:
                raise ProjectError("finishing_video_missing", "方向樣片的影像串流已不存在，請重新檢查素材。", status_code=409)
            if json.loads(video['details_json']).get('color_transfer') in {'smpte2084','arib-std-b67'}:
                raise ProjectError("finishing_hdr_unsupported", "目前完成度製作支援 SDR；HDR 素材需要先準備 SDR 版本。", status_code=409)
            available = db.execute("SELECT stream_index FROM media_streams WHERE version_id=? AND kind='audio' ORDER BY stream_index",(span.version_id,)).fetchall()
            audio_streams[clip.item_id] = None if not available else available[0]['stream_index']
            row = db.execute(
                "SELECT tr.*,pi.review_required FROM transcription_revisions tr "
                "JOIN preparation_items pi ON pi.transcript_revision_id=tr.revision_id "
                "JOIN current_preparation cp ON cp.run_id=pi.run_id "
                "WHERE tr.version_id=? AND tr.stream_index=? AND tr.status='transcribed' LIMIT 1", (span.version_id,audio_streams[clip.item_id]),
            ).fetchone()
            if row is None: continue
            annotation = db.execute("SELECT * FROM transcript_annotations WHERE version_id=? AND stream_index=? AND source_revision_id=?", (span.version_id,row['stream_index'],row['revision_id'])).fetchone()
            if annotation and annotation['discarded']: continue
            if row['review_required'] and not (annotation and annotation['confirmed']):
                speech_limitations.append({'clip_id':clip.item_id,'reason':'transcript_needs_review'})
                continue
            corrections = {} if annotation is None else {str(c['ordinal']):c['text'] for c in json.loads(annotation['corrections_json'])}
            source = json.loads(row['source_span_json'])
            base = source['time_base']
            tb = Fraction(base['num'],base['den'])
            clip_tb = Fraction(span.time_base.numerator,span.time_base.denominator)
            start, end = int(span.start_pts)*clip_tb, int(span.end_pts)*clip_tb
            for seg in db.execute("SELECT * FROM transcript_segments WHERE revision_id=? ORDER BY ordinal", (row['revision_id'],)):
                left, right = max(start,int(seg['start_pts'])*tb), min(end,int(seg['end_pts'])*tb)
                if right <= left: continue
                # Never pretend an entire sentence belongs to a partial source selection.
                complete = left == int(seg['start_pts'])*tb and right == int(seg['end_pts'])*tb
                begin = clip.start_frame + math.floor((int(seg['start_pts'])*tb-start)*fps)
                finish = clip.start_frame + math.ceil((int(seg['end_pts'])*tb-start)*fps)
                text = corrections.get(str(seg['ordinal']), seg['text']) if isinstance(corrections,dict) else seg['text']
                if not text.strip(): continue
                cues.append({"cue_id": 'cue_'+digest([clip.item_id,row['revision_id'],seg['segment_id']])[:24],
                             "clip_id": clip.item_id,"start_frame":begin,"duration_frames":finish-begin,"complete":complete,
                             "text":text,"original_text":seg['text'],"revision_id":row['revision_id'],
                             "segment_id":seg['segment_id'],"source_span": {"version_id":span.version_id,"stream_index":row['stream_index'],"start_pts":seg['start_pts'],"end_pts":seg['end_pts'],"time_base":base}})
    cues.sort(key=lambda c:c['start_frame'])
    if len(cues)>96:
        raise ProjectError("finishing_scope_too_large", "口語片段超過本次製作上限，請縮小方向樣片。", status_code=409)
    assets = [a for lib in library.values() for a in lib['assets']]
    direction, facts = load_creative_context(projects, project_id, timeline)
    return {"base_revision_id": revision.revision_id, "timeline":timeline.to_payload(),"cues":cues,"assets":assets,"audio_streams":audio_streams,
            "confirmed_direction": direction, "shot_facts": facts, "speech_limitations":speech_limitations,
            "directories":{role:lib['directory'] for role,lib in library.items()}}


def load_creative_context(projects, project_id, timeline):
    from clipen.planning import PlanningRepository
    plan, context = PlanningRepository(projects).execution_input(project_id)
    direction = {
        'brief': context.brief, 'title': plan.title, 'summary': plan.summary,
        'reference_style': list(context.reference_notes), 'limitations': list(context.limitations),
        'sections': [{'title': s.title, 'purpose': s.purpose, 'edit_notes': s.edit_notes} for s in plan.sections],
        'blueprint': None if plan.blueprint is None else plan.blueprint.to_payload(),
    }
    facts = []
    for track in timeline.tracks:
        if track.kind.value != 'video': continue
        for clip in track.items:
            span = clip.source.span
            unit = Fraction(span.time_base.numerator, span.time_base.denominator)
            evidence = []
            for candidate in context.candidates:
                source = candidate.source
                other = Fraction(source.time_base.numerator, source.time_base.denominator)
                if source.version_id == span.version_id and int(source.start_pts)*other < int(span.end_pts)*unit and int(source.end_pts)*other > int(span.start_pts)*unit:
                    evidence.append({'candidate_id': candidate.candidate_id, 'description': candidate.evidence_summary,
                                     'transcript_reliability': candidate.transcript_reliability.value})
            facts.append({'evidence_id': 'shot:'+clip.item_id, 'clip_id': clip.item_id, 'source': span.to_storage(), 'observations': evidence[:8]})
    return direction, facts


def provider_context(context):
    return {key: context.get(key) for key in ('base_revision_id', 'timeline', 'cues', 'audio_streams', 'confirmed_direction', 'shot_facts', 'speech_limitations', 'creative_brief', 'feedback', 'previous_plan', 'previous_music')} | {
        'assets': [{k:v for k,v in a.items() if k not in {'relative_path','mtime_ns','fingerprint','size_bytes'}} for a in context['assets']]
    }
