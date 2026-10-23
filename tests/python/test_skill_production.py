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
              'duration_frames':45,'text':'招牌餐點','detail':'菜單標示 980 円','evidence':'本測試來源資訊標註','summary':'介紹餐點'}
        args.update(overrides)
        if args['component_id']!='fact' and 'detail' not in overrides:args.pop('detail')
        # Production now requires an explicit placement; tests pick each component's designed spot.
        spot=next((c['default_position'] for c in get_skill()['components'] if c['component_id']==args['component_id']),{'x':.06,'y':.12})
        for axis in ('x','y'):args.setdefault(axis,spot[axis])
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

    def test_status_reports_bounded_real_activity_without_invented_progress(self):
        from clipen.editing import EditAgentCoordinator
        self.cut();self.component()
        for _ in range(15):self.call('list_program_components')
        coordinator=EditAgentCoordinator(self.repository,self.root)
        status=coordinator.status(self.project.project_id)
        progress=status['production']['progress']
        self.assertEqual(len(progress['activity']),12)
        self.assertEqual(progress['activity'][-1]['tool'],'list_program_components')
        self.assertEqual(progress['activity'][-1]['label'],'理解製作需求與影片風格')
        self.assertEqual(progress['updated_at'],progress['activity'][-1]['created_at'])
        self.assertNotIn('percent',progress)
        self.assertEqual([e['sequence'] for e in progress['activity']],sorted(e['sequence'] for e in progress['activity']))
        self.repository.record_provider_outcome(self.project.project_id,self.run.run_id,'failed-test',
                                               usage=None,error_code='provider_network_error')
        failed=coordinator.status(self.project.project_id)
        self.assertEqual(failed['latest_run']['error_code'],'provider_network_error')
        self.assertEqual(failed['production']['draft']['ordinal'],status['production']['draft']['ordinal'])
        self.assertEqual(failed['production']['progress']['activity'][-1]['kind'],'provider_failed')
        self.assertEqual(failed['production']['progress']['activity'][-2],progress['activity'][-1])

    def test_quantized_zero_width_alignment_is_grouped_without_invented_character_times(self):
        words=validate_alignment([{'text':'點','start':2.24,'end':2.56},{'text':'一','start':2.56,'end':2.56},
                                  {'text':'個','start':2.64,'end':3.04}],'點一個',4)
        self.assertEqual(words[0],{'text':'點一','char_start':0,'char_end':2,'start_seconds':2.24,'end_seconds':2.56})
        self.assertEqual(words[1]['start_seconds'],2.64)
        leading=validate_alignment([{'text':'一','start':1,'end':1},{'text':'個','start':1,'end':1.3}],'一個',2)
        self.assertEqual(leading[0]['text'],'一個')
        self.assertEqual(leading[0]['start_seconds'],1)
        with self.assertRaises(ProjectError):
            validate_alignment([{'text':'一','start':0,'end':0},{'text':'個','start':1,'end':1.3}],'一個',2)

    def test_pacing_groups_split_continuous_source_instead_of_counting_fake_variety(self):
        self.cut()
        report=self.call('inspect_pacing').payload
        self.assertEqual(len(report['source_runs']),1)
        self.assertEqual(report['source_runs'][0]['duration_seconds'],8)
        self.assertEqual(len(report['source_runs'][0]['item_ids']),2)
        self.call('reorder_clips',item_ids=['agent-video-0002','agent-video-0001'],summary='來源區間不連續')
        self.assertEqual(len(self.call('inspect_pacing').payload['source_runs']),2)

    def test_caption_omission_cannot_pass_by_retaining_one_aligned_word(self):
        from clipen.editing.programme_review import caption_coverage
        from clipen.editing.agent_models import ToolResult
        from clipen.editing.production_media import atomic_json,text_style_path
        from clipen.domain import Track
        from dataclasses import replace
        self.cut()
        cue={'cue_id':'first','item_id':'agent-video-0001','text':'咖啡。','start_frame':0,'duration_frames':30}
        second={**cue,'cue_id':'second','item_id':'agent-video-0002','text':'餐點上桌。','start_frame':120}
        styles={'test.caption':{'kind':'program_subtitle','anchor_item_id':cue['item_id'],'cue_id':cue['cue_id'],
                               'timing_basis':'forced_alignment'}}
        atomic_json(text_style_path(self.projects,self.project.project_id),styles)
        timeline=replace(self.tools._timeline(),tracks=self.tools._timeline().tracks+(
            Track('agent-subtitles',TrackKind.TITLE,(TextClip('one-word',0,30,'咖啡。','test.caption'),)),))
        self.repository.save_revision(self.project.project_id,self.run.run_id,timeline,'只保留一小句')
        speech={'cues':[cue,second],'limitations':[]}
        result=caption_coverage(self.tools,speech)
        self.assertFalse(result['complete'])
        self.assertEqual([i['cue_id'] for i in result['missing_cues']],['second'])
        with patch.object(self.tools,'_production_list_transcripts',return_value=ToolResult(speech)):
            self.assertIn('subtitle_coverage_incomplete',{i['code'] for i in self.call('validate_sequence').payload['delivery_issues']})
        self.assertTrue(caption_coverage(self.tools,{'cues':[cue],'limitations':[]})['complete'])
        unresolved=caption_coverage(self.tools,{'cues':[],'limitations':[{'item_id':'agent-video-0002','reason':'partial_utterance'}]})
        self.assertEqual(unresolved['unresolved_item_ids'],['agent-video-0002'])

    def test_visual_style_selection_is_frozen_and_request_replay_is_consistent(self):
        self.repository.cancel(self.project.project_id,self.run.run_id)
        run,_=self.repository.create_run(self.project.project_id,'cinema-style-request',production={
            'skill_id':'travel-program','intent':'同一套剪輯選擇不同元件風格','visual_style_id':'cinema'})
        frozen=self.repository.production_session(self.project.project_id,run.run_id)['skill']
        self.assertEqual(frozen['selected_visual_style_id'],'cinema')
        self.assertTrue({'variety','editorial','cinema'} <= {s['visual_style_id'] for s in frozen['visual_styles']})
        self.assertIn('assets/style-cinema.png',frozen['assets'])
        repeated,created=self.repository.create_run(self.project.project_id,'cinema-style-request',production={
            'skill_id':'travel-program','intent':'同一套剪輯選擇不同元件風格','visual_style_id':'cinema'})
        self.assertFalse(created);self.assertEqual(repeated.run_id,run.run_id)
        with self.assertRaises(ProjectError):
            self.repository.create_run(self.project.project_id,'cinema-style-request',production={
                'skill_id':'travel-program','intent':'同一套剪輯選擇不同元件風格','visual_style_id':'editorial'})
        with self.assertRaises(ProjectError):
            self.repository.create_run(self.project.project_id,'unknown-style-request',production={
                'skill_id':'travel-program','intent':'元件風格','visual_style_id':'unknown'})

    def test_restyle_preserves_cuts_text_timing_and_other_tracks_with_undo(self):
        from clipen.editing.production_media import load_text_styles
        self.cut()
        self.component(element_id='title',component_id='program_title',text='城市慢遊',y=.1)
        self.component(element_id='person',component_id='person',text='現場人物',y=.58)
        self.call('set_color_grade',item_id='agent-video-0001',contrast=1.1,saturation=1,brightness=0,summary='保留調色')
        self.call('set_source_audio',item_id='agent-video-0001',mode='dialogue',gain_db=-1,summary='保留人聲設定')
        from dataclasses import replace
        from clipen.domain import Track
        current=self.tools._timeline()
        self.repository.save_revision(self.project.project_id,self.run.run_id,
            replace(current,tracks=current.tracks+(Track('agent-subtitles',TrackKind.TITLE,
                (TextClip('unchanged-subtitle',12,30,'保留這句字幕','title.caption'),)),)),'保留字幕測試')
        before=self.repository.current_revision(self.project.project_id,self.run.run_id)
        result=self.call('restyle_program_components',visual_style_id='cinema',summary='整套切換影誌風格')
        after=self.tools._timeline();profiles=load_text_styles(self.projects,self.project.project_id)
        self.assertEqual(after.tracks[0],before.timeline.tracks[0])
        for a,b in zip(before.timeline.tracks[1:],after.tracks[1:]):
            if a.track_id=='agent-subtitles':
                self.assertEqual(a,b);continue
            for left,right in zip(a.items,b.items):
                self.assertEqual((left.item_id,left.start_frame,left.duration_frames,left.text),
                                 (right.item_id,right.start_frame,right.duration_frames,right.text))
                self.assertEqual(profiles[right.style_id]['visual_style_id'],'cinema')
                self.assertIn(left.style_id,profiles)
        self.assertEqual(set(result.payload['changed_element_ids']),{'title','person'})
        self.call('update_program_component',element_id='title',item_id='agent-video-0001',component_id='program_title',
                  start_offset_frames=6,duration_frames=45,text='城市慢遊計畫',evidence='編輯標題',summary='換字保留風格',y=.1)
        updated=next(i for t in self.tools._timeline().tracks for i in t.items if i.item_id=='title')
        self.assertEqual(load_text_styles(self.projects,self.project.project_id)[updated.style_id]['visual_style_id'],'cinema')
        self.call('restore_revision',revision_id=before.revision_id,summary='還原整套樣式')
        self.assertEqual(self.tools._timeline(),before.timeline)
        with self.assertRaises(ProjectError):self.call('restyle_program_components',visual_style_id='editorial',element_ids=['missing'],summary='不可部分修改')

    def test_all_component_styles_draw_distinct_frames_with_complete_traditional_glyphs(self):
        from PIL import Image,ImageChops,ImageDraw
        from clipen.skills.visual_styles import style_presets
        from clipen.rendering.program_components import draw_program_component
        from clipen.rendering.program_styles import load_style_font
        from clipen.rendering.titles import TitleCardRenderer
        self.cut();skill=get_skill();font=TitleCardRenderer._font_path()
        for component in skill['components']:
            frames=[]
            for style in ('variety','editorial','cinema'):
                _,presets=style_presets(skill,style)
                profile={'kind':'program_component','component_id':component['component_id'],'presets':presets,**component['default_position']}
                if component['requires_target']:profile['target']={'x':.77,'y':.56,'radius':.05}
                image=Image.new('RGBA',(640,360));item=TextClip('style-test',0,90,'城市・慢遊','test')
                draw_program_component(ImageDraw.Draw(image),font,self.tools._timeline(),item,profile,35)
                self.assertIsNotNone(image.getbbox());frames.append(image)
                selected=load_style_font(font,32,profile)
                for char in '遊發題':self.assertIsNotNone(selected.getmask(char).getbbox())
            self.assertIsNotNone(ImageChops.difference(frames[0],frames[1]).getbbox())
            self.assertIsNotNone(ImageChops.difference(frames[1],frames[2]).getbbox())
        frozen=self.tools._skill_value();self.tools._session_cache={**self.tools._session_value(),'skill':{k:v for k,v in frozen.items() if k not in {'visual_styles','selected_visual_style_id'}}}
        with self.assertRaises(ProjectError):self.component(visual_style_id='cinema')

    def test_editorial_composition_motion_remains_inside_measured_footprint(self):
        from dataclasses import replace
        from PIL import Image, ImageDraw
        from clipen.skills.visual_styles import style_presets
        from clipen.rendering.program_components import component_bounds, draw_program_component
        from clipen.rendering.titles import TitleCardRenderer
        self.cut()
        _, presets = style_presets(get_skill(), 'editorial')
        for width, height in ((640,360),(1920,1080)):
            timeline = replace(self.tools._timeline(),width=width,height=height)
            for kind in ('program_title','headline','impact','chapter','burst','reaction','fact','person','location'):
                profile = {'kind':'program_component','component_id':kind,'presets':presets,'x':.08,'y':.12}
                item = TextClip('motion',0,90,'咖啡・時光\n街角小日常','motion')
                canvas = Image.new('RGBA',(width,height))
                bounds, _, _ = component_bounds(ImageDraw.Draw(canvas),TitleCardRenderer._font_path(),timeline,item,profile)
                frames = []
                for frame in (0,8,25,59,89):
                    image = canvas.copy()
                    draw_program_component(ImageDraw.Draw(image),TitleCardRenderer._font_path(),timeline,item,profile,frame)
                    actual = image.getbbox()
                    self.assertIsNotNone(actual)
                    self.assertGreaterEqual(actual[0],bounds[0])
                    self.assertGreaterEqual(actual[1],bounds[1])
                    self.assertLessEqual(actual[2],bounds[2]+1)
                    self.assertLessEqual(actual[3],bounds[3]+1)
                    frames.append(image.tobytes())
                self.assertNotEqual(frames[0],frames[2])
                self.assertNotEqual(frames[3],frames[4])

    def test_font_selection_freezes_with_request_and_replay(self):
        self.repository.cancel(self.project.project_id,self.run.run_id)
        request={'skill_id':'travel-program','intent':'保留版型，更換字體','visual_style_id':'editorial','font_style_id':'street'}
        run,_=self.repository.create_run(self.project.project_id,'font-request',production=request)
        snapshot=self.repository.production_session(self.project.project_id,run.run_id)
        self.assertEqual(snapshot['skill']['selected_font_style_id'],'street')
        self.assertEqual(snapshot['intent']['font_style_id'],'street')
        self.assertIn('font_styles.json',snapshot['skill']['resources'])
        self.assertIn('assets/font-street.png',snapshot['skill']['assets'])
        repeated,created=self.repository.create_run(self.project.project_id,'font-request',production=request)
        self.assertFalse(created);self.assertEqual(repeated.run_id,run.run_id)
        with self.assertRaises(ProjectError):
            self.repository.create_run(self.project.project_id,'font-request',production={**request,'font_style_id':'travel'})
        with self.assertRaises(ProjectError):
            self.repository.create_run(self.project.project_id,'invalid-font',production={**request,'font_style_id':'unknown'})

    def test_font_restyle_retains_layout_identity_text_and_timing_and_is_undoable(self):
        from clipen.editing.production_media import load_text_styles
        self.cut();self.component(element_id='title',component_id='program_title',text='城市慢遊',y=.1)
        before=self.repository.current_revision(self.project.project_id,self.run.run_id)
        self.call('restyle_program_components',visual_style_id='variety',font_style_id='street',summary='只換字體搭配')
        after=self.tools._timeline()
        self.assertEqual(before.timeline.tracks[0],after.tracks[0])
        left=next(i for t in before.timeline.tracks for i in t.items if i.item_id=='title')
        right=next(i for t in after.tracks for i in t.items if i.item_id=='title')
        self.assertEqual((left.text,left.start_frame,left.duration_frames),(right.text,right.start_frame,right.duration_frames))
        profile=load_text_styles(self.projects,self.project.project_id)[right.style_id]
        self.assertEqual(profile['visual_style_id'],'variety');self.assertEqual(profile['font_style_id'],'street')
        self.assertEqual(profile['presets']['font_roles']['title']['family'],'serif')
        self.call('update_program_component',element_id='title',item_id='agent-video-0001',component_id='program_title',start_offset_frames=6,duration_frames=45,text='城市慢遊',evidence='編輯主標',summary='改字保留字體',y=.1)
        right=next(i for t in self.tools._timeline().tracks for i in t.items if i.item_id=='title')
        self.assertEqual(load_text_styles(self.projects,self.project.project_id)[right.style_id]['font_style_id'],'street')
        self.call('restore_revision',revision_id=before.revision_id,summary='還原字體')
        self.assertEqual(self.tools._timeline(),before.timeline)

    def test_font_pairings_measure_and_draw_every_component_and_preserve_legacy(self):
        from PIL import Image,ImageDraw
        from clipen.skills.visual_styles import style_presets,font_presets
        from clipen.rendering.program_components import draw_program_component
        from clipen.rendering.titles import TitleCardRenderer
        self.cut();skill=get_skill()
        from clipen.rendering.program_styles import load_style_font
        signatures=[];available_faces=[]
        for identifier in ('japanese','street','travel'):
            for component in skill['components']:
                _,presets=font_presets(skill,style_presets(skill,'editorial')[1],identifier)
                profile={'kind':'program_component','component_id':component['component_id'],'presets':presets,**component['default_position']}
                if component['requires_target']:profile['target']={'x':.77,'y':.56,'radius':.05}
                canvas=Image.new('RGBA',(640,360))
                draw_program_component(ImageDraw.Draw(canvas),TitleCardRenderer._font_path(),self.tools._timeline(),TextClip('font',0,90,'城市・慢遊\n街角小日常','font'),profile,35)
                self.assertIsNotNone(canvas.getbbox())
                if component['component_id']=='program_title':
                    signatures.append(canvas.tobytes())
                    selected=load_style_font(TitleCardRenderer._font_path(),36,profile)
                    available_faces.append((str(selected.path),selected.index))
        # A host without the preferred families can legitimately substitute one
        # installed face. Distinct available faces must still render distinctly.
        self.assertGreaterEqual(len(set(signatures)),len(set(available_faces)))
        legacy={k:v for k,v in skill.items() if k not in {'font_styles','selected_font_style_id'}}
        self.assertEqual(font_presets(legacy,skill['presets'])[1],skill['presets'])
        with self.assertRaises(ProjectError):font_presets(legacy,skill['presets'],'street')

    def test_bundled_handwriting_and_kai_are_verified_and_render_all_styles(self):
        from PIL import Image,ImageDraw
        from clipen.skills.visual_styles import style_presets,font_presets
        from clipen.rendering.program_styles import load_style_font,bundled_font,_has_glyph
        from clipen.rendering.program_components import draw_program_component
        from clipen.rendering.titles import TitleCardRenderer,TitleRenderError
        self.cut();skill=get_skill();faces=[]
        for identifier in ('handwriting','calligraphy'):
            for visual in ('variety','editorial','cinema'):
                for component in skill['components']:
                    _,presets=font_presets(skill,style_presets(skill,visual)[1],identifier)
                    profile={'kind':'program_component','component_id':component['component_id'],'presets':presets,**component['default_position']}
                    if component['requires_target']:profile['target']={'x':.77,'y':.56,'radius':.05}
                    canvas=Image.new('RGBA',(640,360))
                    draw_program_component(ImageDraw.Draw(canvas),TitleCardRenderer._font_path(),self.tools._timeline(),TextClip('handwritten',0,90,'咖啡・時光\n旅行日記','test'),profile,35)
                    self.assertIsNotNone(canvas.getbbox())
            selected=load_style_font(TitleCardRenderer._font_path(),48,{'presets':presets,'component_id':'program_title'})
            faces.append(selected.getname())
            for char in '咖啡時光城市慢遊發現體驗臺灣':
                self.assertTrue(_has_glyph(str(selected.path),48,selected.index,char))
            specification=presets['font_roles']['title']
            with self.assertRaises(TitleRenderError):bundled_font({**specification,'sha256':'0'*64})
        self.assertNotEqual(faces[0],faces[1])
        with self.assertRaises(TitleRenderError):bundled_font({'bundled_font':'../../secret','sha256':'0'*64})
        self.assertIn('references/font-chenyu-OFL.txt',skill['resources'])
        self.assertIn('references/font-iansui-OFL.txt',skill['resources'])

    def test_authenticated_resume_dispatches_existing_cut_without_resetting_budget(self):
        import asyncio
        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from clipen.editing import EditAgentCoordinator
        from clipen.providers import ProviderCancelled
        from test_project_api import request_app
        self.cut();before=self.repository.get(self.project.project_id,self.run.run_id)
        self.repository.record_provider_outcome(self.project.project_id,self.run.run_id,'timed-out',usage=None,error_code='provider_timed_out')
        started=threading.Event();seen=[]
        class LocalOnlyAgent:
            def __init__(self,projects_root,attempt_root,repository):self.repository=repository
            def execute(self,project_id,run_id,*,cancel):
                seen.append(self.repository.activate(project_id,run_id));started.set();cancel.wait(3)
                raise ProviderCancelled('test stop')
        static=self.root/'resume-dist';static.mkdir();(static/'index.html').write_text('Clipen')
        coordinator=EditAgentCoordinator(self.repository,self.root,adapter_factory=LocalOnlyAgent)
        app=create_app(ControllerConfig(static),ProviderStatus('Codex',ProviderState.READY,'ready'),LocalSessionAuthority(),edit_agents=coordinator)
        def request(method,path,payload=None,headers=None):
            return asyncio.run(request_app(app,method,path,headers=headers,body=b'' if payload is None else json.dumps(payload).encode()))
        try:
            _,headers,_=request('POST','/api/session/connect',headers={'origin':'http://localhost:4200','sec-fetch-site':'same-origin'})
            cookie=headers['set-cookie'].split(';',1)[0]
            _,_,body=request('GET','/api/status',headers={'cookie':cookie});csrf=json.loads(body)['session']['csrf_token']
            mutation={'origin':'http://localhost:4200','sec-fetch-site':'same-origin','content-type':'application/json','cookie':cookie,'x-clipen-csrf':csrf}
            route=f'/api/projects/{self.project.project_id}/production/commands/resume/{self.run.run_id}'
            self.assertEqual(request('POST',route,{'consent':True})[0],401)
            self.assertEqual(request('POST',route,{'consent':True},{'cookie':cookie})[0],403)
            self.assertEqual(request('POST',route,{'consent':False},mutation)[0],422)
            code,_,body=request('POST',route,{'consent':True},mutation)
            self.assertEqual(code,202,body);self.assertTrue(started.wait(2))
            self.assertEqual(seen[0].current_revision_id,before.current_revision_id)
            self.assertEqual(seen[0].tool_calls,before.tool_calls)
            self.assertEqual(request('POST',route,{'consent':True},mutation)[0],409)
        finally:coordinator.close()

    def test_timeout_resume_keeps_draft_skill_and_consumed_budget(self):
        self.cut()
        before=self.repository.get(self.project.project_id,self.run.run_id)
        frozen=self.repository.production_session(self.project.project_id,self.run.run_id)
        self.repository.record_provider_outcome(self.project.project_id,self.run.run_id,'timeout-attempt',usage=None,error_code='provider_timed_out')
        resumed=self.repository.resume_timed_out(self.project.project_id,self.run.run_id)
        self.assertEqual(resumed.state,'queued');self.assertIsNone(resumed.error_code)
        self.assertEqual(resumed.current_revision_id,before.current_revision_id)
        self.assertEqual(resumed.tool_calls,before.tool_calls)
        self.assertEqual(self.repository.production_session(self.project.project_id,self.run.run_id),frozen)
        with self.assertRaises(ProjectError):self.repository.resume_timed_out(self.project.project_id,self.run.run_id)
        self.repository.activate(self.project.project_id,self.run.run_id)
        self.repository.record_provider_outcome(self.project.project_id,self.run.run_id,'other-failure',usage=None,error_code='provider_network_error')
        with self.assertRaises(ProjectError):self.repository.resume_timed_out(self.project.project_id,self.run.run_id)

    def test_usage_limit_resume_continues_the_draft_and_keeps_spent_usage(self):
        self.cut()
        before=self.repository.get(self.project.project_id,self.run.run_id)
        spent={'input_tokens':14758358,'cached_input_tokens':14392319,'output_tokens':125750}
        self.repository.record_provider_outcome(self.project.project_id,self.run.run_id,'quota-attempt',usage=spent,error_code='provider_usage_limit')
        resumed=self.repository.resume_stopped(self.project.project_id,self.run.run_id)
        self.assertEqual((resumed.state,resumed.error_code),('queued',None))
        self.assertEqual(resumed.current_revision_id,before.current_revision_id)
        self.assertEqual(resumed.tool_calls,before.tool_calls)
        with self.projects.connect(self.project.project_id) as db:
            row=db.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='run_resumed'",(self.run.run_id,)).fetchone()
        event=json.loads(row['payload_json'])
        self.assertEqual((event['previous_error_code'],event['previous_usage']),('provider_usage_limit',spent))
        # The account may still be limited; stopping again keeps the draft resumable.
        self.repository.activate(self.project.project_id,self.run.run_id)
        self.repository.record_provider_outcome(self.project.project_id,self.run.run_id,'quota-again',usage=None,error_code='provider_usage_limit')
        self.assertEqual(self.repository.resume_stopped(self.project.project_id,self.run.run_id).current_revision_id,before.current_revision_id)

    def test_explicit_long_form_session_accepts_ten_minutes_without_relaxing_default(self):
        from clipen.editing.production_limits import limits_for
        self.cut()
        candidate=self.call('list_source_media',limit=50).payload['sources']
        candidate=next(r for r in candidate if r['kind']=='video')
        self.repository.cancel(self.project.project_id,self.run.run_id)
        run,_=self.repository.create_run(self.project.project_id,'long-form',production={'skill_id':'travel-program','intent':'十分鐘完整影片','target_duration_seconds':600})
        long=EditAgentTools(self.repository,self.project.project_id,run.run_id);long.activate()
        try:
            self.assertEqual(limits_for(long).duration_seconds,660)
            self.assertEqual(limits_for(long).clips,180)
            candidate=long.call('register_source_range',{'version_id':candidate['version_id'],'stream_index':0,'start_offset_seconds':0,'duration_seconds':12,'description':'合成素材邊界測試'}).payload['candidate']['candidate_id']
            revision=self.repository.current_revision(self.project.project_id,run.run_id).revision_id
            clips=[{'candidate_id':candidate,'start_offset_seconds':0,'duration_seconds':12}]*50
            long.call('replace_sequence',{'clips':clips,'reprise_reason':'合成來源的長片政策邊界測試，非成片剪輯建議','summary':'驗證十分鐘可建立','expected_revision_id':revision,'mutation_id':'long-boundary'})
            timeline=long._timeline()
            self.assertEqual(timeline.duration_frames,18000);self.assertEqual(len(long._video().items),50)
            from dataclasses import replace
            from clipen.editing.production_limits import valid_production
            short=replace(timeline,duration_frames=1080,tracks=(replace(timeline.tracks[0],items=timeline.tracks[0].items[:3]),))
            self.assertTrue(valid_production(long,short))
            self.assertFalse(valid_production(long,short,complete=True))
            revision=self.repository.current_revision(self.project.project_id,run.run_id).revision_id
            with self.assertRaises(ProjectError):
                long.call('replace_sequence',{'clips':clips+clips[:6],'reprise_reason':'測試超出本次長片上限','summary':'必須拒絕','expected_revision_id':revision,'mutation_id':'too-long'})
            self.assertEqual(long._timeline(),timeline)

            self.assertEqual(self.repository.production_session(self.project.project_id,run.run_id)['intent']['target_duration_seconds'],600)
            repeated,created=self.repository.create_run(self.project.project_id,'long-form',production={'skill_id':'travel-program','intent':'十分鐘完整影片','target_duration_seconds':600})
            self.assertFalse(created);self.assertEqual(repeated.run_id,run.run_id)
            with self.assertRaises(ProjectError):self.repository.create_run(self.project.project_id,'long-form',production={'skill_id':'travel-program','intent':'十分鐘完整影片','target_duration_seconds':60})
        finally:long.close_production()
        # Prior frozen sessions remain short even after the API gains long form.
        self.assertEqual(limits_for(self.tools).duration_seconds,75)
        for value in (True,0,1201,600.5):
            with self.assertRaises(ProjectError):self.repository.create_run(self.project.project_id,'invalid-duration-'+str(value),production={'skill_id':'travel-program','intent':'片長檢查','target_duration_seconds':value})

    def test_overlapping_speech_windows_choose_one_complete_evidence_without_duplicate_captions(self):
        from clipen.projects.storage import utc_now
        self.cut();clip=self.tools._video().items[0]
        def add(identifier,text,aligned,correction=False):
            segment={'text':text,'start_seconds':.2 if identifier!='old' else 0,'end_seconds':3.8}
            if aligned:segment['words']=[{'text':text,'char_start':0,'char_end':len(text),'start_seconds':1,'end_seconds':2}]
            if correction:segment['wording_authority']='model_proposed_correction'
            value={'source':clip.source.span.to_storage(),'source_start_seconds':1,'segments':[segment]}
            with self.projects.connect(self.project.project_id) as db:
                db.execute('INSERT INTO production_evidence_jobs VALUES(?,?,?,?,?,?,?,?,?)',(identifier,self.run.run_id,identifier,'align','succeeded','{}',json.dumps(value),None,utc_now()))
        add('old','舊字幕',True);add('failed-alignment','未對齊的新視窗',False)
        self.assertEqual([c['text'] for c in self.call('list_transcripts',include_drafts=True).payload['cues']],['舊字幕'])
        add('aligned-new-window','新字幕',True)
        self.assertEqual([c['text'] for c in self.call('list_transcripts',include_drafts=True).payload['cues']],['新字幕'])
        add('explicit-wording-correction','更正原話',False,True)
        self.assertEqual([c['text'] for c in self.call('list_transcripts',include_drafts=True).payload['cues']],['更正原話'])

    def test_caption_fragments_join_without_moving_word_boundaries(self):
        from clipen.editing.program_subtitles import aligned_pages
        from clipen.editing.alignment import significant
        text='買什麼？我們剛剛買到安全帽了。'
        words=[]
        for n,index in enumerate(i for i,c in enumerate(text) if significant(c)):
            start=n*2+(30 if n>=4 else 0)
            words.append({'text':text[index],'char_start':index,'char_end':index+1,'start_frame':start,'end_frame':start+2})
        pages=aligned_pages({'text':text,'words':words},text,None,[(0,len(text))],30)
        self.assertEqual(pages,[(0,len(text),words[0]['start_frame'],words[-1]['end_frame'])])

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
    def test_local_wording_correction_does_not_realign_unrelated_failed_segment(self):
        self.real_media();self.cut()
        original='大家河濱公園。';corrected='台場海濱公園。';noise='遠方雜音。'
        class Transcriber:
            def transcribe(self,audio,output,**kwargs):
                return TranscriptionResult('zh',original+noise,(TranscriptSegment(0,64000,original,None,()),TranscriptSegment(64000,128000,noise,None,())))
            def close(self):pass
        self.tools.transcriber_binding=lambda:(Transcriber,{'id':'test-local','revision':'fixed'})
        def initial(_root,_audio,text,*args):
            if text==noise:raise ProjectError('alignment_invalid','未使用的背景片段')
            return [{'text':text,'start':.2,'end':1.4}]
        with patch('clipen.editing.alignment.run_alignment',side_effect=initial):
            job=self.call('transcribe_source_range',candidate_id=self.candidate).payload['job']
            before=self.call('read_evidence_job',job_id=job['job_id'],wait_seconds=10).payload['job']
        self.assertEqual(before['state'],'succeeded')
        def only_changed(_root,_audio,text,*args):
            self.assertEqual(text,corrected)
            return [{'text':text,'start':.2,'end':1.4}]
        with patch('clipen.editing.alignment.run_alignment',side_effect=only_changed):
            changed=self.call('revise_source_speech',transcript_job_id=job['job_id'],segment_ordinal=0,text=corrected,evidence='來源畫面的地名指示牌').payload['job']
            after=self.call('read_evidence_job',job_id=changed['job_id'],wait_seconds=10).payload['job']
        self.assertEqual(after['state'],'succeeded',after)
        self.assertEqual(after['result']['segments'][0]['source_asr_text'],original)
        self.assertEqual(after['result']['segments'][1],before['result']['segments'][1])
        self.assertEqual(self.call('read_evidence_job',job_id=job['job_id']).payload['job']['result']['segments'][0]['text'],original)

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

    def test_every_mcp_tool_schema_is_valid_json_schema_2020_12(self):
        # Claude's API rejects the whole request for one invalid tool schema.
        from jsonschema import Draft202012Validator
        from clipen.mcp_editing import EditingMcpServer
        listed=EditingMcpServer(self.tools).handle({'jsonrpc':'2.0','id':1,'method':'tools/list'})['result']['tools']
        for tool in listed:
            with self.subTest(tool=tool['name']):
                Draft202012Validator.check_schema(tool['inputSchema'])
                required=tool['inputSchema'].get('required',[])
                self.assertEqual(len(required),len(set(required)))

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
            payload={'request_id':'api-production','consent':False,'skill_id':'travel-program','intent':'從來源直接製作節目','visual_style_id':'editorial','font_style_id':'street'}
            route=f'/api/projects/{self.project.project_id}/production/commands/start'
            self.assertEqual(request('POST',route,payload,mutation)[0],422)
            code,_,body=request('POST',route,{**payload,'consent':True},mutation)
            self.assertEqual(code,202,body)
            code,_,body=request('GET',f'/api/projects/{self.project.project_id}/edit-agent',headers={'cookie':cookie})
            self.assertEqual(code,200,body)
            self.assertEqual(json.loads(body)['production']['skill']['skill_id'],'travel-program')
            self.assertEqual(json.loads(body)['production']['skill']['selected_visual_style_id'],'editorial')
            self.assertEqual(json.loads(body)['production']['skill']['selected_font_style_id'],'street')
            font_code,_,font_body=request('GET','/api/production/skills/travel-program/fonts/street/preview',headers={'cookie':cookie})
            self.assertEqual(font_code,200);self.assertTrue(font_body.startswith(b'\x89PNG'))
            self.assertEqual(request('GET','/api/production/skills/travel-program/fonts/unknown/preview',headers={'cookie':cookie})[0],404)
            image_code,image_headers,image_body=request('GET','/api/production/skills/travel-program/styles/editorial/preview',headers={'cookie':cookie})
            self.assertEqual(image_code,200)
            self.assertTrue(image_body.startswith(b'\x89PNG'))
            self.assertEqual(request('GET','/api/production/skills/travel-program/styles/unknown/preview',headers={'cookie':cookie})[0],404)
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

    def add_subtitle(self,text,start_frame,duration_frames):
        from dataclasses import replace
        from clipen.domain import Track
        timeline=self.tools._timeline()
        caption=TextClip(f'subtitle-{start_frame}',start_frame,duration_frames,text,'title.caption')
        existing=tuple(i for t in timeline.tracks if t.track_id=='agent-subtitles' for i in t.items)
        tracks=tuple(t for t in timeline.tracks if t.track_id!='agent-subtitles')+(Track('agent-subtitles',TrackKind.TITLE,existing+(caption,)),)
        self.tools._expected_revision_id=None
        self.tools._save(replace(timeline,tracks=tracks),'測試字幕')

    def test_components_need_a_chosen_place_real_information_and_a_short_beat(self):
        self.cut()
        def rejected(code,**args):
            with self.assertRaises(ProjectError) as error:self.call('add_program_component',**{
                'element_id':'probe','item_id':'agent-video-0001','start_offset_frames':6,'duration_frames':45,
                'evidence':'測試來源','summary':'測試','x':.3,'y':.3,**args})
            self.assertEqual(error.exception.code,code)
        with self.assertRaises(ProjectError) as error:
            self.call('add_program_component',element_id='probe',item_id='agent-video-0001',component_id='headline',
                      start_offset_frames=6,duration_frames=45,text='意外的第三碗',evidence='測試來源',summary='未指定位置')
        self.assertEqual(error.exception.code,'component_position_required')
        rejected('component_fact_without_information',component_id='fact',text='熱熱上桌')
        rejected('component_reaction_too_long',component_id='impact',text='有點難度',duration_frames=90)
        self.component(element_id='price',text='拉麵 1200 円',x=.5,y=.12)
        self.component(element_id='beat',component_id='impact',text='有點難度',duration_frames=45,start_offset_frames=60,x=.4,y=.3)
        self.assertTrue(any(i.item_id=='beat' for t in self.tools._timeline().tracks for i in t.items))
        # The user's own workbench edit is not second-guessed by the model's editorial rules.
        self.repository.cancel(self.project.project_id,self.run.run_id)
        self.tools._user_edit=True
        try:self.call('add_program_component',element_id='mine',item_id='agent-video-0002',component_id='fact',start_offset_frames=6,
                      duration_frames=45,text='熱熱上桌',evidence='使用者自己加入',summary='使用者標註')
        finally:self.tools._user_edit=False
        self.assertTrue(any(i.item_id=='mine' for t in self.tools._timeline().tracks for i in t.items))

    def test_components_must_not_restate_concurrent_subtitles(self):
        from clipen.editing.component_review import restated_ratio
        self.assertEqual(restated_ratio('還有三顆蛋',['而且我還有三顆蛋哦。']),1)
        self.assertLess(restated_ratio('兩個人，三碗麵？',['我們手滑點到了三碗拉麵']),.5)
        self.cut()
        self.add_subtitle('這是我們看的第一個不用錢的夜景。',0,60)
        with self.assertRaises(ProjectError) as error:
            self.component(element_id='copy',component_id='headline',text='不用錢的夜景',x=.08,y=.25)
        self.assertEqual(error.exception.code,'component_restates_subtitle')
        self.component(element_id='later',component_id='headline',text='第一次的夜景',start_offset_frames=70,duration_frames=40,x=.08,y=.25)
        self.add_subtitle('第一次的夜景真的很漂亮',60,60)
        validation=self.call('validate_sequence').payload
        self.assertIn('component_restates_subtitle',[i['code'] for i in validation['delivery_issues']])

    def test_design_notes_flag_template_rhythm_without_blocking(self):
        from clipen.domain import Rational, Track
        from clipen.editing.component_review import design_notes
        timeline=EditTimeline('t','fresh-project',None,None,None,Rational(30,1),640,360,0,(Track('video-main',TrackKind.VIDEO,()),),production_session_id='s')
        items=[TextClip(f'c{n}',n*300,120,text,f's{n}') for n,text in enumerate(['熱熱上桌','選擇，從細節開始','親手試一試','接下來，交給店員'])]
        styles={f's{n}':{'component_id':'fact','x':.07,'y':.1+n*.005} for n in range(4)}
        codes=[n['code'] for n in design_notes(timeline,items,styles)]
        self.assertIn('component_position_repeated',codes)
        self.assertIn('component_duration_uniform',codes)
        self.assertEqual(codes.count('component_parallel_phrasing'),2)
        varied=[TextClip(f'c{n}',n*300,d,'拉麵 1200 円',f's{n}') for n,d in enumerate((30,45,90,150))]
        spread={f's{n}':{'component_id':'fact','x':x,'y':y} for n,(x,y) in enumerate(((.07,.1),(.5,.4),(.2,.6),(.6,.1)))}
        self.assertEqual(design_notes(timeline,varied,spread),[])
        self.cut();self.component()
        validation=self.call('validate_sequence').payload
        self.assertIn('design_notes',validation)
        self.assertFalse(any(i['code'].startswith('component_') and i['code']!='component_restates_subtitle' for i in validation['issues']))

    def test_component_safety_and_actual_layout_overflow(self):
        self.cut()
        for args in ({'start_offset_frames':110,'duration_frames':20}, {'component_id':'pointer'},
                     {'x':.9}, {'text':'很長的價格和品名資訊'*4}):
            with self.assertRaises((ProjectError,RuntimeError)):self.component(**args)
        self.component(component_id='pointer',target={'x':.7,'y':.45,'radius':.04})
        self.assertEqual(self.call('list_program_components').payload['tracking'],'static_target_only')

    def test_simultaneous_graphics_keep_separate_lanes_through_update_recut_and_remove(self):
        self.cut()
        self.component(element_id='identity',component_id='program_title',text='節目主題',y=.10)
        self.component(element_id='speaker',component_id='person',text='現場人物',y=.58)
        self.assertEqual(self.tools._component_issues(),[])
        args={'element_id':'identity','item_id':'agent-video-0001','component_id':'program_title',
              'start_offset_frames':6,'duration_frames':45,'text':'新版節目主題',
              'y':.10,'evidence':'編輯命名，非人物引言','summary':'修改主標'}
        self.call('update_program_component',**args)
        self.call('reorder_clips',item_ids=['agent-video-0002','agent-video-0001'],summary='交換鏡頭')
        elements=[i for t in self.tools._timeline().tracks if t.track_id.startswith('program-components') for i in t.items]
        self.assertEqual({i.item_id for i in elements},{'identity','speaker'})
        self.assertTrue(all(i.start_frame==126 for i in elements))
        self.assertEqual(self.tools._component_issues(),[])
        self.call('remove_program_component',element_id='identity',summary='移除主標保留人物資訊')
        validation=self.call('validate_sequence').payload
        self.assertFalse(any(i['code'] in {'program_graphics_missing','title_cards_disabled'} for i in validation['issues']))
        self.assertEqual(self.tools._component_issues(),[])

    def test_motion_families_are_executable_frozen_and_keep_layout_guards(self):
        from PIL import Image, ImageChops, ImageDraw
        from clipen.rendering.program_components import animation_boundaries, component_bounds, draw_program_component
        from clipen.rendering.program_motion import FAMILIES
        from clipen.rendering.titles import TitleCardRenderer
        from clipen.editing.production_media import load_text_styles
        self.cut()
        for kind in FAMILIES:
            with self.subTest(kind=kind):
                self.component(component_id=kind,element_id=kind,text='節目主題',detail='設計預覽')
                element=next(i for t in self.tools._timeline().tracks for i in t.items if i.item_id==kind)
                profile=load_text_styles(self.projects,self.project.project_id)[element.style_id]
                images=[]
                for frame in (0,8,28):
                    image=Image.new('RGBA',(640,360))
                    draw_program_component(ImageDraw.Draw(image),TitleCardRenderer._font_path(),self.tools._timeline(),element,profile,frame)
                    images.append(image)
                self.assertIsNotNone(ImageChops.difference(images[0],images[1]).getbbox())
                self.assertIsNotNone(ImageChops.difference(images[1],images[2]).getbbox())
                self.assertIn(element.start_frame+16,animation_boundaries(element,profile))
                bounds,_,_=component_bounds(ImageDraw.Draw(images[0]),TitleCardRenderer._font_path(),self.tools._timeline(),element,profile)
                self.assertLessEqual(bounds[2],640*.945)
                self.assertLessEqual(bounds[3],360*.77)
                asset=self.call('read_production_skill',resource='assets/'+kind+'.png')
                self.assertTrue(asset.images)
        self.assertTrue(any(i['code']=='program_component_collision' for i in self.tools._component_issues()))
        with self.assertRaises(ProjectError):self.component(component_id='program_title',element_id='unsafe',y=.68,text='節目主題')
        frozen=self.tools._skill_value()
        self.tools._session_cache={**self.tools._session_value(),'skill':{**frozen,'components':[
            c for c in frozen['components'] if c['component_id'] not in FAMILIES]}}
        with self.assertRaises(ProjectError) as error:self.component(component_id='headline',element_id='old-session',text='主題')
        self.assertEqual(error.exception.code,'component_unavailable')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
    def test_all_new_families_animate_in_actual_video_and_exit(self):
        from PIL import ImageChops
        self.real_media();self.cut()
        for kind,shot,offset,duration in [('program_title',1,0,55),('headline',1,60,55),
                                         ('impact',2,0,35),('chapter',2,40,35),('burst',2,80,35)]:
            self.component(component_id=kind,element_id=kind,item_id=f'agent-video-{shot:04d}',
                           start_offset_frames=offset,duration_frames=duration,text='節目主題')
        self.assertEqual(self.tools._component_issues(),[])
        render=self.call('render_preview').payload['render']
        offsets=[.03,.90,1.94,2.9,4.65,5.98,7.30,7.90]
        inspected=self.call('inspect_preview',render_id=render['render_id'],offsets_seconds=offsets,wait_seconds=15)
        self.assertEqual(inspected.payload['render']['state'],'succeeded',inspected.payload)
        from PIL import Image
        frames=[Image.open(p).convert('RGB') for p in inspected.images]
        for n in (0,1,3,4,5,6):
            self.assertIsNotNone(ImageChops.difference(frames[n],frames[2]).crop((30,20,610,270)).getbbox())
        self.assertGreater(sum(ImageChops.difference(frames[0],frames[1]).resize((1,1)).getpixel((0,0))),1)
        self.assertLess(sum(ImageChops.difference(frames[2],frames[-1]).resize((1,1)).getpixel((0,0))),8)

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
