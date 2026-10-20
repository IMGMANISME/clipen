import asyncio
from array import array
import copy
from contextlib import contextmanager
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
from clipen.domain import ClipSource, EditTimeline, EffectRequest, MediaClip, Rational, SourceSpan, TextClip, Track, TrackKind
from clipen.finishing.audio import detect_beats, inspect_library, open_asset, run_process
from clipen.finishing.context import build_context
from clipen.finishing.models import compile_context, digest, encoded, validate_plan
from clipen.finishing.renderer import FinishingRenderer, render_overlays
from clipen.finishing.service import FinishingService
from clipen.media.preview import MediaPreview
from clipen.projects import DirectoryRole, ProjectError, ProjectRepository
from clipen.projects.picker import inspect_directory
from clipen.providers import ProviderUsage
from test_preview_rendering import request_app


def fixture(project_id='prj_'+'a'*32):
    clips=tuple(MediaClip(f'clip-{i}',i*180,180,ClipSource(SourceSpan('av_'+'b'*32,0,'0','540000',Rational(1,90000))),
                         (EffectRequest('video.slow_push',{'scale_to':1.08}),) if i==0 else ()) for i in range(8))
    timeline=EditTimeline('timeline-fixture',project_id,'plan-fixture',1,'c'*64,Rational(30,1),640,360,1440,
                          (Track('video-main',TrackKind.VIDEO,clips),Track('title-main',TrackKind.TITLE,(TextClip('title-original',0,60,'方向','title.chapter'),))))
    context={'base_revision_id':'editrev_fixture','timeline':timeline.to_payload(),
             'cues':[{'cue_id':'cue-one','clip_id':'clip-0','start_frame':15,'duration_frames':75,'text':'我們抵達台北了','original_text':'我們抵達台北了'}],
             'shot_facts':[{'evidence_id':f'shot:clip-{i}','clip_id':f'clip-{i}','observations':[{'description':'旅行街景'}]} for i in range(8)],
             'confirmed_direction':{'brief':'旅行日記'},
             'assets':[{'asset_id':'music-one','role':'music','name':'music.wav','duration_seconds':64.,'beats_seconds':[i*.5 for i in range(128)],'rms_db':-15.,'bpm':120.,'analyzed_seconds':64, 'provenance':{'attribution':'Test music — fixture','changes':'合成測試音樂','source_url':'https://example.org/music','license':'cc0','license_url':'https://creativecommons.org/publicdomain/zero/1.0/'}}], 'directories':{}}
    plan={'schema_version':2,'style':'travel.clean.v2','summary':'從抵達到探索的旅行日記',
          'edit':[{'clip_id':f'clip-{i}','source_offset_frames':0,'duration_frames':180,'purpose':'hook' if i==0 else 'story','reason':'保留旅行事件順序','evidence_refs':[f'shot:clip-{i}']} for i in range(8)],'alternatives':[],'captions':[{'cue_id':'cue-one','text':'我們抵達台北了','emphasis':'台北'}],
          'cards':[{'start_frame':0,'duration_frames':90,'text':'台北・第一天','purpose':'location'}],
          'music':{'asset_id':'music-one','offset_seconds':0.,'gain_db':0,'reason':'來源標籤與探索旅行的內容相符','regions':[{'start_frame':0,'duration_frames':180,'gain_db':-12,'reason':'開場人聲優先'},{'start_frame':180,'duration_frames':1260,'gain_db':0,'reason':'讓配樂帶動探索段落'}]},'sound_effects':[],
          'source_audio':[{'clip_id':f'clip-{i}','mode':'dialogue' if i==0 else 'keep','reason':'保留人物口語與現場聲'} for i in range(8)],
          'transitions':[{'clip_id':'clip-1','kind':'dip_black','duration_frames':12,'reason':'切換地點'}],
          'motion':[{'clip_id':'clip-2','kind':'slow_push','amount':1.08,'x':.5,'y':.5}],
          'grade':{'contrast':1.03,'saturation':1.04,'brightness':.01},'beat_markers':[180]}
    return timeline,context,plan


