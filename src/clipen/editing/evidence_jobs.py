"""On-demand local source speech jobs, independent of the legacy preparation stage."""
from fractions import Fraction
import json
import hashlib
import math
from pathlib import Path
import threading
import time
import wave

from clipen.domain import TrackKind
from clipen.projects import ProjectError
from clipen.projects.storage import utc_now
from .agent_models import ToolResult, encoded, make_id


class EvidenceTools:
    def _production_prepare_local_speech(self,args):
        session=self._session_value()
        if session is None or not session['intent'].get('allow_local_model_downloads'):
            raise ProjectError('local_model_grant_required','這次製作尚未授權準備本機模型。',status_code=409)
        return self._start_evidence('setup',{'target':args['target'],'release':'clipen-local-speech-v1'},None)

    def _production_transcribe_source_range(self, args):
        candidate = self._candidate(args['candidate_id'])
        span = candidate.source
        duration = float(span.duration_pts*Fraction(span.time_base.numerator,span.time_base.denominator))
        if duration>60:
            raise ProjectError('speech_range_too_large', '單次按需語音分析最多 60 秒，請縮小來源範圍。', status_code=422)
        binding = getattr(self, 'transcriber_binding', None)
        if binding is None or (runtime:=binding()) is None:
            raise ProjectError('asr_unavailable', '本機語音辨識尚未就緒，請先安裝或修復本機語音環境。', status_code=409)
        request = {'source':span.to_storage(), 'language':args.get('language','zh'),
                   'align_words':args.get('align_words',True), 'model':runtime[1]}
        if request['align_words']:
            from .alignment import REVISION
            request['alignment_revision']=REVISION
        return self._start_evidence('transcribe', request, runtime[0])

    def _production_align_source_speech(self, args):
        job = self._evidence_job(args['transcript_job_id'])
        if job['state']!='succeeded' or job['kind']!='transcribe':
            raise ProjectError('transcript_job_required', '請指定已完成的來源轉寫工作。', status_code=422)
        from .alignment import REVISION
        request = {**job['request'], 'transcript_job_id':job['job_id'], 'alignment_revision':REVISION}
        return self._start_evidence('align', request, None)

    def _production_retry_evidence_job(self,args):
        job=self._evidence_job(args['job_id'])
        if job['state'] not in {'failed','cancelled','interrupted'}:
            raise ProjectError('evidence_retry_invalid','只能重試已失敗、取消或中斷的本機工作。',status_code=409)
        request={**job['request'],'retry_request_id':args['request_id']}
        factory=None
        if job['kind']=='transcribe':
            binding=getattr(self,'transcriber_binding',None)
            runtime=binding() if binding else None
            if runtime is None:raise ProjectError('asr_unavailable','請先準備本機語音環境。',status_code=409)
            factory=runtime[0];request['model']=runtime[1]
        return self._start_evidence(job['kind'],request,factory)

    def _production_revise_source_speech(self,args):
        job=self._evidence_job(args['transcript_job_id'])
        if job['state']!='succeeded' or job['kind'] not in {'transcribe','align'}:
            raise ProjectError('transcript_job_required','請指定已完成的來源口語工作。',status_code=422)
        index=args['segment_ordinal']
        if index>=len(job['result']['segments']):
            raise ProjectError('speech_segment_missing','指定口語段落不存在。',status_code=422)
        from .alignment import REVISION
        request={**job['request'],'transcript_job_id':job['job_id'],'alignment_revision':REVISION,
                 'correction':{key:args[key] for key in ('segment_ordinal','text','evidence')}}
        return self._start_evidence('align',request,None)

    def _evidence_job(self, job_id):
        with self.repository.projects.connect(self.project_id) as db:
            row = db.execute('SELECT * FROM production_evidence_jobs WHERE job_id=? AND run_id=?', (job_id,self.run_id)).fetchone()
        if row is None:
            raise ProjectError('evidence_job_not_found','證據工作不屬於這次製作。',status_code=404)
        return {'job_id':row['job_id'], 'kind':row['kind'], 'state':row['state'],
                'request':json.loads(row['request_json']), 'result':json.loads(row['result_json']) if row['result_json'] else None,
                'error_code':row['error_code']}

    def _start_evidence(self, kind, request, factory):
        from clipen.analysis import transcription
        recipe=hashlib.sha256()
        for path in (Path(__file__),Path(__file__).with_name('alignment.py'),Path(__file__).with_name('alignment_worker.py'),
                     Path(transcription.__file__),Path(transcription.__file__).with_name('qwen_worker.py')):
            recipe.update(path.read_bytes())
        key = make_id('evidence_', [kind,request,recipe.hexdigest()])
        identifier = make_id('speech_', [self.run_id,key])
        with self.repository.projects.connect(self.project_id) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT job_id FROM production_evidence_jobs WHERE run_id=? AND cache_key=?', (self.run_id,key)).fetchone()
            if existing:
                db.rollback()
                return ToolResult({'job':self._evidence_job(existing['job_id']), 'reused':True})
            cached=db.execute("SELECT job_id,run_id,result_json FROM production_evidence_jobs WHERE cache_key=? AND state='succeeded' AND kind!='setup' ORDER BY rowid DESC LIMIT 1",(key,)).fetchone()
            if cached is not None:
                result={**json.loads(cached['result_json']),'cache_provenance':{'job_id':cached['job_id'],'run_id':cached['run_id']}}
                db.execute('INSERT INTO production_evidence_jobs VALUES(?,?,?,?,?,?,?,?,?)',
                           (identifier,self.run_id,key,kind,'succeeded',encoded(request),encoded(result),None,utc_now()))
                db.commit()
                return ToolResult({'job':self._evidence_job(identifier),'reused':True})
            if db.execute("SELECT 1 FROM production_evidence_jobs WHERE run_id=? AND state='running'", (self.run_id,)).fetchone():
                raise ProjectError('evidence_busy', '已有本機語音工作執行中，請先讀取或取消。', status_code=409)
            db.execute('INSERT INTO production_evidence_jobs VALUES(?,?,?,?,?,?,?,?,?)',
                       (identifier,self.run_id,key,kind,'running',encoded(request),None,None,utc_now()))
            db.commit()
        cancel = threading.Event()
        if not hasattr(self,'_evidence_jobs'):self._evidence_jobs={}
        worker = threading.Thread(target=self._run_evidence,args=(identifier,factory,cancel),daemon=True,name='clipen-source-speech')
        self._evidence_jobs[identifier]=(cancel,worker)
        worker.start()
        return ToolResult({'job':self._evidence_job(identifier),'reused':False})

    def _run_evidence(self, identifier, factory, cancel):
        transcriber = None
        try:
            from clipen.analysis.audio import PreparedAudio
            from clipen.media import MediaPreviewRepository
            from clipen.finishing.audio import run_process
            from .alignment import REVISION, run_alignment, validate_alignment
            job = self._evidence_job(identifier)
            if job['kind']=='setup':
                def progress(value):
                    with self.repository.projects.connect(self.project_id) as db:
                        db.execute("UPDATE production_evidence_jobs SET result_json=? WHERE job_id=? AND state='running'",(encoded(value),identifier))
                if job['request']['target']=='alignment':
                    from .alignment_setup import install_runtime
                    result=install_runtime(getattr(self,'local_data_root',None),cancel,progress)
                else:
                    setup=getattr(self,'asr_setup_manager',None)
                    if setup is None:raise ProjectError('asr_unavailable','語音環境尚未設定。',status_code=409)
                    from clipen.asr_release import NOTICE_VERSION
                    setup.start_install(NOTICE_VERSION)
                    while setup.status().state.value=='installing':
                        if cancel.wait(.5):
                            setup.cancel_install()
                            raise ProjectError('user_cancelled','已取消本機語音環境準備。',status_code=409)
                    if setup.status().state.value!='ready':raise ProjectError('asr_setup_failed','本機語音環境準備失敗。',status_code=409)
                    result={'ready':True,'target':'transcription'}
                with self.repository.projects.connect(self.project_id) as db:
                    db.execute("UPDATE production_evidence_jobs SET state='succeeded',result_json=? WHERE job_id=? AND state='running'",(encoded(result),identifier))
                return
            request = job['request'];span=request['source']
            unit=Fraction(span['time_base']['num'],span['time_base']['den'])
            start=float(int(span['start_pts'])*unit);duration=float((int(span['end_pts'])-int(span['start_pts']))*unit)
            directory=self._workdir('evidence')/identifier
            directory.mkdir(mode=0o700,exist_ok=True)
            audio=directory/'source.wav'
            media=(getattr(self,'media',None) or MediaPreviewRepository(self.repository.projects)).open(self.project_id,span['version_id'])
            try:
                if not media.audio_stream_indices:
                    raise ProjectError('source_audio_missing','指定素材沒有音訊。',status_code=422)
                stream=span['stream_index'] if span['stream_index'] in media.audio_stream_indices else media.audio_stream_indices[0]
                run_process(['ffmpeg','-v','error','-nostdin','-y','-seek_timestamp','1','-ss',str(start),'-i',f'/dev/fd/{media.descriptor}',
                             '-map',f'0:{stream}','-t',str(duration),'-ac','1','-ar','16000','-c:a','pcm_s16le',str(audio)],
                            cancel,pass_fds=(media.descriptor,),timeout=90)
            finally:media.close()
            with wave.open(str(audio),'rb') as wav:
                prepared=PreparedAudio(audio,wav.getframerate(),wav.getnchannels(),wav.getsampwidth(),wav.getnframes())
            if job['kind']=='transcribe':
                transcriber=factory()
                transcript_output=directory/'transcript.json'
                transcript_output.touch(mode=0o600,exist_ok=False)
                result=transcriber.transcribe(prepared,transcript_output,language=request['language'],cancel=cancel)
                segments=[{'text':seg.text,'start_seconds':seg.start_sample/16000,
                           'end_seconds':seg.end_sample/16000} for seg in result.segments]
            else:
                segments=self._evidence_job(request['transcript_job_id'])['result']['segments']
                correction=request.get('correction')
                if correction:
                    segment=segments[correction['segment_ordinal']]
                    segment.update({'source_asr_text':segment.get('source_asr_text',segment['text']),
                                    'text':correction['text'],'wording_authority':'model_proposed_correction',
                                    'correction_evidence':correction['evidence']})
                    segment.pop('words',None)
            alignment_error=None
            if job['kind']=='align' or request.get('align_words'):
                for index,segment in enumerate(segments):
                    if job['kind']=='align' and request.get('correction') and index!=request['correction']['segment_ordinal']:continue
                    if not segment['text'].strip():continue
                    chunk=directory/f'align-{index}.wav'
                    length=segment['end_seconds']-segment['start_seconds']
                    run_process(['ffmpeg','-v','error','-nostdin','-y','-ss',str(segment['start_seconds']),'-i',str(audio),'-t',str(length),
                                 '-ac','1','-ar','16000','-c:a','pcm_s16le',str(chunk)],cancel,timeout=30)
                    try:
                        raw=run_alignment(getattr(self,'local_data_root',None),chunk,segment['text'],request['language'],directory/f'words-{index}.json',cancel)
                        words=validate_alignment(raw,segment['text'],length)
                        segment['words']=[{**word,'start_seconds':segment['start_seconds']+word['start_seconds'],
                                            'end_seconds':segment['start_seconds']+word['end_seconds']} for word in words]
                    except ProjectError as error:
                        if job['kind']=='align':raise
                        alignment_error=error.code
            payload={'source':span,'segments':segments,'verification':'asr_draft',
                     'timing_basis':'forced_alignment' if segments and all(s.get('words') for s in segments) else 'asr_segment',
                     'alignment_revision':REVISION if any(s.get('words') for s in segments) else None,
                     'alignment_error':alignment_error,'audio_listened':False,'source_start_seconds':start,
                     'coverage_seconds':float(prepared.duration_seconds)}
            if cancel.is_set() or self._cancel_event().is_set():
                raise ProjectError('user_cancelled','已取消來源證據工作。',status_code=409)
            with self.repository.projects.connect(self.project_id) as db:
                db.execute("UPDATE production_evidence_jobs SET state='succeeded',result_json=? WHERE job_id=? AND state='running'", (encoded(payload),identifier))
        except Exception as error:
            with self.repository.projects.connect(self.project_id) as db:
                db.execute('UPDATE production_evidence_jobs SET state=?,error_code=? WHERE job_id=? AND state=\'running\'',
                           ('cancelled' if cancel.is_set() or self._cancel_event().is_set() else 'failed',getattr(error,'code','source_evidence_failed'),identifier))
        finally:
            if transcriber is not None:transcriber.close()

    def _production_read_evidence_job(self, args):
        deadline=time.monotonic()+args.get('wait_seconds',0)
        while (job:=self._evidence_job(args['job_id']))['state']=='running' and time.monotonic()<deadline:
            time.sleep(.1)
        return ToolResult({'job':job})

    def _production_cancel_evidence_job(self, args):
        job=self._evidence_job(args['job_id'])
        token=getattr(self,'_evidence_jobs',{}).get(args['job_id'])
        if token:token[0].set()
        with self.repository.projects.connect(self.project_id) as db:
            db.execute("UPDATE production_evidence_jobs SET state='cancelled' WHERE job_id=? AND run_id=? AND state='running'",(args['job_id'],self.run_id))
        return ToolResult({'job':self._evidence_job(job['job_id'])})


