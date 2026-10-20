"""Search current source speech before picking a cut; no synthetic event labels."""
from fractions import Fraction
import json
import math
from .production_media import source_rows
from .program_reference import reference_for
from .agent_models import ToolResult


def search_dialogue(tools,args):
    from .speech_quality import speech_flags
    sources={}
    for source in source_rows(tools.repository.projects,tools.project_id):
        if source['kind']=='video':sources.setdefault(source['version_id'],source)
    reference=reference_for(tools.repository,tools.project_id,tools.run_id)
    excluded=set(reference['source_version_ids']) if reference and args.get('exclude_reference_sources',True) else set()
    query=args.get('query','').strip().casefold();version=args.get('version_id');drafts=args.get('include_drafts',False)
    results=[];unreviewed=0;incomplete=0
    with tools.repository.projects.connect(tools.project_id) as db:
        rows=db.execute("SELECT tr.*,pi.review_required FROM current_preparation cp JOIN preparation_items pi ON pi.run_id=cp.run_id JOIN transcription_revisions tr ON tr.revision_id=pi.transcript_revision_id WHERE tr.status IN ('transcribed','needs_review') AND tr.stream_index=(SELECT MIN(stream_index) FROM media_streams WHERE version_id=tr.version_id AND kind='audio') ORDER BY tr.version_id,tr.revision_id").fetchall()
        seen=set()
        for row in rows:
            vid=row['version_id']
            if vid not in sources or vid in excluded or vid in seen or (version and version!=vid):continue
            seen.add(vid);source=sources[vid]
            annotation=db.execute('SELECT * FROM transcript_annotations WHERE version_id=? AND stream_index=? AND source_revision_id=?',(vid,row['stream_index'],row['revision_id'])).fetchone()
            if annotation and annotation['discarded']:continue
            pending=bool((row['review_required'] or row['status']=='needs_review') and not (annotation and annotation['confirmed']))
            corrections={} if annotation is None else {str(c['ordinal']):c['text'] for c in json.loads(annotation['corrections_json'])}
            segments=db.execute('SELECT * FROM transcript_segments WHERE revision_id=? ORDER BY ordinal',(row['revision_id'],)).fetchall()
            tb=json.loads(row['source_span_json'])['time_base'];unit=Fraction(source['time_base']['num'],source['time_base']['den']);asr_unit=Fraction(tb['num'],tb['den'])
            for n,seg in enumerate(segments):
                text=corrections.get(str(seg['ordinal']),seg['text'])
                if query and query not in text.casefold():continue
                if pending and not drafts:unreviewed+=1;continue
                left=int(seg['start_pts'])*asr_unit;right=int(seg['end_pts'])*asr_unit
                if left<int(source['start_pts'])*unit-Fraction(1,30) or right>int(source['end_pts'])*unit+Fraction(1,30):
                    incomplete+=1;continue
                # Audio sample tails may exceed the last video frame. Return
                # an executable video span, plus the unchanged ASR timing.
                start=max(int(source['start_pts']),math.floor(left/unit));end=min(int(source['end_pts']),math.ceil(right/unit))
                if start>=end:continue
                results.append({'version_id':vid,'display_name':source['display_name'],'source_revision_id':row['revision_id'],
                    'segment_id':seg['segment_id'],'text':text,'confidence':seg['confidence'],
                    'verification':'asr_draft' if pending else 'reviewed_source',
                    'quality_flags':speech_flags(text) if pending else [],
                    'start_offset_seconds':float((start-int(source['start_pts']))*unit),'duration_seconds':float((end-start)*unit),
                    'source':{'version_id':vid,'stream_index':source['stream_index'],'start_pts':str(start),'end_pts':str(end),'time_base':source['time_base']},
                    'asr_start_seconds':float(left),'asr_end_seconds':float(right),
                    'context':[{'segment_id':c['segment_id'],'text':corrections.get(str(c['ordinal']),c['text'])} for c in segments[max(0,n-1):n+2]]})
    results.sort(key=lambda r:(r['display_name'],r['version_id'],r['start_offset_seconds']))
    offset=args.get('offset',0);limit=args.get('limit',12)
    return ToolResult({'utterances':results[offset:offset+limit],'total':len(results),'next_offset':offset+limit if offset+limit<len(results) else None,
        'unreviewed_omitted':unreviewed,'incomplete_omitted':incomplete,'excluded_reference_sources':len(excluded),
        'scope':'Current inventory and preparation only. Text matches are discovery clues, not proof of event continuity or causal reactions.',
        'next_step':'Choose a promising event, register_source_range around complete speech and nearby context, then inspect_candidates/sample_source. Explore another event rather than reproducing reference footage.'})
