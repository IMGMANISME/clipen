"""Durable prompts and visual manual editing without a paid agent or timeline writes."""
import asyncio
import base64
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from PIL import Image
import test_skill_production as fixtures
from clipen.editing import EditAgentRepository, EditAgentCoordinator
from clipen.editing.prompt_history import history, save
from clipen.editing.visual_preview import frame
from clipen.rendering.titles import TitleCardRenderer
from clipen.projects import ProjectError


class CanvasAndPromptTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SkillProductionTests(); self.f.setUp(); self.f.cut()
        self.repository = self.f.repository; self.project = self.f.project.project_id; self.run = self.f.run.run_id

    def tearDown(self):
        self.f.tearDown()

    def test_prompt_save_is_durable_idempotent_and_isolated_from_ai(self):
        before = self.repository.get(self.project, self.run)
        first = save(self.repository, self.project, 'save-one', 'refinement', '保留剪輯，只修字幕。')
        self.assertEqual(save(self.repository, self.project, 'save-one', 'refinement', '保留剪輯，只修字幕。'), first)
        with self.assertRaises(ProjectError) as error:
            save(self.repository, self.project, 'save-one', 'refinement', '另一份需求')
        self.assertEqual(error.exception.status_code, 409)
        loaded = history(EditAgentRepository(self.f.projects), self.project)
        self.assertEqual(loaded['total'], 2)
        self.assertEqual(loaded['prompts'][0]['text'], '保留剪輯，只修字幕。')
        self.assertEqual(loaded['prompts'][0]['project_name'], '全新節目')
        self.assertEqual(self.repository.get(self.project, self.run), before)

    def test_recovers_old_initial_and_all_refinement_prompts_after_failure(self):
        initial = history(self.repository, self.project)['prompts'][0]
        self.assertEqual(initial['text'], '用真實對話製作節目，配合字幕、音樂與資訊標註。')
        before = self.repository.current_revision(self.project, self.run)
        for number in range(2):
            self.repository.cancel(self.project, self.run)
            self.repository.request_refinement(self.project, self.run, f'refine-{number}', before.revision_id, f'修改要求 {number}')
        rows = history(self.repository, self.project)['prompts']
        self.assertEqual([r['text'] for r in rows if r['kind'] == 'refinement'], ['修改要求 1', '修改要求 0'])
        self.assertEqual(self.repository.current_revision(self.project, self.run), before)

    def test_refinement_history_keeps_distinct_entries_across_production_runs(self):
        before = self.repository.current_revision(self.project, self.run)
        self.repository.cancel(self.project, self.run)
        self.repository.request_refinement(self.project, self.run, 'first-film-revision', before.revision_id, '修改第一版')
        self.repository.cancel(self.project, self.run)
        second, _ = self.repository.create_run(self.project, 'second-film', production={'skill_id':'travel-program','intent':'第二個故事'})
        self.repository.cancel(self.project, second.run_id)
        current = self.repository.current_revision(self.project, second.run_id)
        self.repository.request_refinement(self.project, second.run_id, 'second-film-revision', current.revision_id, '修改第二版')
        rows = history(self.repository, self.project)['prompts']
        self.assertEqual(len({row['prompt_id'] for row in rows}), 4)
        self.assertEqual({row['text'] for row in rows if row['kind']=='refinement'}, {'修改第一版','修改第二版'})

    def test_project_and_workspace_scopes_and_pagination(self):
        other = self.f.projects.create_project('other-project', '另一支影片')
        save(self.repository, other.project_id, 'save-before-production', 'production', '第二部影片的需求')
        self.assertEqual(history(self.repository, other.project_id)['total'], 1)
        workspace = history(self.repository)
        self.assertEqual(workspace['total'], 2)
        self.assertEqual({r['project_id'] for r in workspace['prompts']}, {self.project, other.project_id})
        self.assertEqual(history(self.repository, offset=1, limit=1)['prompts'], workspace['prompts'][1:2])
        self.assertNotIn('canonical_path', json.dumps(workspace))
        for invalid in ('', ' ', 'a' * 8001):
            with self.assertRaises(ProjectError): save(self.repository, other.project_id, 'invalid', 'production', invalid)

    def test_actual_frame_never_mutates_revision_or_budget(self):
        self.f.component(element_id='live-label', start_offset_frames=0, duration_frames=60, text='原標註')
        self.f.real_media()
        before = self.repository.current_revision(self.project, self.run)
        budget = self.repository.get(self.project, self.run)
        with patch('clipen.editing.visual_preview.MediaPreviewRepository', return_value=self.f.tools.media):
            still = frame(self.repository, self.project, self.run, before.revision_id, 30)
        with Image.open(BytesIO(base64.b64decode(still['image_data_url'].split(',',1)[1]))) as image:
            self.assertEqual(image.size, (640, 360)); self.assertEqual(image.format, 'JPEG')
        self.assertEqual(still['source_item_id'], 'agent-video-0001')
        self.assertEqual(self.repository.current_revision(self.project, self.run), before)
        self.assertEqual(self.repository.get(self.project, self.run), budget)

    def test_a_still_can_preview_unsaved_component_wording(self):
        self.f.component(element_id='live-label', start_offset_frames=0, duration_frames=60, text='原標註')
        self.f.real_media()
        before = self.repository.current_revision(self.project, self.run)
        drawn = []
        original = TitleCardRenderer._draw_card
        def record(renderer, draw, font_module, font, timeline, item, offset):
            drawn.append(item.text)
            return original(renderer, draw, font_module, font, timeline, item, offset)
        with patch('clipen.editing.visual_preview.MediaPreviewRepository', return_value=self.f.tools.media), \
             patch.object(TitleCardRenderer, '_draw_card', record):
            frame(self.repository, self.project, self.run, before.revision_id, 30, {'element_id': 'live-label', 'text': '新標註'})
            for invalid in ({'element_id': 'live-label', 'text': ' '}, {'element_id': 'live-label', 'text': 'x' * 201}, 'text'):
                with self.assertRaises(ProjectError): frame(self.repository, self.project, self.run, before.revision_id, 30, invalid)
        self.assertIn('新標註', drawn); self.assertNotIn('原標註', drawn)
        # Previewing never saves the wording.
        self.assertEqual(self.repository.current_revision(self.project, self.run), before)

    def test_canvas_seeks_into_the_selected_clip_using_source_pts(self):
        current = self.repository.current_revision(self.project, self.run)
        self.f.real_media()
        commands = []
        def source_image(command, *_args, **_kwargs):
            commands.append(command)
            Image.new('RGB',(640,360),(20,40,60)).save(command[-1])
        with patch('clipen.editing.visual_preview.MediaPreviewRepository',return_value=self.f.tools.media), patch('clipen.editing.visual_preview.run_process',side_effect=source_image):
            result = frame(self.repository,self.project,self.run,current.revision_id,150)
        self.assertEqual(result['source_item_id'],'agent-video-0002')
        command = commands[0]
        # Second shot starts at source 5 s (1 s PTS origin + 4 s trim), plus 1 s into the shot.
        self.assertAlmostEqual(float(command[command.index('-ss')+1]),6)

    def test_invalid_or_stale_frame_is_rejected_before_opening_source(self):
        self.f.component(element_id='live-label')
        current = self.repository.current_revision(self.project, self.run)
        with patch('clipen.editing.visual_preview.MediaPreviewRepository') as media:
            with self.assertRaises(ProjectError) as error: frame(self.repository,self.project,self.run,'old-revision',20)
            self.assertEqual(error.exception.status_code,409)
            for number in (-1,True,1.5,current.timeline.duration_frames):
                with self.assertRaises(ProjectError): frame(self.repository,self.project,self.run,current.revision_id,number)
            media.assert_not_called()

    def test_prompt_and_frame_api_require_session_and_csrf(self):
        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from test_project_api import request_app
        static = self.f.root / 'static'; static.mkdir(); (static/'index.html').write_text('Clipen')
        coordinator = EditAgentCoordinator(self.repository, self.f.root)
        app = create_app(ControllerConfig(static),ProviderStatus('Codex',ProviderState.READY,'ready'),LocalSessionAuthority(),edit_agents=coordinator)
        def request(method,path,payload=None,headers=None):
            return asyncio.run(request_app(app,method,path,headers=headers,body=json.dumps(payload).encode() if payload is not None else b''))
        try:
            path = f'/api/projects/{self.project}/production/prompts'
            frame_path = f'/api/projects/{self.project}/production/{self.run}/workbench/frame'
            self.assertEqual(request('GET','/api/production/prompts')[0],401)
            self.assertEqual(request('POST',frame_path,{})[0],401)
            _, headers, _ = request('POST','/api/session/connect',headers={'origin':'http://localhost:4200','sec-fetch-site':'same-origin'})
            auth = {'cookie':headers['set-cookie'].split(';',1)[0]}
            _, _, body = request('GET','/api/status',headers=auth)
            mutation = {**auth,'origin':'http://localhost:4200','sec-fetch-site':'same-origin','content-type':'application/json','x-clipen-csrf':json.loads(body)['session']['csrf_token']}
            payload = {'request_id':'http-save','kind':'production','text':'儲存但不啟動 AI'}
            self.assertEqual(request('POST',path,payload,auth)[0],403)
            self.assertEqual(request('POST',path,payload,mutation)[0],201)
            code, _, body = request('GET',f'/api/production/prompts?project_id={self.project}',headers=auth)
            self.assertEqual(code,200,body); self.assertEqual(json.loads(body)['total'],2)
            self.assertEqual(request('GET','/api/production/prompts?offset=-1',headers=auth)[0],422)
            self.assertEqual(request('POST',frame_path,{},auth)[0],403)
            current = self.repository.current_revision(self.project,self.run)
            with patch('clipen.editing.visual_preview.frame',return_value={'frame':30}) as render:
                code, _, body = request('POST',frame_path,{'expected_revision_id':current.revision_id,'frame':30},mutation)
                self.assertEqual(code,200,body); self.assertEqual(render.call_args.args[3:6],(current.revision_id,30,None))
        finally:
            coordinator.close()