class FinishingValidationTests(unittest.TestCase):
    def setUp(self): self.timeline,self.context,self.plan=fixture()

    def test_complete_plan_preserves_provenance_and_parameters(self):
        result=validate_plan(self.plan,self.context)
        self.assertEqual(result,self.plan)
        self.assertIsNot(result,self.plan)
        self.assertEqual(self.context['cues'][0]['original_text'],'我們抵達台北了')

    def test_missing_layers_wrong_sources_and_executable_content_block_completion(self):
        changes=[('captions',[]),('edit',[]),('style','shell:rm'),('grade',{'contrast':float('nan'),'saturation':1.,'brightness':0.})]
        for key,value in changes:
            with self.subTest(key=key):
                plan=copy.deepcopy(self.plan);plan[key]=value
                with self.assertRaises(ProjectError): validate_plan(plan,self.context)
        for mutate in (
            lambda p:p.update(command='ffmpeg -i /private/file'),
            lambda p:p['captions'][0].update(cue_id='foreign-cue'),
            lambda p:p['captions'][0].update(emphasis='不存在'),
            lambda p:p['music'].update(asset_id='foreign-project'),
            lambda p:p['music'].update(offset_seconds=30),
            lambda p:p.update(beat_markers=[100]),
            lambda p:p['cards'][0].update(start_frame=1400),
            lambda p:p['transitions'][0].update(clip_id='clip-0'),
            lambda p:p['motion'][0].update(clip_id='clip-0',kind='speed_ramp'),
            lambda p:p['source_audio'].append(p['source_audio'][0]),
        ):
            plan=copy.deepcopy(self.plan);mutate(plan)
            with self.assertRaises(ProjectError): validate_plan(plan,self.context)

    def test_non_speech_speed_ramp_is_allowed(self):
        self.plan['motion'][0]['kind']='speed_ramp'
        validate_plan(self.plan,self.context)

    def test_concurrent_chapter_and_location_cards_keep_their_text_and_timing(self):
        self.plan['cards']=[
            {'start_frame':735,'duration_frames':75,'text':'第三天','purpose':'chapter'},
            {'start_frame':795,'duration_frames':75,'text':'東京迪士尼','purpose':'location'},
        ]
        self.assertEqual(validate_plan(self.plan,self.context)['cards'],self.plan['cards'])

    def test_simultaneous_cards_render_as_separate_rows_above_caption_area(self):
        from PIL import Image
        self.plan['cards']=[{'start_frame':0,'duration_frames':90,'text':'第三天' if i==0 else '東京迪士尼',
                             'purpose':'chapter' if i==0 else 'location'} for i in range(6)]
        validate_plan(self.plan,self.context)
        with tempfile.TemporaryDirectory() as tmp:
            render_overlays(self.context,self.plan,self.timeline,Path(tmp),threading.Event())
            image=Image.open(Path(tmp)/'overlay-0.png').convert('RGBA')
            occupied=[y for y in range(image.height) if image.crop((0,y,image.width,y+1)).getbbox()]
            bands=1+sum(b-a>1 for a,b in zip(occupied,occupied[1:]))
            self.assertEqual(bands,6)
            self.assertLess(max(occupied),image.height*.55)

    def test_actual_recut_changes_source_and_timeline_and_remaps_dialogue(self):
        edit=self.plan['edit']
        edit[2].update(source_offset_frames=15,duration_frames=150)
        self.plan['edit']=[edit[2],edit[1],edit[0],*edit[3:]]
        self.plan['beat_markers']=[150]
        self.plan['music']['regions']=[{'start_frame':0,'duration_frames':1410,'gain_db':0,'reason':'測試重新對齊後的配樂區段'}]
        validate_plan(self.plan,self.context)
        compiled=compile_context(self.context,self.plan)
        self.assertEqual(compiled['timeline']['duration_frames'],1410)
        first=compiled['timeline']['tracks'][0]['items'][0]
        self.assertEqual(first['item_id'],'clip-2');self.assertEqual(first['source']['start_pts'],'45000')
        self.assertEqual(compiled['cues'][0]['start_frame'],345)
        self.assertEqual(self.context['timeline']['duration_frames'],1440)

    def test_pure_ambient_film_does_not_need_checklist_effects_or_fake_subtitles(self):
        self.plan['source_audio'][0]['mode']='mute'
        self.plan.update(captions=[],cards=[],motion=[],transitions=[],sound_effects=[],beat_markers=[])
        validate_plan(self.plan,self.context)

    def test_recut_cannot_truncate_retained_speech_or_invent_evidence(self):
        self.plan['edit'][0]['source_offset_frames']=30
        self.plan['edit'][0]['duration_frames']=150
        with self.assertRaises(ProjectError):validate_plan(self.plan,self.context)
        self.plan['edit'][0].update(source_offset_frames=0,duration_frames=180,evidence_refs=['invented'])
        with self.assertRaises(ProjectError):validate_plan(self.plan,self.context)

    def test_flat_audio_does_not_get_fabricated_beats(self):
        beats,bpm=detect_beats(array('h',[0]*8000))
        self.assertEqual(beats,[]);self.assertIsNone(bpm)

    def test_subprocess_can_cancel_without_waiting_for_stdout(self):
        import sys
        token=threading.Event();timer=threading.Timer(.1,token.set);timer.start()
        started=time.monotonic()
        with self.assertRaises(ProjectError) as caught:
            run_process([sys.executable,'-c','import time; time.sleep(20)'],token,timeout=25)
        timer.join();self.assertEqual(caught.exception.code,'user_cancelled')
        self.assertLess(time.monotonic()-started,3)


class FinishingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.projects=ProjectRepository(self.root/'projects');self.project=self.projects.create_project('request-create','旅行')
        self.attempts=self.root/'attempts';self.attempts.mkdir()
        self.timeline,self.context,self.plan=fixture(self.project.project_id)
        self.brief={'intent':'探索旅行的節奏','story_arc':'抵達、探索、結尾','music_tags':['travel','acoustic'],
                    'music_reason':'讓街景帶出輕鬆探索感','preserve':[]}
        self.builder=patch('clipen.finishing.service.build_context',return_value=self.context);self.builder.start()
        self.calls=[];self.fail_music=False;self.rendered=[]
        test=self
        class Catalog:
            def search_and_prepare(self,brief,directory,cancel,progress):
                test.assertEqual(brief['music_tags'],['travel','acoustic'])
                if test.fail_music:raise ProjectError('finishing_catalog_unavailable','搜尋暫時無法使用')
                return copy.deepcopy(test.context['assets']),{}
        class Renderer:
            def render(self,project_id,context,plan,output,cancel,progress):
                test.rendered.append(copy.deepcopy(plan));output.write_bytes(b'tested renderer output');progress(1)
        self.service=FinishingService(self.projects,None,None,self.attempts,adapter_factory=self.adapter,
                                     renderer=Renderer(),catalog=Catalog(),snapshotter=lambda *args:((),[]))

    def tearDown(self):
        self.service.close();self.builder.stop();self.tmp.cleanup()

    def adapter(self,root):
        test=self
        class Adapter:
            def probe(self):return SimpleNamespace(available=True)
            def execute(self,attempt,cancel):
                test.calls.append(attempt)
                payload=test.brief if attempt.attempt_id.endswith('-brief') else test.plan
                return SimpleNamespace(payload=copy.deepcopy(payload),usage=ProviderUsage(100,20,30))
        return Adapter()

    def wait(self,run):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            current=self.service.get(self.project.project_id,run['run_id'])
            if current['state'] not in {'queued','running','cancelling'}:
                with self.service._lock:job=self.service._jobs.get((self.project.project_id,run['run_id']))
                if job:job[1].join(2)
                return current
            time.sleep(.01)
        self.fail('finishing did not finish')

    def produce(self,request='request-produce',feedback=''):
        return self.service.start(self.project.project_id,request,'produce',consent=True,direction_confirmed=True,
                                  context_hash=digest(self.context),feedback=feedback)

    def test_no_music_directory_or_manual_settings_required_and_two_bounded_calls(self):
        status=self.service.status(self.project.project_id)
        self.assertIsNotNone(status['preflight']);self.assertEqual(status['preflight']['maximum_ai_calls'],2)
        result=self.wait(self.produce())
        self.assertEqual(result['state'],'succeeded');self.assertEqual(len(self.calls),2)
        self.assertEqual(result['usage']['input_tokens'],200);self.assertTrue(result['usage']['complete'])
        self.assertEqual(len(self.rendered),1)
        self.assertIn('real story',self.calls[0].prompt)
        self.assertIn('Re-cut/reorder',self.calls[1].prompt)
        self.assertEqual(result['result']['music']['provenance']['license'],'cc0')
        self.assertEqual(self.service.media_path(self.project.project_id,result['run_id']).read_bytes(),b'tested renderer output')
        self.assertFalse(self.service.status(self.project.project_id)['result']['stale'])

    def test_consent_and_direction_confirmation_and_stale_context(self):
        with self.assertRaises(ProjectError) as caught:
            self.service.start(self.project.project_id,'request-no-consent',context_hash=digest(self.context))
        self.assertEqual(caught.exception.code,'finishing_consent_required')
        with self.assertRaises(ProjectError):self.service.start(self.project.project_id,'request-no-direction',consent=True,context_hash=digest(self.context))
        with self.assertRaises(ProjectError):self.service.start(self.project.project_id,'request-stale',consent=True,direction_confirmed=True,context_hash='stale')
        self.assertFalse(self.calls)

    def test_idempotency_never_replays_paid_attempt(self):
        run=self.wait(self.produce())
        self.assertEqual(self.produce()['run_id'],run['run_id']);self.assertEqual(len(self.calls),2)
        with self.assertRaises(ProjectError):self.produce(feedback='改快一點')

    def test_feedback_and_previous_decisions_reach_both_ai_stages(self):
        self.wait(self.produce())
        self.wait(self.produce('request-feedback','保留更多街上的聲音'))
        self.assertIn('保留更多街上的聲音',self.calls[2].prompt)
        self.assertIn('previous_plan',self.calls[3].prompt)
        self.assertIn(self.plan['summary'],self.calls[3].prompt)

    def test_invalid_ai_decisions_keep_previous_result_and_actual_usage(self):
        first=self.wait(self.produce())
        self.plan['captions']=[]
        failed=self.wait(self.produce('request-invalid'))
        self.assertEqual(failed['state'],'failed');self.assertEqual(failed['error_code'],'finishing_invalid')
        self.assertEqual(failed['usage']['output_tokens'],60)
        self.assertEqual(self.service.status(self.project.project_id)['result']['run_id'],first['run_id'])
        self.assertEqual(len(self.rendered),1)

    def test_catalog_failure_is_explicit_and_does_not_silently_become_manual_music(self):
        self.fail_music=True
        failed=self.wait(self.produce())
        self.assertEqual(failed['error_code'],'finishing_catalog_unavailable')
        self.assertEqual(len(self.calls),1);self.assertFalse(failed['usage']['complete'])
        self.assertFalse(self.rendered)

    def test_failed_local_render_resumes_saved_decisions_without_ai_or_music_search(self):
        renderer=self.service.renderer
        self.service.renderer=SimpleNamespace(render=lambda *args:(_ for _ in ()).throw(ProjectError('finishing_render_failed','disk unavailable')))
        failed=self.wait(self.produce())
        self.assertEqual(failed['state'],'failed');self.assertTrue(failed['local_resume_available'])
        with self.projects.connect(self.project.project_id) as db:
            saved=json.loads(db.execute('SELECT context_json FROM finishing_runs WHERE run_id=?',(failed['run_id'],)).fetchone()[0])
        self.assertEqual(saved['raw_decisions'],self.plan)
        self.assertEqual(saved['production_context']['assets'],self.context['assets'])
        self.service.renderer=renderer;self.fail_music=True
        with patch.object(self.service,'adapter_factory',side_effect=AssertionError('must not call AI')):
            result=self.wait(self.service.resume(self.project.project_id,failed['run_id']))
        self.assertEqual(result['state'],'succeeded');self.assertEqual(len(self.calls),2)
        self.assertEqual(result['usage'],failed['usage']);self.assertEqual(len(self.rendered),1)
        self.assertEqual(self.service.resume(self.project.project_id,failed['run_id'])['state'],'succeeded')

    def test_local_retry_refuses_changed_context_and_missing_paid_decisions(self):
        self.plan['captions']=[]
        failed=self.wait(self.produce())
        self.assertTrue(failed['local_resume_available'])
        self.context['confirmed_direction']['brief']='新的方向'
        with self.assertRaises(ProjectError) as caught:self.service.resume(self.project.project_id,failed['run_id'])
        self.assertEqual(caught.exception.code,'finishing_context_changed');self.assertEqual(len(self.calls),2)
        self.fail_music=True
        failed=self.wait(self.produce('no-cached-edit'))
        self.assertFalse(failed['local_resume_available'])
        with self.assertRaises(ProjectError) as caught:self.service.resume(self.project.project_id,failed['run_id'])
        self.assertEqual(caught.exception.code,'finishing_resume_unavailable')

    def test_worker_launch_failure_releases_slot_and_does_not_spend_quota(self):
        with patch.object(self.service,'_execute',side_effect=ProjectError('storage_corrupt','unavailable')):
            failed=self.wait(self.produce())
        self.assertEqual(failed['state'],'failed')
        self.assertFalse(self.service._jobs);self.assertFalse(self.calls)

    def test_cancelled_render_cleans_partial_and_keeps_usage(self):
        started=threading.Event()
        class Blocking:
            def render(self,project_id,context,plan,output,cancel,progress):
                output.write_bytes(b'partial');started.set();cancel.wait(3)
                if cancel.is_set():raise ProjectError('user_cancelled','cancelled')
        self.service.renderer=Blocking()
        run=self.produce();self.assertTrue(started.wait(2))
        self.service.cancel(self.project.project_id,run['run_id'])
        failed=self.wait(run)
        self.assertEqual(failed['state'],'cancelled');self.assertEqual(failed['usage']['output_tokens'],60)
        self.assertIsNone(failed['playback_url'])
        self.assertEqual(list((self.projects.root/self.project.project_id/'finishing').glob('*.mp4')),[])

    def test_recovery_never_replays_and_adopts_known_stage_receipt(self):
        with self.projects.connect(self.project.project_id) as db:
            db.execute("INSERT INTO finishing_runs(run_id,request_id,request_hash,kind,state,phase,context_json,created_at) VALUES('finish_recover','request-recover','hash','produce','running','editing','{}','now')")
        (self.attempts/'finish_recover-brief').mkdir()
        with patch('clipen.finishing.service.AttemptReceiptStore.read',return_value=SimpleNamespace(usage=ProviderUsage(250,10,70))):self.service.recover()
        result=self.service.get(self.project.project_id,'finish_recover')
        self.assertEqual(result['state'],'interrupted');self.assertEqual(result['usage']['output_tokens'],70)
        self.assertFalse(result['usage']['complete']);self.assertFalse(self.calls)

    def test_api_guards_and_legacy_commands_are_rejected(self):
        static=self.root/'static';static.mkdir();(static/'index.html').write_text('test')
        authority=LocalSessionAuthority();credentials=authority.connect_local_browser()
        app=create_app(ControllerConfig(static),provider=ProviderStatus('Codex',ProviderState.READY,'ready'),authority=authority,finishing=self.service)
        url=f'/api/projects/{self.project.project_id}/finishing'
        code,_,_=asyncio.run(request_app(app,'GET',url));self.assertEqual(code,401)
        headers={'cookie':f'clipen_session={credentials.session_token}','origin':'http://localhost:4200','sec-fetch-site':'same-origin','content-type':'application/json'}
        code,_,_=asyncio.run(request_app(app,'POST',url+'/commands/produce',headers=headers,body=b'{}'));self.assertEqual(code,403)
        headers['x-clipen-csrf']=credentials.csrf_token
        code,_,body=asyncio.run(request_app(app,'POST',url+'/commands/produce',headers=headers,
            body=json.dumps({'request_id':'request-api','context_hash':digest(self.context),'consent':False,'direction_confirmed':True}).encode()))
        self.assertEqual(code,422);self.assertEqual(json.loads(body)['error']['code'],'finishing_consent_required')
        code,_,_=asyncio.run(request_app(app,'POST',url+'/commands/plan',headers=headers,body=b'{}'));self.assertEqual(code,409)
        renderer=self.service.renderer
        self.service.renderer=SimpleNamespace(render=lambda *args:(_ for _ in ()).throw(ProjectError('finishing_render_failed','disk unavailable')))
        failed=self.wait(self.produce('request-api-local-failure'))
        self.service.renderer=renderer
        resume_url=url+'/'+failed['run_id']+'/commands/resume'
        code,_,_=asyncio.run(request_app(app,'POST',resume_url,body=b'{}'));self.assertEqual(code,401)
        code,_,body=asyncio.run(request_app(app,'POST',resume_url,headers=headers,body=b'{}'))
        self.assertEqual(code,202);self.assertEqual(self.wait(json.loads(body))['state'],'succeeded')
        self.assertEqual(len(self.calls),2)

    def test_v16_migration_keeps_legacy_experiment_without_adopting_it(self):
        database=self.projects.root/self.project.project_id/'project.sqlite3'
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO finishing_runs(run_id,request_id,request_hash,kind,state,phase,context_json,result_json,created_at) VALUES('old','request-old','hash','render','succeeded','completed','{}','{}','now')")
            db.execute('PRAGMA user_version=16')
        self.assertEqual(self.projects.get_project(self.project.project_id),self.project)
        with self.projects.connect(self.project.project_id) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],18)
        status=self.service.status(self.project.project_id)
        self.assertIsNone(status['result']);self.assertTrue(status['has_legacy_experiment'])


