"""User edits and refinement retain the exact native film and explicit music."""
import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch
import test_skill_production as fixtures
from clipen.editing.workbench import edit, state, import_music
from clipen.projects import ProjectError
from clipen.domain import TrackKind

class ProductionWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SkillProductionTests();self.f.setUp();self.f.cut()
        self.repository=self.f.repository;self.project=self.f.project.project_id;self.run=self.f.run.run_id
    def tearDown(self):self.f.tearDown()
    def stop(self):self.repository.cancel(self.project,self.run)

    def aligned_caption(self):
        from clipen.editing.agent_models import ToolResult
        text='原來的字幕'
        cue={'cue_id':'user-edit-cue','text':text,'start_frame':10,'duration_frames':30,'verification':'asr_draft','item_id':'agent-video-0001',
             'words':[{'text':text,'char_start':0,'char_end':len(text),'start_frame':10,'end_frame':40}]}
        with patch.object(self.f.tools,'_production_list_transcripts',return_value=ToolResult({'cues':[cue]})):
            self.f.call('replace_subtitles',captions=[{'cue_id':cue['cue_id'],'text':text}],allow_asr_draft=True,summary='建立來源對齊字幕')
        revision=self.repository.current_revision(self.project,self.run)
        item=next(i for t in revision.timeline.tracks if t.track_id=='agent-subtitles' for i in t.items)
        self.stop()
        return revision,item

    def test_manual_caption_wording_invalidates_alignment_and_undo_restores_it(self):
        from clipen.editing.program_subtitles import subtitle_status
        from clipen.editing.production_media import load_text_styles
        before,item=self.aligned_caption()
        original_profile=load_text_styles(self.f.projects,self.project)[item.style_id]
        values={'item_id':item.item_id,'start_frame':60,'duration_frames':30,'text':'人工修改的新字幕','style_id':'title.caption','summary':'改字和時間'}
        edit(self.repository,self.project,self.run,'edit_text',values,before.revision_id,'manual-caption-wording')
        current=self.repository.current_revision(self.project,self.run)
        changed=next(i for t in current.timeline.tracks if t.track_id=='agent-subtitles' for i in t.items)
        profile=load_text_styles(self.f.projects,self.project)[changed.style_id]
        self.assertNotEqual(changed.style_id,item.style_id)
        self.assertEqual(profile['wording_authority'],'user')
        self.assertEqual(profile['timing_basis'],'alignment_invalidated_by_user_text')
        self.assertEqual(profile['previous_timing_basis'],'forced_alignment')
        status=subtitle_status(self.f.projects,self.project,current.timeline)
        self.assertEqual(status['aligned_count'],0);self.assertEqual(status['unaligned_item_ids'],[item.item_id])
        # A later timing edit cannot silently restore evidence for different words.
        edit(self.repository,self.project,self.run,'edit_text',{**values,'start_frame':70},current.revision_id,'manual-caption-move')
        current=self.repository.current_revision(self.project,self.run)
        self.assertEqual(subtitle_status(self.f.projects,self.project,current.timeline)['unaligned_item_ids'],[item.item_id])
        self.assertEqual(load_text_styles(self.f.projects,self.project)[item.style_id],original_profile)
        edit(self.repository,self.project,self.run,'restore_revision',{'revision_id':before.revision_id,'summary':'還原字幕'},current.revision_id,'restore-caption-evidence')
        restored=self.repository.current_revision(self.project,self.run)
        self.assertEqual(subtitle_status(self.f.projects,self.project,restored.timeline)['aligned_count'],1)

    def test_manual_caption_timing_is_distinct_from_forced_alignment_and_draft_review(self):
        from clipen.editing.program_subtitles import subtitle_status
        before,item=self.aligned_caption()
        values={'item_id':item.item_id,'start_frame':60,'duration_frames':30,'text':item.text,'style_id':'title.caption','summary':'人工調整時間'}
        edit(self.repository,self.project,self.run,'edit_text',values,before.revision_id,'manual-caption-timing')
        current=self.repository.current_revision(self.project,self.run)
        status=subtitle_status(self.f.projects,self.project,current.timeline)
        self.assertEqual(status['aligned_count'],0);self.assertEqual(status['manual_timing_item_ids'],[item.item_id])
        self.assertEqual(status['unaligned_item_ids'],[])
        self.assertEqual(status['draft_item_ids'],[item.item_id])

    def test_unchanged_manual_caption_keeps_existing_alignment(self):
        from clipen.editing.program_subtitles import subtitle_status
        before,item=self.aligned_caption()
        edit(self.repository,self.project,self.run,'edit_text',{'item_id':item.item_id,'start_frame':item.start_frame,'duration_frames':item.duration_frames,'text':item.text,'style_id':'title.caption','summary':'保持原內容'},before.revision_id,'unchanged-caption')
        current=self.repository.current_revision(self.project,self.run)
        self.assertEqual(subtitle_status(self.f.projects,self.project,current.timeline)['aligned_count'],1)

    def test_identity_renderer_changes_invalidate_preview_recipe(self):
        import tempfile
        from clipen.rendering import program_identities
        from clipen.editing.production_preview import render_recipe_id
        before=render_recipe_id()
        with tempfile.TemporaryDirectory() as directory:
            changed=Path(directory)/'program_identities.py'
            changed.write_bytes(Path(program_identities.__file__).read_bytes()+b'\n# new identity renderer revision\n')
            with patch.object(program_identities,'__file__',str(changed)):
                self.assertNotEqual(render_recipe_id(),before)
        self.assertEqual(render_recipe_id(),before)

    def test_status_keeps_successful_preview_when_new_output_is_pending_or_fails(self):
        from clipen.rendering import PreviewRenderRepository
        from clipen.editing.coordinator import EditAgentCoordinator
        from clipen.editing.production_preview import _event,render_recipe_id
        renders=PreviewRenderRepository(self.f.projects)
        revision=self.repository.current_revision(self.project,self.run)
        recipe=render_recipe_id()
        old=renders.create(self.project,'previous-good-output',revision.revision_id)
        renders.mark_running(self.project,old['render_id'])
        # Exercise durable render states; encoding is covered by native render tests.
        path=renders.output_path(self.project,old['render_id']);path.write_bytes(b'isolated-output')
        renders.complete(self.project,old['render_id'],path.name,path.stat().st_size)
        _event(self.f.tools,'production_preview',{'render_id':old['render_id'],'revision_id':revision.revision_id,'recipe_id':recipe})
        self.repository.finish(self.project,self.run,'完成舊版')
        coordinator=EditAgentCoordinator(self.repository,self.f.root)
        try:
            for ending in ('failed','cancelled'):
                pending=renders.create(self.project,'next-output-'+ending,revision.revision_id)
                _event(self.f.tools,'production_preview',{'render_id':pending['render_id'],'revision_id':revision.revision_id,'recipe_id':recipe})
                for state in ('queued','running',ending):
                    if state=='running':renders.mark_running(self.project,pending['render_id'])
                    elif state==ending:renders.finish_unsuccessful(self.project,pending['render_id'],ending,error_code='ffmpeg_render_failed')
                    production=coordinator.status(self.project)['production']
                    self.assertEqual(production['preview']['state'],state)
                    self.assertEqual(production['last_successful_preview']['render_id'],old['render_id'])
                    self.assertEqual(production['last_successful_preview']['state'],'succeeded')
                with patch('clipen.editing.production_preview.render_recipe_id',return_value='changed-recipe'):
                    self.assertTrue(coordinator.status(self.project)['production']['last_successful_preview']['stale'])
        finally:coordinator.close()
    def test_manual_changes_are_versioned_without_ai_budget_and_exportable(self):
        self.f.call('add_program_component',element_id='header',item_id='agent-video-0001',component_id='headline',start_offset_frames=0,duration_frames=60,text='原來的標題',x=.08,y=.25,evidence='測試畫面標註',summary='元件')
        before=self.repository.current_revision(self.project,self.run);budget=self.repository.get(self.project,self.run).tool_calls
        with self.assertRaises(ProjectError):edit(self.repository,self.project,self.run,'remove_item',{'item_id':'header','summary':'刪除'},before.revision_id,'busy')
        self.stop()
        args={'item_id':'header','start_frame':0,'duration_frames':60,'text':'我修好的標題','style_id':'title.caption','summary':'修改標題'}
        result=edit(self.repository,self.project,self.run,'edit_text',args,before.revision_id,'user-text')
        current=self.repository.current_revision(self.project,self.run)
        self.assertNotEqual(current.revision_id,before.revision_id)
        self.assertEqual(current.timeline.tracks[0],before.timeline.tracks[0])
        item=next(i for t in current.timeline.tracks for i in t.items if i.item_id=='header')
        self.assertEqual(item.text,'我修好的標題');self.assertEqual(item.style_id,next(i for t in before.timeline.tracks for i in t.items if i.item_id=='header').style_id)
        self.assertEqual(self.repository.get(self.project,self.run).tool_calls,budget)
        replay=edit(self.repository,self.project,self.run,'edit_text',args,before.revision_id,'user-text')
        self.assertTrue(replay.get('replayed'))
        with self.assertRaises(ProjectError):edit(self.repository,self.project,self.run,'remove_item',{'item_id':'header','summary':'stale'},before.revision_id,'stale')
        info=state(self.repository,self.project,self.run);self.assertIn(item.style_id,info['styles'])
        # The manual edit is one version, newest first; AI steps are not listed one by one.
        self.assertEqual((info['history'][0]['revision_id'],info['history'][0]['label']),(current.revision_id,'手動修改：文字與時間'))
        from clipen.editing.workbench import tools_for
        exported=tools_for(self.repository,self.project,self.run).production_call('export_edit',{}).payload
        path=self.f.root/'projects'/self.project/'agent-production'/self.run/'exports'/(exported['revision_id']+'.json')
        native=json.loads(path.read_text());self.assertIn(item.style_id,native['text_styles']);self.assertEqual(native['timeline'],current.timeline.to_payload())
    def test_refine_keeps_timeline_and_frozen_style_and_is_idempotent(self):
        before=self.repository.current_revision(self.project,self.run);frozen=self.repository.production_session(self.project,self.run)['skill'];self.stop()
        run,created=self.repository.request_refinement(self.project,self.run,'refine-one',before.revision_id,'保留鏡頭，只修改字幕。')
        self.assertTrue(created);self.assertEqual(run.current_revision_id,before.revision_id);self.assertEqual(run.tool_calls,0)
        self.assertEqual(self.repository.current_revision(self.project,self.run).timeline,before.timeline)
        session=self.repository.production_session(self.project,self.run);self.assertEqual(session['skill'],frozen)
        self.assertEqual(session['intent']['latest_revision_request']['instruction'],'保留鏡頭，只修改字幕。')
        from clipen.editing import EditAgentTools
        fresh=EditAgentTools(self.repository,self.project,self.run);fresh.activate()
        try:
            with self.assertRaises(ProjectError) as rejected:
                fresh.call('replace_sequence',{'clips':[],'summary':'不應重新生成','expected_revision_id':before.revision_id,'mutation_id':'blocked-rebuild'})
            self.assertEqual(rejected.exception.code,'incremental_revision_required')
        finally:fresh.close_production()

        same,created=self.repository.request_refinement(self.project,self.run,'refine-one',before.revision_id,'保留鏡頭，只修改字幕。');self.assertFalse(created);self.assertEqual(same.run_id,run.run_id)
        with self.assertRaises(ProjectError):self.repository.request_refinement(self.project,self.run,'refine-two',before.revision_id,'不同修改')
    def test_local_music_is_frozen_as_user_choice_not_claimed_public_license(self):
        self.stop()
        from clipen.editing.production_media import AgentAudioStore
        directory=AgentAudioStore(self.f.projects,self.project).directory/'upload-test';directory.mkdir()
        path=directory/'music.wav'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=3','-y',str(path)],check=True,capture_output=True)
        asset=import_music(self.repository,self.project,path,'我的配樂.wav')
        self.assertEqual(asset['provenance']['source'],'user_upload');self.assertFalse(asset['provenance']['rights_verified'])
        before=self.repository.current_revision(self.project,self.run)
        run,_=self.repository.request_refinement(self.project,self.run,'use-my-music',before.revision_id,'換成我選的音樂',asset['asset_id'])
        self.assertEqual(self.repository.production_session(self.project,self.run)['intent']['music_choice']['asset_id'],asset['asset_id'])
        self.repository.cancel(self.project,self.run)
        with self.assertRaises(ProjectError):self.repository.request_refinement(self.project,self.run,'bad-music',before.revision_id,'換音樂','not-this-project')
        initial,created=self.repository.create_run(self.project,'initial-user-music',production={'skill_id':'travel-program','intent':'使用我提供的配樂製作影片','target_duration_seconds':300,'music_asset_id':asset['asset_id']})
        self.assertTrue(created)
        self.assertEqual(self.repository.production_session(self.project,initial.run_id)['intent']['music_choice']['asset_id'],asset['asset_id'])

    def test_authenticated_upload_edit_export_and_refinement_api(self):
        import asyncio
        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from clipen.editing import EditAgentCoordinator
        from clipen.providers import ProviderCancelled
        from test_project_api import request_app
        self.f.call('add_program_component',element_id='editable-header',item_id='agent-video-0001',component_id='headline',start_offset_frames=0,duration_frames=60,text='原標題',x=.08,y=.25,evidence='測試標題',summary='標題')
        self.stop();before=self.repository.current_revision(self.project,self.run)
        started=threading.Event()
        class LocalAgent:
            def __init__(self,projects_root,attempt_root,repository):self.repository=repository
            def execute(self,project_id,run_id,*,cancel):
                self.repository.activate(project_id,run_id);started.set();cancel.wait(3);raise ProviderCancelled('test')
        static=self.f.root/'api-dist';static.mkdir();(static/'index.html').write_text('Clipen')
        coordinator=EditAgentCoordinator(self.repository,self.f.root,adapter_factory=LocalAgent)
        app=create_app(ControllerConfig(static),ProviderStatus('Codex',ProviderState.READY,'ready'),LocalSessionAuthority(),edit_agents=coordinator)
        def request(method,path,payload=None,headers=None,raw=None):
            return asyncio.run(request_app(app,method,path,headers=headers,body=raw if raw is not None else b'' if payload is None else json.dumps(payload).encode()))
        try:
            base=f'/api/projects/{self.project}/production';work=f'{base}/{self.run}/workbench'
            self.assertEqual(request('GET',work)[0],401)
            _,headers,_=request('POST','/api/session/connect',headers={'origin':'http://localhost:4200','sec-fetch-site':'same-origin'})
            cookie=headers['set-cookie'].split(';',1)[0];_,_,body=request('GET','/api/status',headers={'cookie':cookie});csrf=json.loads(body)['session']['csrf_token']
            auth={'cookie':cookie};mutation={**auth,'origin':'http://localhost:4200','sec-fetch-site':'same-origin','content-type':'application/json','x-clipen-csrf':csrf}
            code,_,body=request('GET',work,headers=auth);self.assertEqual(code,200,body);self.assertEqual(json.loads(body)['revision']['revision_id'],before.revision_id)
            args={'tool':'edit_text','arguments':{'item_id':'editable-header','text':'人工修改','style_id':'title.caption','start_frame':0,'duration_frames':60,'summary':'人工修改'},'expected_revision_id':before.revision_id,'mutation_id':'api-user-edit'}
            self.assertEqual(request('POST',work+'/edit',args,auth)[0],403)
            code,_,body=request('POST',work+'/edit',args,mutation);self.assertEqual(code,200,body)
            current=self.repository.current_revision(self.project,self.run)
            self.assertNotEqual(current.revision_id,before.revision_id)
            code,_,body=request('POST',work+'/export',{},mutation);self.assertEqual(code,200,body);self.assertIn('download_url',json.loads(body))
            source=self.f.root/'upload.wav';subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=330:duration=2','-y',str(source)],check=True,capture_output=True)
            upload_headers={**mutation,'content-type':'application/octet-stream'}
            code,_,body=request('POST',base+'/music?filename=my-song.wav',headers=upload_headers,raw=source.read_bytes());self.assertEqual(code,201,body)
            asset=json.loads(body)['asset'];self.assertEqual(asset['provenance']['source'],'user_upload')
            self.assertEqual(request('POST',base+'/music?filename=bad.exe',headers=upload_headers,raw=b'bad')[0],422)
            refine={'request_id':'api-refine-film','instruction':'保留畫面，使用我的音樂','consent':True,'expected_revision_id':current.revision_id,'music_asset_id':asset['asset_id']}
            code,_,body=request('POST',work+'/refine',refine,mutation);self.assertEqual(code,200,body);self.assertTrue(started.wait(2))
            self.assertEqual(self.repository.current_revision(self.project,self.run).revision_id,current.revision_id)
            from clipen.editing import EditAgentTools
            tools=EditAgentTools(self.repository,self.project,self.run)
            with self.assertRaises(ProjectError):tools.production_call('search_music',{'tags':['calm']})
            tools.close_production()
        finally:coordinator.close()

    def test_second_ai_round_can_render_after_preview_budget_is_renewed(self):
        from clipen.rendering import PreviewRenderRepository
        class Renderer:
            def render(self,project,timeline,output,cancel,progress):output.write_bytes(b'preview');progress(1)
        self.f.tools.preview_renderer=Renderer()
        def render():
            result=self.f.tools.production_call('render_preview',{}).payload
            for cancel,thread in self.f.tools._preview_jobs.values():thread.join(3)
            self.assertEqual(PreviewRenderRepository(self.f.projects).get(self.project,result['render']['render_id'])['state'],'succeeded')
            return result['render']['render_id']
        with patch('clipen.editing.production_preview.verify_output'),patch.object(self.f.tools,'_production_validate_sequence',return_value=type('Result',(),{'payload':{'render_issues':[]}})()):
            first=render();self.stop()
            revision=self.repository.current_revision(self.project,self.run)
            self.repository.request_refinement(self.project,self.run,'second-render-round',revision.revision_id,'只降低原音')
            self.repository.activate(self.project,self.run)
            self.f.call('set_source_audio',item_id='agent-video-0001',mode='mute',gain_db=0,summary='第二輪修改')
            second=render()
            self.assertNotEqual(first,second)
            self.assertEqual(self.repository.get(self.project,self.run).preview_renders,1)

    def test_manual_preview_does_not_consume_ai_budget_or_cancel_terminal_run(self):
        self.stop()
        from clipen.editing.coordinator import EditAgentCoordinator
        before=self.repository.get(self.project,self.run)
        class Renderer:
            def render(self,project,timeline,output,cancel,progress):
                progress(.5);self.cancelled=cancel.is_set();output.write_bytes(b'preview');progress(1)
        renderer=Renderer();coordinator=EditAgentCoordinator(self.repository,self.f.root)
        from clipen.editing.workbench import tools_for
        tools=tools_for(self.repository,self.project,self.run);tools.preview_renderer=renderer
        coordinator._user_previews[(self.project,self.run)]=tools
        try:
            with patch('clipen.editing.production_preview.verify_output'),patch.object(tools,'_production_validate_sequence',return_value=type('Result',(),{'payload':{'render_issues':[]}})()):
                result=coordinator.workbench_render(self.project,self.run,before.current_revision_id)
                for cancel,thread in tools._preview_jobs.values():thread.join(3)
            from clipen.rendering import PreviewRenderRepository
            self.assertEqual(PreviewRenderRepository(self.f.projects).get(self.project,result['render']['render_id'])['state'],'succeeded')
            after=self.repository.get(self.project,self.run);self.assertEqual(after.preview_renders,before.preview_renders);self.assertEqual(after.state,before.state);self.assertFalse(renderer.cancelled)
        finally:coordinator.close()