def local_cues(tools, timeline, *, include_drafts):
    """Map exact source word evidence into the selected cut, not ASR window fractions."""
    from .production_media import digest
    with tools.repository.projects.connect(tools.project_id) as db:
        rows=db.execute("SELECT job_id,result_json FROM production_evidence_jobs WHERE run_id=? AND state='succeeded' AND kind IN ('transcribe','align') ORDER BY created_at DESC,rowid DESC",(tools.run_id,)).fetchall()
    cues=[];limitations=[];seen=set()
    fps=Fraction(timeline.frame_rate.numerator,timeline.frame_rate.denominator)
    for track in timeline.tracks:
        if track.kind is not TrackKind.VIDEO:continue
        for item in track.items:
            if any(e.effect_id=='audio.source' and e.parameters['mode']=='mute' for e in item.effects):continue
            if any(e.effect_id=='video.speed_ramp' for e in item.effects):
                limitations.append({'item_id':item.item_id,'reason':'retimed_speech_mapping_unavailable'})
                continue
            span=item.source.span;unit=Fraction(span.time_base.numerator,span.time_base.denominator)
            cut_left=float(int(span.start_pts)*unit);cut_right=float(int(span.end_pts)*unit)
            # A fresh, fully covering transcription supersedes older overlapping
            # windows. Combining both produces duplicate/conflicting captions.
            covering=[]
            for row in rows:
                value=json.loads(row['result_json'])
                if value['source']['version_id']!=span.version_id:continue
                origin=value['source_start_seconds']
                if origin<=cut_left+.5/float(fps) and int(value['source']['end_pts'])*value['source']['time_base']['num']/value['source']['time_base']['den']>=cut_right-.5/float(fps):covering.append(row)
            if covering:
                first=json.loads(covering[0]['result_json'])
                corrected=any('wording_authority' in s for s in first['segments'])
                def aligned_for_cut(row):
                    value=json.loads(row['result_json']);origin=value['source_start_seconds']
                    return all(s.get('words') for s in value['segments'] if origin+s['start_seconds']<cut_right and cut_left<origin+s['end_seconds'])
                chosen=covering[0] if corrected else next((r for r in covering if aligned_for_cut(r)),covering[0])
                selected_rows=[chosen]
            else:selected_rows=rows
            for row in selected_rows:
                result=json.loads(row['result_json'])
                if result['source']['version_id']!=span.version_id:continue
                origin=result['source_start_seconds']
                for n,segment in enumerate(result['segments']):
                    left=origin+segment['start_seconds'];right=origin+segment['end_seconds']
                    identity=(item.item_id,round(left,4),round(right,4))
                    if identity in seen or right<=cut_left or left>=cut_right:continue
                    partial=left<cut_left-.5/float(fps) or right>cut_right+.5/float(fps)
                    original_text=segment['text']
                    if partial:
                        selected=[w for w in segment.get('words',[]) if origin+w['start_seconds']>=cut_left-.5/float(fps)
                                  and origin+w['end_seconds']<=cut_right+.5/float(fps)]
                        if not selected:
                            limitations.append({'item_id':item.item_id,'reason':'partial_utterance_requires_alignment','text':segment['text']})
                            continue
                        first,last=selected[0]['char_start'],selected[-1]['char_end']
                        # Retain only complete aligned words. Display punctuation
                        # belongs to the source text, never invented speech.
                        from .alignment import significant
                        while last<len(original_text) and not significant(original_text[last]):last+=1
                        segment={**segment,'text':original_text[first:last],
                                 'words':[{**w,'char_start':w['char_start']-first,'char_end':w['char_end']-first} for w in selected]}
                        left=origin+selected[0]['start_seconds'];right=origin+selected[-1]['end_seconds']
                    seen.add(identity)
                    if not include_drafts:continue
                    begin=item.start_frame+max(0,math.floor((left-cut_left)*fps))
                    end=min(item.start_frame+item.duration_frames,item.start_frame+math.ceil((right-cut_left)*fps))
                    words=[{**w,'start_frame':item.start_frame+max(0,round((origin+w['start_seconds']-cut_left)*fps)),
                            'end_frame':min(item.start_frame+item.duration_frames,item.start_frame+round((origin+w['end_seconds']-cut_left)*fps))} for w in segment.get('words',[])]
                    cues.append({'cue_id':digest([item.item_id,row['job_id'],n])[:24], 'item_id':item.item_id,
                                 'start_frame':begin,'duration_frames':end-begin,'text':segment['text'],
                                 'verification':'asr_draft','source_revision_id':row['job_id'],'segment_id':str(n),
                                 'source_utterance_partial':partial,'source_utterance_text':original_text,
                                 **{key:segment[key] for key in ('wording_authority','source_asr_text','correction_evidence') if key in segment},
                                 'timing_basis':'forced_alignment' if words else 'asr_segment','words':words,
                                 'source':{**span.to_storage(),'start_pts':str(round(left/float(unit))),'end_pts':str(round(right/float(unit)))}})
    return sorted(cues,key=lambda c:c['start_frame']),limitations
