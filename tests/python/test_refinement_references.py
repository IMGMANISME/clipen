"""User facts, story beats and sound guidance survive incremental refinements."""
from dataclasses import replace
import json
import unittest
import test_skill_production as fixtures
from clipen.domain import TrackKind
from clipen.editing import EditAgentRepository, EditAgentTools
from clipen.editing.refinement_references import update, public_state
from clipen.editing.workbench import state, tools_for


def edit(repository, project, run, name, arguments, expected, mutation_id):
    # Stand-in for an AI revision made between review rounds.
    tools=tools_for(repository,project,run)
    try:return tools.call(name,{**arguments,'expected_revision_id':expected,'mutation_id':mutation_id}).payload
    finally:tools.close_production()
from clipen.editing.prompt_history import history
from clipen.projects import ProjectError


class RefinementReferenceTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SkillProductionTests();self.f.setUp();self.f.cut()
        self.repository=self.f.repository;self.project=self.f.project.project_id;self.run=self.f.run.run_id
        self.repository.cancel(self.project,self.run)
    def tearDown(self): self.f.tearDown()
    def current(self): return self.repository.current_revision(self.project,self.run)
    def note(self,**values):
        return {'reference_id':'user-location','kind':'location','item_id':'agent-video-0001','start_offset_frames':15,'duration_frames':45,'note':'這裡是銀座的店家，不是淺草。',**values}
    def save(self,reference=None,version=0,mutation='save-reference'):
        return update(self.repository,self.project,self.run,self.current().revision_id,version,mutation,reference or self.note())

    def test_durable_source_anchored_reference_and_idempotency_do_not_edit_film(self):
        before=self.current();run=self.repository.get(self.project,self.run)
        saved=self.save();self.assertEqual(saved['version'],1)
        note=saved['items'][0];self.assertEqual(note['current_range']['start_frame'],15)
        self.assertEqual(note['source_range']['start_pts'],'135000')
        self.assertEqual(note['source_range']['end_pts'],'270000')
        self.assertEqual(self.save(),saved)
        self.assertEqual(state(EditAgentRepository(self.f.projects),self.project,self.run)['references'],saved)
        self.assertEqual(self.current(),before);self.assertEqual(self.repository.get(self.project,self.run),run)
        with self.assertRaises(ProjectError) as e:self.save(self.note(note='另一家店'))
        self.assertEqual(e.exception.code,'request_reused')
        with self.assertRaises(ProjectError) as e:self.save(version=0,mutation='stale-note-version')
        self.assertEqual(e.exception.code,'reference_conflict')
        updated=self.save(self.note(note='確切店名已補上'),version=1,mutation='edit-note')
        self.assertEqual(len(updated['items']),1)
        result=update(self.repository,self.project,self.run,self.current().revision_id,2,'delete-note',delete_id='user-location')
        self.assertEqual(result,{'version':3,'items':[]})

    def test_invalid_notes_cannot_target_other_media_or_invalid_ranges(self):
        invalid=[{'item_id':'other-project-item'},{'start_offset_frames':-1},{'duration_frames':0},{'duration_frames':999},{'start_offset_frames':True},{'kind':'guess'}, {'note':' '},{'source_gain_db':-12},{'kind':'audio','source_gain_db':float('nan')},{'kind':'audio','source_gain_db':1},{'kind':'audio','fill_music':'yes'},{'kind':'audio','audio_layer_id':'unrelated-layer'}]
        for i,fields in enumerate(invalid):
            with self.subTest(fields=fields),self.assertRaises(ProjectError): self.save(self.note(**fields),mutation=f'bad-{i}')
        self.assertEqual(state(self.repository,self.project,self.run)['references']['version'],0)

    def test_review_notes_mark_a_moment_and_can_point_at_a_caption(self):
        from clipen.domain import Track, TextClip
        from clipen.editing.refinement_references import normalize
        before=self.current();clip=before.timeline.tracks[0].items[0]
        saved=self.save(self.note(reference_id='user-remove',kind='remove',note='',start_offset_frames=0,duration_frames=clip.duration_frames,at_offset_frames=20))
        note=saved['items'][0]
        self.assertEqual((note['note'],note['at_offset_frames'],note['explicit_range']),('',20,False))
        self.assertEqual(note['current_range']['at_frame'],clip.start_frame+20)
        caption=TextClip('agent-caption-0001',30,30,'這段文字可以','title.caption')
        timeline=replace(before.timeline,tracks=before.timeline.tracks+(Track('agent-subtitles',TrackKind.TITLE,(caption,)),))
        revision=replace(before,timeline=timeline)
        kept=normalize(self.note(reference_id='user-keep',kind='keep',note='',start_offset_frames=40,duration_frames=1,element_id=caption.item_id),revision)
        self.assertEqual((kept['element_id'],kept['element_track'],kept['element_text']),(caption.item_id,'agent-subtitles','這段文字可以'))
        self.assertTrue(public_state({'user_references':{'version':1,'items':[kept]}},timeline)['items'][0]['current_range']['element_present'])
        self.assertFalse(public_state({'user_references':{'version':1,'items':[kept]}},before.timeline)['items'][0]['current_range']['element_present'])
        bad=[{'kind':'change','note':''},{'at_offset_frames':99},{'element_id':'missing'},{'element_id':caption.item_id,'start_offset_frames':90,'duration_frames':10},{'explicit_range':'yes'}]
        for fields in bad:
            with self.subTest(fields=fields),self.assertRaises(ProjectError):normalize(self.note(**fields),revision)

    def test_reordering_updates_time_but_trimming_requires_reconfirmation(self):
        self.save()
        before=self.current()
        edit(self.repository,self.project,self.run,'reorder_clips',{'item_ids':['agent-video-0002','agent-video-0001'],'summary':'重排'},before.revision_id,'reorder')
        moved=state(self.repository,self.project,self.run)['references']['items'][0]
        self.assertEqual(moved['current_range']['start_frame'],135)
        edit(self.repository,self.project,self.run,'trim_clip',{'item_id':'agent-video-0001','start_offset_frames':30,'duration_frames':60,'summary':'裁切'},self.current().revision_id,'trim')
        stale=state(self.repository,self.project,self.run)['references']['items'][0]
        self.assertIsNone(stale['current_range']);self.assertEqual(stale['source_range'],moved['source_range'])

    def test_refinement_freezes_refs_for_agent_and_prompt_history(self):
        self.save()
        self.save(self.note(reference_id='user-punchline',kind='highlight',note='第一口之後的反應是爆點，請留鋪陳。'),version=1,mutation='punchline')
        self.save(self.note(reference_id='user-audio',kind='audio',note='風聲太大，壓低原音並補滿配樂。',source_gain_db=-18,fill_music=True),version=2,mutation='audio-reference')
        before=self.current()
        with self.assertRaises(ProjectError) as error:self.repository.request_refinement(self.project,self.run,'stale-reference',before.revision_id,'依註記修訂',expected_references_version=2)
        self.assertEqual(error.exception.code,'reference_conflict')
        self.repository.request_refinement(self.project,self.run,'refine-with-refs',before.revision_id,'依註記修訂',expected_references_version=3)
        tools=EditAgentTools(self.repository,self.project,self.run);tools.activate()
        try:
            intent=tools.call('get_edit_status',{}).payload['intent']
            snapshot=intent['latest_revision_request']['references']
            self.assertEqual(len(snapshot['items']),3)
            self.assertEqual(snapshot['items'][2]['source_gain_db'],-18)
            tools.call('set_source_audio',{'item_id':'agent-video-0001','mode':'keep','gain_db':-18,'summary':'依使用者註記降低原音','expected_revision_id':before.revision_id,'mutation_id':'agent-audio-ref'})
            after=self.current()
            source=next(i for t in after.timeline.tracks if t.kind is TrackKind.VIDEO for i in t.items if i.item_id=='agent-video-0001')
            self.assertEqual(next(e for e in source.effects if e.effect_id=='audio.source').parameters['gain_db'],-18)
            self.assertEqual(next(i for t in before.timeline.tracks for i in t.items if i.item_id=='agent-video-0002'),next(i for t in after.timeline.tracks for i in t.items if i.item_id=='agent-video-0002'))
        finally:tools.close_production()
        rows=history(self.repository,self.project)['prompts']
        recorded=next(row for row in rows if row['kind']=='refinement')
        self.assertEqual(recorded['references'],snapshot)
        with self.assertRaises(ProjectError):self.save(version=4,mutation='during-ai')
        self.repository.cancel(self.project,self.run)
        # The round is closed: only the location fact stays for the next one.
        listed=state(self.repository,self.project,self.run)
        self.assertEqual([item['kind'] for item in listed['references']['items']],['location'])
        self.assertEqual(listed['references']['version'],4)
        self.assertEqual(len(listed['sent_notes']['items']),3);self.assertEqual(listed['sent_notes']['instruction'],'依註記修訂')
        self.save(self.note(note='下一輪的新位置'),version=4,mutation='next-round-note')
        self.assertEqual(history(self.repository,self.project)['prompts'][0]['references'],snapshot)

    def test_audio_layer_must_belong_to_the_selected_clip(self):
        before=self.current();video=before.timeline.tracks[0].items[0]
        from clipen.domain import EffectRequest
        layer={'layer_id':'existing-music','asset_id':'music-asset','gain_db':-6}
        video=replace(video,effects=video.effects+(EffectRequest('audio.layers',{'layers':[layer]}),))
        timeline=replace(before.timeline,tracks=(replace(before.timeline.tracks[0],items=(video,)+before.timeline.tracks[0].items[1:]),)+before.timeline.tracks[1:])
        revision=replace(before,timeline=timeline)
        from clipen.editing.refinement_references import normalize
        value=normalize(self.note(kind='audio',audio_layer_id='existing-music',source_gain_db=-20),revision)
        self.assertEqual(value['audio_layer_id'],'existing-music')
        self.assertEqual(public_state({'user_references':{'version':1,'items':[value]}},timeline)['items'][0]['current_range']['item_id'],'agent-video-0001')
        self.assertIsNone(public_state({'user_references':{'version':1,'items':[value]}},before.timeline)['items'][0]['current_range'])

    def test_native_render_lowers_camera_audio_and_can_fill_music_with_headroom(self):
        import math, subprocess, threading
        from array import array
        from clipen.editing.workbench import import_music
        from clipen.editing.program_audio import mix_program_audio
        self.f.real_media()
        self.f.tools.preview_renderer._BATCH_SIZE=1
        original=self.f.root/'original.mp4'
        self.f.tools.preview_renderer.render(self.project,self.current().timeline,original,threading.Event(),lambda _:None)
        edit(self.repository,self.project,self.run,'set_source_audio',{'item_id':'agent-video-0001','mode':'keep','gain_db':-18,'summary':'降低風聲'},self.current().revision_id,'attenuate-camera')
        lowered=self.f.root/'lowered.mp4'
        self.f.tools.preview_renderer.render(self.project,replace(self.current().timeline,production_session_id=None,plan_id='legacy-plan',plan_revision=1,plan_content_hash='a'*64),lowered,threading.Event(),lambda _:None)
        def rms(path,start):
            data=subprocess.run(['ffmpeg','-v','error','-ss',str(start),'-i',str(path),'-t','2.5','-vn','-ac','1','-ar','48000','-f','f32le','-'],check=True,capture_output=True).stdout
            samples=array('f');samples.frombytes(data)
            return math.sqrt(sum(value*value for value in samples)/len(samples))
        self.assertAlmostEqual(20*math.log10(rms(lowered,.5)/rms(original,.5)),-18,delta=.4)
        self.assertAlmostEqual(20*math.log10(rms(lowered,4.5)/rms(original,4.5)),0,delta=.2)
        from clipen.editing.production_media import AgentAudioStore
        directory=AgentAudioStore(self.f.projects,self.project).directory/'test-upload';directory.mkdir();music=directory/'fill.wav'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=9','-y',str(music)],check=True,capture_output=True)
        asset=import_music(self.repository,self.project,music,'自己的背景音樂.wav')
        edit(self.repository,self.project,self.run,'place_audio',{'item_id':'agent-video-0001','layer_id':'continuous-user-track','asset_id':asset['asset_id'],'start_frame':0,'duration_frames':240,'source_offset_seconds':0,'gain_db':-5,'duck':True,'fade_seconds':.1,'summary':'讓配樂補滿影片'},self.current().revision_id,'fill-music')
        mixed=self.f.root/'mixed.mp4'
        mix_program_audio(self.f.tools.media,self.project,self.current().timeline,lowered,mixed,threading.Event())
        evidence=json.loads(mixed.with_suffix('.mix.json').read_text())
        self.assertEqual(evidence['layers'][0]['duration_seconds'],8)
        self.assertTrue(all(level is not None and level > -50 for level in evidence['measurements']['music']['window_rms_dbfs']))
        self.assertLess(evidence['measurements']['mix']['peak_dbfs'],-1)

    def test_reference_api_is_authenticated_and_csrf_protected(self):
        import asyncio
        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from clipen.editing import EditAgentCoordinator
        from test_project_api import request_app
        static=self.f.root/'static';static.mkdir();(static/'index.html').write_text('Clipen')
        coordinator=EditAgentCoordinator(self.repository,self.f.root)
        app=create_app(ControllerConfig(static),ProviderStatus('Codex',ProviderState.READY,'ready'),LocalSessionAuthority(),edit_agents=coordinator)
        def request(method,path,payload=None,headers=None):return asyncio.run(request_app(app,method,path,headers=headers,body=json.dumps(payload).encode() if payload is not None else b''))
        try:
            url=f'/api/projects/{self.project}/production/{self.run}/workbench/reference'
            args={'expected_revision_id':self.current().revision_id,'expected_version':0,'mutation_id':'api-note','reference':self.note()}
            self.assertEqual(request('POST',url,args)[0],401)
            _,headers,_=request('POST','/api/session/connect',headers={'origin':'http://localhost:4200','sec-fetch-site':'same-origin'})
            auth={'cookie':headers['set-cookie'].split(';',1)[0]};_,_,body=request('GET','/api/status',headers=auth)
            self.assertEqual(request('POST',url,args,auth)[0],403)
            mutation={**auth,'origin':'http://localhost:4200','sec-fetch-site':'same-origin','content-type':'application/json','x-clipen-csrf':json.loads(body)['session']['csrf_token']}
            code,_,body=request('POST',url,args,mutation);self.assertEqual(code,200,body);self.assertEqual(json.loads(body)['version'],1)
        finally:coordinator.close()
