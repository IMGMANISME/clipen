"""Cold-start production, source timing and actual graphics/audio output; no paid AI."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from clipen.analysis.transcription import TranscriptionResult, TranscriptSegment
from clipen.domain import EditTimeline, TextClip, TrackKind
from clipen.editing import EditAgentRepository, EditAgentTools
from clipen.editing.alignment import validate_alignment
from clipen.media import (AssetCandidate, AssetKind, InventoryRepository, InventoryState,
                          ItemStatus, MediaPreview, MediaStream, ProbeStatus)
from clipen.projects import DirectoryReference, DirectoryRole, ProjectError, ProjectRepository
from clipen.rendering import FfmpegPreviewRenderer
from clipen.skills import get_skill


class SkillProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.projects=ProjectRepository(self.root/'projects')
        self.project=self.projects.create_project('fresh-project','全新節目')
        source=self.root/'source';source.mkdir();info=source.stat()
        self.project=self.projects.set_directory(self.project.project_id,'grant-source','a'*64,self.project.revision,
            DirectoryReference(DirectoryRole.SOURCE,str(source),str(source),'原片',info.st_dev,info.st_ino,'2026-10-20T00:00:00+00:00'))
        inventory=InventoryRepository(self.projects)
        run=inventory.create_run(self.project.project_id,'source-inventory',self.project.revision)
        inventory.transition(self.project.project_id,run.run_id,InventoryState.RUNNING,phase='probing')
        inventory.stage_items(self.project.project_id,run.run_id,(
            AssetCandidate(DirectoryRole.SOURCE,'source.mp4',AssetKind.VIDEO,ItemStatus.USABLE,None,1024,12345,'b'*64,ProbeStatus.READY,
                           {'duration_seconds':'12'},(MediaStream(0,'video','h264',{'start_pts':'90000','duration_ts':'1080000','time_base':'1/90000','width':640,'height':360}),
                                                    MediaStream(1,'audio','aac',{'start_pts':'48000','duration_ts':'576000','time_base':'1/48000'}))),))
        inventory.publish(self.project.project_id,run.run_id)
        self.repository=EditAgentRepository(self.projects)
        self.run,_=self.repository.create_run(self.project.project_id,'create-production',production={
            'skill_id':'travel-program','intent':'用真實對話製作節目，配合字幕、音樂與資訊標註。'},max_tool_calls=150)
        self.tools=EditAgentTools(self.repository,self.project.project_id,self.run.run_id)
        self.tools.activate()
        source=self.call('list_source_media').payload['sources'][0]
        self.candidate=self.call('register_source_range',version_id=source['version_id'],stream_index=0,
                                 start_offset_seconds=0,duration_seconds=8,description='待檢視的事件範圍').payload['candidate']['candidate_id']

    def tearDown(self):
        self.tools.close_production();self.temp.cleanup()

    def call(self,name,**args):
        from clipen.editing.tool_receipts import TIMELINE_MUTATIONS
        if name in TIMELINE_MUTATIONS:
            args.setdefault('expected_revision_id',self.repository.current_revision(self.project.project_id,self.run.run_id).revision_id)
            args.setdefault('mutation_id','test-'+str(time.monotonic_ns()))
        return self.tools.call(name,args)

    def cut(self):
        self.call('set_canvas',width=640,height=360,summary='測試畫布')
        return self.call('replace_sequence',clips=[{'candidate_id':self.candidate,'start_offset_seconds':0,'duration_seconds':4},
                                                  {'candidate_id':self.candidate,'start_offset_seconds':4,'duration_seconds':4}],summary='兩個來源區間')

    def component(self, **overrides):
        args={'element_id':'dish-label','item_id':'agent-video-0001','component_id':'fact','start_offset_frames':6,
              'duration_frames':45,'text':'招牌餐點','evidence':'本測試來源資訊標註','summary':'介紹餐點'}
        args.update(overrides)
        return self.call('add_program_component',**args)

    def real_media(self):
        source=self.root/'source.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=0x203040:size=640x360:rate=30:duration=12',
                        '-f','lavfi','-i','sine=frequency=220:sample_rate=48000:duration=12','-c:v','libx264','-preset','ultrafast',
                        '-pix_fmt','yuv420p','-c:a','aac',str(source)],check=True)
        projects=self.projects
        class Media:
            def __init__(self):self.projects=projects
            def open(self,*args):return MediaPreview(os.open(source,os.O_RDONLY),source.stat().st_size,'video/mp4',(1,),(0,))
        self.tools.media=Media();self.tools.preview_renderer=FfmpegPreviewRenderer(self.tools.media)
        return source

    def test_starts_without_preparation_plan_or_timeline_and_freezes_skill(self):
        with self.projects.connect(self.project.project_id) as db:
            for table in ('preparation_runs','plan_revisions','edit_timelines'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0)
        status=self.call('get_edit_status').payload
        self.assertEqual(status['mode'],'skill_production')
        self.assertIsNone(status['base_timeline_id'])
        timeline=self.tools._timeline()
        self.assertEqual(timeline.duration_frames,0)
        self.assertEqual(EditTimeline.from_payload(timeline.to_payload()),timeline)
        self.assertNotIn('plan_id',timeline.to_payload())
        self.assertEqual(self.call('read_production_skill').payload['skill']['content_hash'],get_skill()['content_hash'])
        frozen=self.repository.production_session(self.project.project_id,self.run.run_id)['skill']
        with patch('clipen.skills.catalog.get_skill',side_effect=AssertionError('must use frozen package')):
            self.assertEqual(self.call('read_production_skill',resource='references/graphics.md').payload['content'],frozen['resources']['references/graphics.md'])
        with self.assertRaises(ProjectError):self.call('read_production_skill',resource='../../project.sqlite3')
        self.assertFalse(self.call('validate_sequence').payload['valid'])
        with self.assertRaises(ProjectError):self.call('render_preview')

    def test_start_receipt_and_revision_guard_protect_existing_work(self):
        repeated,created=self.repository.create_run(self.project.project_id,'create-production',production={
            'skill_id':'travel-program','intent':'用真實對話製作節目，配合字幕、音樂與資訊標註。'},max_tool_calls=150)
        self.assertFalse(created);self.assertEqual(repeated.run_id,self.run.run_id)
        previous=self.cut().payload['revision']['revision_id']
        self.call('set_color_grade',item_id='agent-video-0001',contrast=1.1,saturation=1,brightness=0,summary='改版')
        with self.assertRaises(ProjectError) as error:self.component(expected_revision_id=previous)
        self.assertEqual(error.exception.code,'revision_conflict')
        self.assertFalse(any(t.track_id=='program-components' for t in self.tools._timeline().tracks))

    def test_mutation_retry_replays_without_overwriting_newer_revision(self):
        self.cut()
        before=self.repository.current_revision(self.project.project_id,self.run.run_id).revision_id
        args={'item_id':'agent-video-0001','contrast':1.1,'saturation':1,'brightness':0,'summary':'同一個改動',
              'expected_revision_id':before,'mutation_id':'stable-operation'}
        first=self.call('set_color_grade',**args)
        self.component()
        latest=self.repository.current_revision(self.project.project_id,self.run.run_id).revision_id
        used=self.repository.get(self.project.project_id,self.run.run_id).tool_calls
        repeated=self.call('set_color_grade',**args)
        self.assertTrue(repeated.payload['replayed'])
        self.assertEqual(repeated.payload['revision'],first.payload['revision'])
        self.assertEqual(self.repository.current_revision(self.project.project_id,self.run.run_id).revision_id,latest)
        self.assertEqual(self.repository.get(self.project.project_id,self.run.run_id).tool_calls,used)
        with self.assertRaises(ProjectError):self.call('set_color_grade',**{**args,'contrast':1.2})
        with self.assertRaises(ProjectError) as error:
            self.tools.call('remove_program_component',{'element_id':'dish-label','summary':'缺少版本與操作識別'})
        self.assertEqual(error.exception.code,'mutation_guard_required')

    def test_mcp_publishes_required_mutation_guards_and_frozen_skill_resources(self):
        from clipen.mcp_editing import EditingMcpServer
        server=EditingMcpServer(self.tools)
        listed=server.handle({'jsonrpc':'2.0','id':1,'method':'tools/list'})['result']['tools']
        component=next(t for t in listed if t['name']=='add_program_component')
        self.assertTrue({'mutation_id','expected_revision_id'}<=set(component['inputSchema']['required']))
        response=server.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'read_production_skill','arguments':{'resource':'SKILL.md'}}})
        self.assertIn('travel-program',response['result']['content'][0]['text'])
        preview=server.handle({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'read_production_skill','arguments':{'resource':'assets/pointer.png'}}})
        self.assertEqual([item['type'] for item in preview['result']['content']],['text','image'])
        self.assertIn('style_example_not_source_evidence',preview['result']['content'][0]['text'])

    def test_v17_migration_preserves_real_legacy_revision_and_foreign_keys(self):
        import test_edit_agent as legacy
        from clipen.projects.agent_schema import EDIT_AGENT_SCHEMA
        fixture=legacy.EditAgentTests();fixture.setUp()
        try:
            run,tools=fixture.create_tools()
            original=fixture.repository.current_revision(fixture.project.project_id,run.run_id)
            tools.close_production()
            path=fixture.projects.root/fixture.project.project_id/'project.sqlite3'
            db=sqlite3.connect(path)
            create=EDIT_AGENT_SCHEMA.split('CREATE TABLE edit_agent_revisions',1)[0].replace('CREATE TABLE edit_agent_runs','CREATE TABLE runs_v17',1)
            create=create.replace('base_timeline_id TEXT REFERENCES','base_timeline_id TEXT NOT NULL REFERENCES')
            db.executescript('PRAGMA foreign_keys=OFF; BEGIN IMMEDIATE;'+create+
                'INSERT INTO runs_v17 SELECT * FROM edit_agent_runs; DROP TABLE edit_agent_runs; ALTER TABLE runs_v17 RENAME TO edit_agent_runs;'
                'CREATE INDEX edit_agent_runs_project_created ON edit_agent_runs(project_id,created_at DESC);'
                'DROP TABLE production_tool_receipts; DROP TABLE production_evidence_jobs; DROP TABLE production_sessions; PRAGMA user_version=17; COMMIT;')
            db.close()
            restored=fixture.repository.current_revision(fixture.project.project_id,run.run_id)
            self.assertEqual(restored,original)
            with fixture.projects.connect(fixture.project.project_id) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],18)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0],1)
        finally:fixture.tearDown()

    def test_authenticated_production_entry_uses_inventory_without_old_plan(self):
        import asyncio
        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from clipen.editing import EditAgentCoordinator
        from clipen.providers import ProviderCancelled
        from test_project_api import request_app
        self.repository.cancel(self.project.project_id,self.run.run_id)
        class LocalOnlyAgent:
            def __init__(self,projects_root,attempt_root,repository):self.repository=repository
            def execute(self,project_id,run_id,*,cancel):
                self.repository.activate(project_id,run_id);cancel.wait(3)
                raise ProviderCancelled('test stop')
        static=self.root/'dist';static.mkdir();(static/'index.html').write_text('Clipen')
        coordinator=EditAgentCoordinator(self.repository,self.root,adapter_factory=LocalOnlyAgent)
        app=create_app(ControllerConfig(static),ProviderStatus('Codex',ProviderState.READY,'ready'),LocalSessionAuthority(),edit_agents=coordinator)
        def request(method,path,payload=None,headers=None):
            return asyncio.run(request_app(app,method,path,headers=headers,body=b'' if payload is None else json.dumps(payload).encode()))
        try:
            _,headers,_=request('POST','/api/session/connect',headers={'origin':'http://localhost:4200','sec-fetch-site':'same-origin'})
            cookie=headers['set-cookie'].split(';',1)[0]
            _,_,body=request('GET','/api/status',headers={'cookie':cookie})
            csrf=json.loads(body)['session']['csrf_token']
            mutation={'origin':'http://localhost:4200','sec-fetch-site':'same-origin','content-type':'application/json','cookie':cookie,'x-clipen-csrf':csrf}
            payload={'request_id':'api-production','consent':False,'skill_id':'travel-program','intent':'從來源直接製作節目'}
            route=f'/api/projects/{self.project.project_id}/production/commands/start'
            self.assertEqual(request('POST',route,payload,mutation)[0],422)
            code,_,body=request('POST',route,{**payload,'consent':True},mutation)
            self.assertEqual(code,202,body)
            code,_,body=request('GET',f'/api/projects/{self.project.project_id}/edit-agent',headers={'cookie':cookie})
            self.assertEqual(code,200,body)
            self.assertEqual(json.loads(body)['production']['skill']['skill_id'],'travel-program')
        finally:coordinator.close()

    def test_graphics_follow_reorder_trim_and_undo_without_clearing_other_treatments(self):
        self.cut();self.component()
        self.call('set_color_grade',item_id='agent-video-0001',contrast=1.1,saturation=1,brightness=0,summary='曝光')
        revision=self.repository.current_revision(self.project.project_id,self.run.run_id)
        result=self.call('reorder_clips',item_ids=['agent-video-0002','agent-video-0001'],summary='重排')
        self.assertEqual(result.payload['invalidated'],[])
        label=next(i for t in self.tools._timeline().tracks if t.track_id=='program-components' for i in t.items)
        self.assertEqual(label.start_frame,126)
        self.assertEqual(self.tools._video().items[1].effects[0].effect_id,'video.grade')
        trimmed=self.call('trim_clip',item_id='agent-video-0002',start_offset_frames=30,duration_frames=90,summary='縮短別鏡')
        self.assertEqual(trimmed.payload['invalidated'],[])
        label=next(i for t in self.tools._timeline().tracks if t.track_id=='program-components' for i in t.items)
        self.assertEqual(label.start_frame,96)
        self.call('restore_revision',revision_id=revision.revision_id,summary='還原')
        self.assertEqual(self.tools._timeline(),revision.timeline)

    def test_component_safety_and_actual_layout_overflow(self):
        self.cut()
        for args in ({'start_offset_frames':110,'duration_frames':20}, {'component_id':'pointer'},
                     {'x':.9}, {'text':'很長的價格和品名資訊'*4}):
            with self.assertRaises((ProjectError,RuntimeError)):self.component(**args)
        self.component(component_id='pointer',target={'x':.7,'y':.45,'radius':.04})
        self.assertEqual(self.call('list_program_components').payload['tracking'],'static_target_only')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
    def test_actual_graphics_render_and_partial_draft_preview(self):
        self.real_media();self.cut();self.component()
        validation=self.call('validate_sequence').payload
        self.assertTrue(validation['delivery_issues'])
        self.assertEqual(validation['render_issues'],[])
        render=self.call('render_preview').payload['render']
        inspected=self.call('inspect_preview',render_id=render['render_id'],offsets_seconds=[.1,.6,2.],wait_seconds=15)
        self.assertEqual(inspected.payload['render']['state'],'succeeded',inspected.payload)
        from PIL import Image, ImageChops
        before,during,after=[Image.open(p).convert('RGB') for p in inspected.images]
        self.assertIsNotNone(ImageChops.difference(before,during).crop((30,20,300,100)).getbbox())
        # A label no longer appears after its active interval.
        self.assertLess(sum(ImageChops.difference(before,after).resize((1,1)).getpixel((0,0))),8)

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
    def test_on_demand_speech_alignment_preserves_pauses_and_excerpt_timing(self):
        self.real_media();self.cut()
        text='先等一下。真的好吃！'
        class Transcriber:
            def transcribe(self,audio,output,**kwargs):
                if not output.is_file() or output.stat().st_size!=0:
                    raise AssertionError('ASR adapter requires a reserved empty output file')
                return TranscriptionResult('zh',text,(TranscriptSegment(0,64000,text,None,()),))
            def close(self):pass
        self.tools.transcriber_binding=lambda:(Transcriber,{'id':'test-local','revision':'fixed'})
        words=[{'text':c,'start':(.4+.15*n if n<4 else 2.4+.15*(n-4)),
                'end':(.55+.15*n if n<4 else 2.55+.15*(n-4))} for n,c in enumerate('先等一下真的好吃')]
        with patch('clipen.editing.alignment.run_alignment',return_value=words):
            job=self.call('transcribe_source_range',candidate_id=self.candidate).payload['job']
            completed=self.call('read_evidence_job',job_id=job['job_id'],wait_seconds=10).payload['job']
        self.assertEqual(completed['state'],'succeeded',completed)
        self.assertEqual(completed['result']['timing_basis'],'forced_alignment')
        reused=self.call('transcribe_source_range',candidate_id=self.candidate).payload
        self.assertTrue(reused['reused'])
        cues=self.call('list_transcripts',include_drafts=True).payload['cues']
        self.assertTrue(cues)
        self.call('replace_subtitles',captions=[{'cue_id':cues[0]['cue_id'],'text':text}],allow_asr_draft=True,summary='依字詞時間建立字幕')
        captions=[i for t in self.tools._timeline().tracks if t.track_id=='agent-subtitles' for i in t.items]
        self.assertEqual(len(captions),2)
        self.assertEqual(captions[0].start_frame,12)
        self.assertEqual(captions[1].start_frame,72)
        self.assertLess(captions[0].start_frame+captions[0].duration_frames,captions[1].start_frame)
        self.call('replace_subtitles',captions=[{'cue_id':cues[0]['cue_id'],'text':'真的好吃','source_excerpt':{'start':5,'end':9}}],allow_asr_draft=True,summary='摘錄完整字詞')
        caption=next(i for t in self.tools._timeline().tracks if t.track_id=='agent-subtitles' for i in t.items)
        self.assertEqual(caption.start_frame,72)
        corrected='先等一下。真是好吃！'
        corrected_words=[{**w,'text':c} for w,c in zip(words,'先等一下真是好吃')]
        with patch('clipen.editing.alignment.run_alignment',return_value=corrected_words):
            correction=self.call('revise_source_speech',transcript_job_id=job['job_id'],segment_ordinal=0,
                                 text=corrected,evidence='測試更正提案，尚未核對原音。').payload['job']
            correction=self.call('read_evidence_job',job_id=correction['job_id'],wait_seconds=10).payload['job']
        self.assertEqual(correction['state'],'succeeded',correction)
        self.assertEqual(correction['result']['segments'][0]['source_asr_text'],text)
        issues=self.call('validate_sequence').payload['issues']
        self.assertIn('subtitle_source_changed',{issue['code'] for issue in issues})
        latest=self.call('list_transcripts',include_drafts=True).payload['cues']
        self.assertEqual(len(latest),1)
        self.assertEqual(latest[0]['text'],corrected)
        self.assertEqual(latest[0]['wording_authority'],'model_proposed_correction')
        # A new session reuses source/model evidence without requiring the old
        # analysis stage or invoking the local model a second time.
        self.repository.cancel(self.project.project_id,self.run.run_id)
        next_run,_=self.repository.create_run(self.project.project_id,'reuse-source-evidence',production={
            'skill_id':'travel-program','intent':'另一個節目製作工作'})
        following=EditAgentTools(self.repository,self.project.project_id,next_run.run_id)
        following.activate()
        def must_not_run():raise AssertionError('cached evidence must not rerun inference')
        following.transcriber_binding=lambda:(must_not_run,{'id':'test-local','revision':'fixed'})
        try:
            source=following.call('list_source_media',{}).payload['sources'][0]
            candidate=following.call('register_source_range',{'version_id':source['version_id'],'stream_index':0,
                'start_offset_seconds':0,'duration_seconds':8,'description':'相同來源'}).payload['candidate']['candidate_id']
            cached=following.call('transcribe_source_range',{'candidate_id':candidate}).payload
            self.assertTrue(cached['reused'])
            self.assertEqual(cached['job']['state'],'succeeded')
            self.assertEqual(cached['job']['result']['segments'][0]['text'],text)
        finally:following.close_production()

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
    def test_split_remaps_subtitles_and_can_read_partial_aligned_utterances(self):
        self.cut()
        from clipen.projects.storage import utc_now
        clip=self.tools._video().items[0]
        result={'source':clip.source.span.to_storage(),'source_start_seconds':1,
                'segments':[{'text':'先等。好吃！','start_seconds':0,'end_seconds':4,
                             'words':[{'text':'先等','char_start':0,'char_end':2,'start_seconds':.4,'end_seconds':.9},
                                      {'text':'好吃','char_start':3,'char_end':5,'start_seconds':2.4,'end_seconds':2.9}]}]}
        with self.projects.connect(self.project.project_id) as db:
            db.execute('INSERT INTO production_evidence_jobs VALUES(?,?,?,?,?,?,?,?,?)',
                       ('speech-split',self.run.run_id,'split-key','align','succeeded','{}',json.dumps(result),None,utc_now()))
        cue=self.call('list_transcripts',include_drafts=True).payload['cues'][0]
        self.call('replace_subtitles',captions=[{'cue_id':cue['cue_id'],'text':cue['text']}],allow_asr_draft=True,summary='來源字幕')
        self.call('split_clip',item_id=clip.item_id,offset_frames=45,summary='在停頓處分鏡')
        fresh=self.call('list_transcripts',include_drafts=True).payload['cues']
        self.assertEqual([c['text'] for c in fresh],['先等。','好吃！'])
        ids=[i.item_id for i in self.tools._video().items]
        self.call('reorder_clips',item_ids=[ids[1],ids[0],ids[2]],summary='檢查分割後再次修改')
        captions=[i for t in self.tools._timeline().tracks if t.track_id=='agent-subtitles' for i in t.items]
        self.assertEqual(sorted(i.start_frame for i in captions),[27,87])

    def test_alignment_rejects_mismatch_missing_words_and_invalid_times(self):
        for words in ([{'text':'別','start':0,'end':1}], [{'text':'好','start':1,'end':0}],
                      [{'text':'好','start':float('nan'),'end':1}], []):
            with self.assertRaises(ProjectError):validate_alignment(words,'好',2)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
    def test_actual_music_stem_ducks_only_during_aligned_speech_and_reaches_final_mix(self):
        self.real_media();self.cut();self.component()
        from clipen.finishing.audio import inspect_library
        from clipen.editing.production_media import AgentAudioStore
        from clipen.projects.storage import utc_now
        store=AgentAudioStore(self.projects,self.project.project_id)
        directory=store.directory/'test-music';directory.mkdir()
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=880:sample_rate=48000:duration=8',str(directory/'music.wav')],check=True)
        info=directory.stat()
        reference=DirectoryReference(DirectoryRole.SOURCE,str(directory),directory.name,directory.name,info.st_dev,info.st_ino,utc_now()).to_storage()
        assets,_=inspect_library('music',reference,threading.Event(),lambda _:None)
        store.add(assets,reference)
        self.call('place_audio',item_id='agent-video-0001',layer_id='music',asset_id=assets[0]['asset_id'],
                  start_frame=0,duration_frames=240,source_offset_seconds=0,gain_db=-8,duck=True,fade_seconds=0,summary='配樂測試')
        span=self.tools._video().items[0].source.span.to_storage()
        speech={'source':span,'source_start_seconds':1,'segments':[{'text':'好','start_seconds':1.,'end_seconds':2.,
                'words':[{'text':'好','char_start':0,'char_end':1,'start_seconds':1.,'end_seconds':2.}]}]}
        with self.projects.connect(self.project.project_id) as db:
            db.execute('INSERT INTO production_evidence_jobs VALUES(?,?,?,?,?,?,?,?,?)',
                       ('speech-test',self.run.run_id,'test-aligned','align','succeeded','{}',json.dumps(speech),None,utc_now()))
        render=self.call('render_preview').payload['render']
        done=self.call('inspect_preview',render_id=render['render_id'],offsets_seconds=[.6],wait_seconds=15)
        self.assertEqual(done.payload['render']['state'],'succeeded',done.payload)
        mixed=self.call('inspect_mix',render_id=render['render_id']).payload
        self.assertEqual(mixed['duck_basis'],'aligned_source_words')
        self.assertEqual(mixed['issues'],[])
        windows=mixed['measurements']['music']['window_rms_dbfs']
        self.assertAlmostEqual(windows[3]-windows[0],-7,delta=.7)
        self.assertAlmostEqual(windows[8],windows[0],delta=.5)
        self.assertFalse(mixed['audio_listened'])
        # Check the actual encoded final output, not just music metadata/stem.
        from clipen.rendering import PreviewRenderRepository
        from array import array
        import math
        output=PreviewRenderRepository(self.projects).output_path(self.project.project_id,render['render_id'],prepare=False)
        raw=subprocess.check_output(['ffmpeg','-v','error','-ss','0.5','-i',str(output),'-t','0.25','-ac','1','-ar','8000','-f','s16le','pipe:1'])
        samples=array('h');samples.frombytes(raw)
        def amplitude(frequency):
            return abs(sum(v*complex(math.cos(2*math.pi*frequency*n/8000),math.sin(2*math.pi*frequency*n/8000)) for n,v in enumerate(samples)))*2/len(samples)
        self.assertGreater(amplitude(880),300)
        self.assertGreater(amplitude(220),300)


if __name__=='__main__':unittest.main()
