"""Explicit analyzed ranges for retained speech; no transcript match is not absence."""
from fractions import Fraction
import json


def speech_coverage(tools):
    audio_versions={row['version_id'] for row in tools._source_rows() if row['kind']=='audio'}
    ranges={}
    with tools.repository.projects.connect(tools.project_id) as db:
        local=db.execute("SELECT result_json FROM production_evidence_jobs WHERE run_id=? AND state='succeeded' AND kind IN ('transcribe','align')",(tools.run_id,)).fetchall()
        legacy=db.execute("SELECT tr.source_span_json FROM current_preparation cp JOIN preparation_items pi ON pi.run_id=cp.run_id JOIN transcription_revisions tr ON tr.revision_id=pi.transcript_revision_id WHERE pi.status IN ('transcribed','no_speech') AND tr.status IN ('transcribed','needs_review','no_speech_detected')").fetchall()
    spans=[json.loads(row['result_json'])['source'] for row in local]+[json.loads(row['source_span_json']) for row in legacy]
    for span in spans:
        unit=Fraction(span['time_base']['num'],span['time_base']['den'])
        ranges.setdefault(span['version_id'],[]).append((int(span['start_pts'])*unit,int(span['end_pts'])*unit))
    unknown=[]
    fps=Fraction(tools._timeline().frame_rate.numerator,tools._timeline().frame_rate.denominator)
    for clip in tools._video().items:
        if clip.source.span.version_id not in audio_versions:continue
        if any(e.effect_id=='audio.source' and e.parameters['mode']=='mute' for e in clip.effects):continue
        span=clip.source.span;unit=Fraction(span.time_base.numerator,span.time_base.denominator)
        left=int(span.start_pts)*unit;right=int(span.end_pts)*unit;cursor=left
        for start,end in sorted(ranges.get(span.version_id,[])):
            if end<=cursor:continue
            if start>cursor+1/fps:break
            cursor=max(cursor,end)
        if cursor<right-1/fps:
            unknown.append({'item_id':clip.item_id,'source':span.to_storage(),'unknown_from_seconds':float(cursor)})
    return {'unknown_ranges':unknown,'complete':not unknown,
            'scope':'Local transcription coverage only; ASR correctness, hearing and semantics are separate.'}
