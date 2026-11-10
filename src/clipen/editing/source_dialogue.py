"""Search current source speech before picking a cut; no synthetic event labels."""
from fractions import Fraction
import json
import math
from .production_media import source_rows
from .agent_models import ToolResult


def search_dialogue(tools,args):
    from .speech_quality import speech_flags
    sources={}
    for source in (tools._source_rows() if hasattr(tools,'_source_rows') else source_rows(tools.repository.projects,tools.project_id)):
        if source['kind']=='video':sources.setdefault(source['version_id'],source)
    query=args.get('query','').strip().casefold();version=args.get('version_id');drafts=args.get('include_drafts',False)
    results=[];unreviewed=0;incomplete=0
    with tools.repository.projects.connect(tools.project_id) as db:
        rows=db.execute("SELECT tr.*,pi.review_required FROM current_preparation cp JOIN preparation_items pi ON pi.run_id=cp.run_id JOIN transcription_revisions tr ON tr.revision_id=pi.transcript_revision_id WHERE tr.status IN ('transcribed','needs_review') AND tr.stream_index=(SELECT MIN(stream_index) FROM media_streams WHERE version_id=tr.version_id AND kind='audio') ORDER BY tr.version_id,tr.revision_id").fetchall()
        seen=set()
        for row in rows:
            vid=row['version_id']
            if vid not in sources or vid in seen or (version and version!=vid):continue
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
    local_ranges=[]
    jobs=[]
    if hasattr(tools.repository,'production_session'):
        with tools.repository.projects.connect(tools.project_id) as db:
            jobs=db.execute("SELECT job_id,result_json FROM production_evidence_jobs WHERE run_id=? AND state='succeeded' AND kind IN ('transcribe','align') ORDER BY created_at DESC,rowid DESC",(tools.run_id,)).fetchall()
    local_seen=set()
    for job in jobs:
        value=json.loads(job['result_json']);span=value['source'];vid=span['version_id']
        if vid not in sources or (version and vid!=version):continue
        source=sources[vid];unit=Fraction(source['time_base']['num'],source['time_base']['den'])
        local_ranges.append(span)
        for n,segment in enumerate(value['segments']):
            text=segment['text'];left=value['source_start_seconds']+segment['start_seconds'];right=value['source_start_seconds']+segment['end_seconds']
            identity=(vid,round(left,4),round(right,4))
            if identity in local_seen:continue
            local_seen.add(identity)
            results=[r for r in results if not (r['version_id']==vid and abs(r['asr_start_seconds']-left)<.05 and abs(r['asr_end_seconds']-right)<.05)]
            if query and query not in text.casefold():continue
            if not drafts:unreviewed+=1;continue
            results.append({'version_id':vid,'display_name':source['display_name'],'source_revision_id':job['job_id'],
                'segment_id':str(n),'text':text,'confidence':None,'verification':'asr_draft','quality_flags':speech_flags(text),
                'start_offset_seconds':left-float(int(source['start_pts'])*unit),'duration_seconds':right-left,
                'source':{**span,'start_pts':str(round(left/float(unit))),'end_pts':str(round(right/float(unit)))},
                'asr_start_seconds':left,'asr_end_seconds':right,
                'context':[{'segment_id':str(i),'text':s['text']} for i,s in enumerate(value['segments'][max(0,n-1):n+2],max(0,n-1))]})
    results.sort(key=lambda r:(r['display_name'],r['version_id'],r['start_offset_seconds']))
    offset=args.get('offset',0);limit=args.get('limit',12)
    return ToolResult({'utterances':results[offset:offset+limit],'total':len(results),'next_offset':offset+limit if offset+limit<len(results) else None,
        'unreviewed_omitted':unreviewed,'incomplete_omitted':incomplete,
        'coverage': {'video_sources': len(sources), 'sources_with_transcripts': len(seen|{r['version_id'] for r in local_ranges}),
                     'untranscribed_version_ids': sorted(set(sources)-seen-{r['version_id'] for r in local_ranges}),
                     'local_transcribed_ranges':local_ranges,
                     'limitations':'A source with transcripts may still have uncovered intervals; local ranges cover only their recorded source span.'},
        'scope':'Inventoried authorized sources; only existing transcripts searched. Unknown footage is not evidence of absence. Text matches are not proof of event continuity.',
        'next_step':'Choose a promising event, register_source_range around complete speech and nearby context, then inspect_candidates/sample_source.'})