class FinishingEvidenceMappingTests(unittest.TestCase):
    def setUp(self):
        self.timeline,_,_=fixture()
        video=self.timeline.tracks[0]
        self.timeline=replace(self.timeline,tracks=(replace(video,items=tuple(replace(c,source=ClipSource(SourceSpan(c.source.span.version_id,0,'90000','630000',Rational(1,90000)))) for c in video.items)),self.timeline.tracks[1]))
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript('''
            CREATE TABLE current_plan(slot,plan_id,revision);
            CREATE TABLE plan_revisions(plan_id,revision,status,content_hash);
            CREATE TABLE media_streams(version_id,stream_index,kind,details_json);
            CREATE TABLE transcription_revisions(revision_id,version_id,stream_index,status,source_span_json);
            CREATE TABLE preparation_items(transcript_revision_id,run_id,review_required);
            CREATE TABLE current_preparation(run_id);
            CREATE TABLE transcript_annotations(version_id,stream_index,source_revision_id,discarded,corrections_json,confirmed);
            CREATE TABLE transcript_segments(revision_id,segment_id,start_pts,end_pts,text,ordinal,confidence);
        ''')
        self.db.execute("INSERT INTO current_plan VALUES(1,?,1)",(self.timeline.plan_id,))
        self.db.execute("INSERT INTO plan_revisions VALUES(?,1,'confirmed',?)",(self.timeline.plan_id,self.timeline.plan_content_hash))
        version='av_'+'b'*32
        self.db.execute("INSERT INTO media_streams VALUES(?,1,'audio','{}')",(version,))
        self.db.execute("INSERT INTO media_streams VALUES(?,0,'video','{}')",(version,))
        self.db.execute("INSERT INTO transcription_revisions VALUES('transcript-one',?,1,'transcribed',?)",(version,encoded({'time_base':{'num':1,'den':48000}})))
        self.db.execute("INSERT INTO preparation_items VALUES('transcript-one','prep-one',0)")
        self.db.execute("INSERT INTO current_preparation VALUES('prep-one')")
        self.db.execute("INSERT INTO transcript_segments VALUES('transcript-one','segment-one','72000','192000','原始口語',0,'0.7')")
        self.db.execute("INSERT INTO transcript_annotations VALUES(?,1,'transcript-one',0,?,1)",(version,encoded([{'ordinal':0,'text':'我們抵達臺北了'}])))
        db=self.db
        class Projects:
            @contextmanager
            def connect(self,project_id):yield db
        self.projects=Projects()
        self.creative=patch('clipen.finishing.context.load_creative_context',return_value=({'brief':'旅行'},[]));self.creative.start()
        self.agents=SimpleNamespace(latest_successful=lambda _:SimpleNamespace(run_id='run'),final_revision=lambda *_:SimpleNamespace(revision_id='editrev-one',timeline=self.timeline))
        self.library={'music':{'directory':{},'assets':[{'role':'music','duration_seconds':64,'beats_seconds':[6.]}]}}

    def tearDown(self):self.creative.stop();self.db.close()

    def test_nonzero_source_pts_audio_timebase_and_human_corrections(self):
        context=build_context(self.projects,self.agents,self.timeline.project_id,self.library)
        self.assertEqual(len(context['cues']),8)
        first,second=context['cues'][:2]
        self.assertEqual((first['start_frame'],first['duration_frames']),(15,75))
        self.assertEqual(second['start_frame'],195)
        self.assertEqual(first['text'],'我們抵達臺北了');self.assertEqual(first['original_text'],'原始口語')
        self.assertEqual(first['source_span']['time_base'],{'num':1,'den':48000})
        self.assertEqual(context['audio_streams']['clip-0'],1)

    def test_cut_off_speech_is_exposed_for_ai_to_exclude_or_mute(self):
        self.db.execute("UPDATE transcript_segments SET end_pts='400000'")
        context=build_context(self.projects,self.agents,self.timeline.project_id,self.library)
        self.assertFalse(context['cues'][0]['complete'])

    def test_discarded_speech_and_stale_direction_cannot_finish(self):
        self.db.execute('UPDATE transcript_annotations SET discarded=1')
        context=build_context(self.projects,self.agents,self.timeline.project_id,self.library)
        self.assertEqual(context['cues'],[])
        self.db.execute("UPDATE plan_revisions SET status='superseded'")
        with self.assertRaises(ProjectError) as error:build_context(self.projects,self.agents,self.timeline.project_id,self.library)
        self.assertEqual(error.exception.code,'finishing_direction_stale')

    def test_unreviewed_low_confidence_speech_is_not_sent_as_trusted_caption_text(self):
        self.db.execute('UPDATE preparation_items SET review_required=1')
        self.db.execute('UPDATE transcript_annotations SET confirmed=0')
        context=build_context(self.projects,self.agents,self.timeline.project_id,self.library)
        self.assertEqual(context['cues'],[])
        self.assertEqual(context['speech_limitations'][0]['reason'],'transcript_needs_review')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class RealFinishingRenderTests(unittest.TestCase):
    def test_real_music_inspection_mix_cjk_animation_transitions_motion_and_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);managed=root/'managed';managed.mkdir();music_root=root/'music';music_root.mkdir()
            # A real PCM music fixture with observed half-second rhythmic onsets.
            with wave.open(str(music_root/'rhythm.wav'),'wb') as out:
                out.setparams((1,2,8000,0,'NONE','not compressed'))
                samples=array('h',(int((.5 if (i%4000)<400 else .08)*12000*math.sin(2*math.pi*220*i/8000)) for i in range(64*8000)))
                out.writeframes(samples.tobytes())
            ref=inspect_directory(music_root,DirectoryRole.SOURCE,managed).to_storage()
            assets,issues=inspect_library('music',ref,threading.Event(),lambda _:None)
            self.assertFalse(issues);self.assertEqual(len(assets),1)
            self.assertTrue(assets[0]['beats_seconds'])
            timeline,context,plan=fixture()
            assets[0]['provenance']=context['assets'][0]['provenance']
            context['assets']=assets;context['directories']={'music':ref}
            # A supplied SFX uses its own identity and independent timeline span.
            sfx=copy.deepcopy(assets[0]);sfx.update(asset_id='sfx-one',role='sfx')
            context['assets'].append(sfx);context['directories']['sfx']=ref
            plan['sound_effects']=[{'asset_id':'sfx-one','start_frame':180,'duration_frames':15,'gain_db':-9,'reason':'標記抵達下一站'}]
            plan['music']['asset_id']=assets[0]['asset_id']
            # Align the second cut to an actually detected beat.
            beat=next(b for b in assets[0]['beats_seconds'] if b>=6)
            plan['music']['offset_seconds']=beat-6
            plan['motion']=[{'clip_id':f'clip-{i+2}','kind':kind,'amount':1.08,'x':.5,'y':.5} for i,kind in enumerate(('punch_in','slow_push','reframe','speed_ramp'))]
            plan['source_audio'][6]['mode']='mute'
            plan['transitions'].append({'clip_id':'clip-7','kind':'dip_white','duration_frames':12,'reason':'抵達下一站'})
            source=root/'source.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=640x360:rate=30:duration=6','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=6','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac',str(source)],check=True)
            class Media:
                def open(self,project_id,version_id):return MediaPreview(os.open(source,os.O_RDONLY),source.stat().st_size,'video/mp4',(1,),(0,))
            output=root/'finished.mp4';progress=[]
            FinishingRenderer(Media()).render(timeline.project_id,context,plan,output,threading.Event(),progress.append)
            self.assertGreater(output.stat().st_size,10000);self.assertEqual(progress[-1],1.)
            metadata=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(output)]))
            self.assertAlmostEqual(float(metadata['streams'][0]['duration']),48.,places=1)
            # Verify the subtitle region actually changes when the caption is on screen.
            original=run_process(['ffmpeg','-v','error','-ss','1','-i',str(source),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],threading.Event())
            finished=run_process(['ffmpeg','-v','error','-ss','1','-i',str(output),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],threading.Event())
            self.assertNotEqual(original[640*3*250:640*3*315],finished[640*3*250:640*3*315])
            # Inspect the overlay itself so a grade change cannot masquerade as a caption.
            from PIL import Image
            overlays=root/'overlay-check';overlays.mkdir()
            render_overlays(context,plan,timeline,overlays,threading.Event())
            caption=Image.open(overlays/'overlay-9.png').convert('RGBA').crop((0,250,640,320))
            pixels=list(caption.get_flattened_data())
            self.assertGreater(sum(1 for r,g,b,a in pixels if r>220 and 160<g<240 and b<130 and a>200),20)
            # Changed audio is rejected before a later render can reuse the old snapshot.
            with (music_root/'rhythm.wav').open('ab') as out:out.write(b'changed')
            with self.assertRaises(ProjectError):open_asset(context,assets[0]['asset_id'])


if __name__=='__main__':unittest.main()
