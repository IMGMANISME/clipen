"""Production tools exercised through real dispatch and real local renders; no AI calls."""
from dataclasses import replace
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from clipen.domain import EffectRequest, MediaClip, TextClip, Track, TrackKind
from clipen.editing.production_catalog import EXTRA_TOOLS
from clipen.editing.production_media import AgentAudioStore, transcript_cues
from clipen.media import MediaPreview
from clipen.projects import ProjectError
from clipen.rendering import FfmpegPreviewRenderer
from clipen.mcp_editing.server import _TOOLS, EditingMcpServer
from clipen.providers.codex_editing import _MCP_TOOLS
import test_edit_agent as fixtures
import test_finishing as evidence_fixtures


class ProductionToolsTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.EditAgentTests();self.fixture.setUp()
        self.projects=self.fixture.projects;self.project=self.fixture.project
        self.run,self.tools=self.fixture.create_tools(calls=120)
        self.tools.call('replace_sequence',{'clips':[{'candidate_id':'candidate-street','start_offset_seconds':o,'duration_seconds':4} for o in (0,.5,1,1.5,2,2.5,3,4)],'summary':'先建立可看的粗剪'})
        timeline=self.tools._timeline()
        self.fixture.repository.save_revision(self.project.project_id,self.run.run_id,replace(timeline,width=320,height=180),'使用小尺寸測試畫布')

    def tearDown(self):
        self.tools.close_production();self.fixture.tearDown()

    def call(self,name,**args):return self.tools.call(name,args)

    def test_catalog_capabilities_and_every_tool_is_registered_with_provider(self):
        names={t['name'] for t in _TOOLS}
        self.assertEqual(names,set(_MCP_TOOLS))
        for tool in EXTRA_TOOLS:self.assertTrue(callable(getattr(self.tools,'_production_'+tool['name'])))
        result=self.call('list_editing_tools')
        self.assertEqual(result.payload['decision_owner'],'LLM')
        self.assertIn('automatic_object_tracking',result.payload['unsupported'])
        self.assertNotIn(str(self.projects.root),json.dumps(result.payload))
        paged=self.call('search_media',query='',limit=1,offset=1)
        self.assertEqual(paged.payload['returned'],0)

    def test_agent_can_revisit_full_source_outside_the_original_candidate_windows(self):
        from clipen.media import InventoryRepository,InventoryState,AssetCandidate,AssetKind,ItemStatus,ProbeStatus,MediaStream
        from clipen.projects import DirectoryReference,DirectoryRole
        from clipen.editing import EditAgentTools
        source=Path(self.fixture.temporary.name)/'source';source.mkdir();info=source.stat()
        project=self.projects.set_directory(self.project.project_id,'grant-source','a'*64,self.project.revision,
            DirectoryReference(DirectoryRole.SOURCE,str(source),str(source),'source',info.st_dev,info.st_ino,'2026-10-17T00:00:00+00:00'))
        inventory=InventoryRepository(self.projects);run=inventory.create_run(project.project_id,'inventory-raw',project.revision)
        inventory.transition(project.project_id,run.run_id,InventoryState.RUNNING,phase='probing')
        item=AssetCandidate(DirectoryRole.SOURCE,'raw.mp4',AssetKind.VIDEO,ItemStatus.USABLE,None,1024,123456,'b'*64,ProbeStatus.READY,
                            {'duration_seconds':'20'},(MediaStream(0,'video','h264',{'start_pts':'90000','duration_ts':'1800000','time_base':'1/90000','width':320,'height':180}),))
        inventory.stage_items(project.project_id,run.run_id,(item,));inventory.publish(project.project_id,run.run_id)
        listed=self.call('list_source_media',limit=1).payload['sources']
        self.assertEqual(len(listed),1);self.assertNotIn(str(source),json.dumps(listed))
        candidate=self.call('register_source_range',version_id=listed[0]['version_id'],stream_index=0,start_offset_seconds=.25,duration_seconds=8,description='LLM 重新檢視的原片範圍').payload
        identifier=candidate['candidate']['candidate_id']
        self.assertEqual(candidate['source']['start_pts'],'112500')
        self.assertEqual(candidate['authority'],'model_authored_observation')
        with self.assertRaises(ProjectError):self.call('register_source_range',version_id=listed[0]['version_id'],stream_index=0,start_offset_seconds=19,duration_seconds=8,description='超出來源')
        self.call('record_story_plan',intent='讓事件順序更清楚',sections=[{'purpose':'從抵達到探索','candidate_ids':[identifier],'treatment':'使用原音帶出現場感'}],uncertainties=['尚未檢視完整動作'])
        self.assertEqual(self.call('read_story_plan').payload['story_plan']['authority'],'model_authored')
        self.call('replace_sequence',clips=[{'candidate_id':identifier,'start_offset_seconds':n*.25,'duration_seconds':4} for n in range(8)],summary='使用原本候選以外的片段重剪')
        self.assertTrue(all(i.source.span.version_id==listed[0]['version_id'] for i in self.tools._video().items))
        source={'version_id':listed[0]['version_id'],'stream_index':0,'start_pts':'90000','end_pts':'1170000','time_base':{'num':1,'den':90000}}
        self.call('replace_clip_source',item_id='agent-video-0001',source=source,summary='擴回十二秒完整口語來源')
        self.assertEqual(self.tools._video().items[0].source.span.start_pts,'90000')
        self.assertEqual(self.tools._video().items[0].duration_frames,360)
        with self.assertRaises(ProjectError):self.call('replace_clip_source',item_id='agent-video-0001',source={**source,'end_pts':'999999999'},summary='不能越出原片')
        reloaded=EditAgentTools(self.fixture.repository,project.project_id,self.run.run_id,planning=fixtures.StaticPlanningInput(self.fixture.plan,self.fixture.context),artifacts=fixtures.EmptyArtifacts())
        self.assertEqual(reloaded._candidate(identifier).source.start_pts,'112500')

    def test_mutations_are_real_undoable_and_recut_explicitly_invalidates_timing(self):
        original=self.tools.repository.current_revision(self.project.project_id,self.run.run_id)
        self.call('set_color_grade',item_id='agent-video-0001',contrast=1.2,saturation=.9,brightness=.02,summary='調整開場曝光')
        self.call('set_source_audio',item_id='agent-video-0001',mode='dialogue',gain_db=-3,summary='讓人聲清楚')
        self.call('set_mosaic',item_id='agent-video-0001',regions=[{'start_frame':0,'duration_frames':30,'x':.1,'y':.1,'width':.2,'height':.2,'block_size':12}],summary='遮住指定區域')
        self.call('set_transition',item_id='agent-video-0002',kind='dip_black',duration_frames=12,summary='切換場景')
        effects=self.tools._video().items[0].effects
        self.assertEqual({e.effect_id for e in effects},{'video.grade','audio.source','video.mosaic'})
        with self.assertRaises(ProjectError):self.call('set_mosaic',item_id='agent-video-0001',regions=[{'start_frame':0,'duration_frames':300,'x':.9,'y':.1,'width':.2,'height':.2,'block_size':12}],summary='超界')
        self.call('remove_clip_effect',item_id='agent-video-0001',effect_id='video.mosaic',summary='取消遮罩')
        self.assertNotIn('video.mosaic',{e.effect_id for e in self.tools._video().items[0].effects})
        changed=self.call('trim_clip',item_id='agent-video-0001',start_offset_frames=15,duration_frames=90,summary='移除晃動畫面')
        self.assertEqual(changed.payload['invalidated'],[])
        self.assertEqual({e.effect_id for e in self.tools._video().items[0].effects},{'video.grade','audio.source'})
        self.assertEqual(self.tools._timeline().duration_frames,930)
        self.assertEqual(self.tools._video().items[0].source.span.start_pts,'45000')
        self.call('split_clip',item_id='agent-video-0001',offset_frames=45,summary='拆開兩個動作')
        self.assertEqual(len(self.tools._video().items),9)
        ids=[i.item_id for i in self.tools._video().items]
        self.call('reorder_clips',item_ids=ids[::-1],summary='重排敘事')
        self.assertEqual(self.tools._video().items[0].item_id,ids[-1])
        self.call('insert_clip',candidate_id='candidate-street',start_offset_seconds=5,duration_seconds=2,before_item_id=None,summary='補入環境鏡頭')
        self.assertEqual(len(self.tools._video().items),10)
        self.call('remove_item',item_id=self.tools._video().items[-1].item_id,summary='移除補拍鏡頭')
        self.call('restore_revision',revision_id=original.revision_id,summary='還原粗剪')
        self.assertEqual(self.tools._timeline(),original.timeline)
        history=self.call('get_edit_history').payload
        self.assertTrue(any(e['event_type']=='tool_failed' for e in history['events']))
        self.assertTrue(self.call('validate_sequence').payload['valid'])
        exported=self.call('export_edit').payload
        self.assertEqual(exported['format'],'clipen.native.v1')
        path=self.projects.root/self.project.project_id/'agent-production'/self.run.run_id/'exports'/(original.revision_id+'.json')
        self.assertEqual(json.loads(path.read_text())['timeline'],original.timeline.to_payload())

    def test_shot_bound_cards_and_final_fade_have_precise_semantics(self):
        first=self.tools._video().items[0];last=self.tools._video().items[-1]
        with self.assertRaises(ProjectError):self.call('add_clip_title',item_id=first.item_id,start_offset_frames=100,duration_frames=30,text='地點',evidence='來源招牌',style_id='title.chapter',summary='不能跨到下個店面')
        self.call('add_clip_title',item_id=last.item_id,start_offset_frames=0,duration_frames=30,text='現場',evidence='目前鏡頭實際顯示的環境',style_id='title.chapter',summary='文字與鏡頭對齊')
        title=next(t for t in self.tools._timeline().tracks if t.kind is TrackKind.TITLE).items[0]
        self.assertEqual(title.start_frame,last.start_frame)
        self.call('set_fade',item_id=last.item_id,fade_in_frames=0,fade_out_frames=24,summary='最後一鏡真正淡出')
        from clipen.rendering.production_effects import transition_filters
        chain=transition_filters(self.tools._video().items[-1],None,self.tools._timeline())
        self.assertIn('fade=t=out:st=3.200000000:d=0.800000000',chain)
        self.assertNotIn('fade=t=in',chain)
        with self.assertRaises(ProjectError):self.call('set_fade',item_id=first.item_id,fade_in_frames=100,fade_out_frames=100,summary='不能超出鏡頭')

    def test_review_cannot_clear_known_problems_by_rewording_the_same_revision(self):
        shots=[{'item_id':i.item_id,'observation':'需要移除未完成的動作','decision':'keep'} for i in self.tools._video().items]
        status={'render_coverage_complete':True,'unresolved':[shots[0]['item_id']]}
        with patch('clipen.editing.production_preview.finish_review_status',return_value=status):
            with self.assertRaises(ProjectError) as error:self.call('review_cut',shots=shots,text_checks=[],story_assessment='只是改評語沒有改影片',audio_limitations='未聆聽')
        self.assertEqual(error.exception.code,'agent_revision_required')

    def test_only_changed_shots_need_new_stills_after_an_accepted_review(self):
        from clipen.editing.editorial_review import carried_reviews, coverage_targets
        from clipen.editing.production_preview import _event, finish_review_status, render_recipe_id
        recipe=render_recipe_id()
        first=self.fixture.repository.current_revision(self.project.project_id,self.run.run_id)
        clips=list(self.tools._video().items)
        _event(self.tools,'editorial_review',{'revision_id':first.revision_id,'recipe_id':recipe,'authority':'model_authored',
            'shots':[{'item_id':c.item_id,'observation':'ok','decision':'revise' if c is clips[-1] else 'keep'} for c in clips],'text_checks':[]})
        # Shorten the second shot: every later shot moves, but only the trimmed one changed on screen.
        self.call('trim_clip',item_id=clips[1].item_id,start_offset_frames=0,duration_frames=clips[1].duration_frames-6,summary='縮短第二鏡')
        second=self.fixture.repository.current_revision(self.project.project_id,self.run.run_id)
        carried,_=carried_reviews(self.tools,second,recipe)
        self.assertNotIn(clips[1].item_id,carried)
        # A shot left as "revise" is never carried, even when unchanged.
        self.assertNotIn(clips[-1].item_id,carried)
        self.assertEqual(set(carried),{c.item_id for c in clips}-{clips[1].item_id,clips[-1].item_id})
        status=finish_review_status(self.tools)
        self.assertEqual(set(status['missing_shots']),{clips[1].item_id,clips[-1].item_id})
        self.assertEqual(set(status['carried_shots'])|set(status['missing_shots']),set(coverage_targets(second.timeline)))

    def test_background_and_foreground_events_cannot_allocate_the_same_sequence(self):
        from concurrent.futures import ThreadPoolExecutor
        from clipen.projects.storage import utc_now
        barrier=threading.Barrier(4)
        def writer(n):
            with self.projects.connect(self.project.project_id) as db:
                barrier.wait()
                for value in range(15):self.fixture.repository._append_event(db,self.run.run_id,'concurrent-test',{'writer':n,'value':value},utc_now())
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(writer,range(4)))
        with self.projects.connect(self.project.project_id) as db:
            rows=db.execute("SELECT sequence FROM edit_agent_events WHERE run_id=? AND event_type='concurrent-test' ORDER BY sequence",(self.run.run_id,)).fetchall()
        self.assertEqual(len(rows),60)
        self.assertEqual([r['sequence'] for r in rows],list(range(rows[0]['sequence'],rows[0]['sequence']+60)))

    def test_subtitles_are_grounded_and_independent_of_titles(self):
        cue={'cue_id':'real-cue','start_frame':30,'duration_frames':45,'text':'我們抵達臺北了'}
        with patch('clipen.editing.production_media.transcript_cues',return_value=([cue],[])):
            with self.assertRaises(ProjectError):self.call('replace_subtitles',captions=[{'cue_id':'invented','text':'假的口語'}],summary='不應通過')
            self.call('replace_subtitles',captions=[{'cue_id':'real-cue','text':'我們抵達臺北了'}],summary='加入真實口語字幕')
        self.call('add_title',start_frame=0,duration_frames=30,text='抵達',style_id='title.chapter',summary='加入地點字卡')
        self.assertEqual(len([t for t in self.tools._timeline().tracks if t.kind is TrackKind.TITLE]),2)
        self.call('edit_text',item_id='agent-title-0001',start_frame=0,duration_frames=30,text='臺北',style_id='title.chapter',summary='修正地點')
        subtitle=next(t for t in self.tools._timeline().tracks if t.track_id=='agent-subtitles')
        self.assertEqual(subtitle.items[0].text,'我們抵達臺北了')
        self.call('remove_item',item_id='agent-title-0001',summary='移除字卡')
        self.assertTrue(any(t.track_id=='agent-subtitles' for t in self.tools._timeline().tracks))

    def test_malformed_arguments_and_foreign_previews_do_not_mutate_timeline(self):
        original=self.tools._timeline()
        server=EditingMcpServer(self.tools)
        response=server.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'set_mosaic','arguments':{'item_id':'agent-video-0001','regions':'shell','summary':'錯誤'}}})
        self.assertTrue(response['result']['isError']);self.assertEqual(self.tools._timeline(),original)
        with self.assertRaises(ProjectError):self.call('inspect_preview',render_id='../../outside',offsets_seconds=[0])
        with self.assertRaises(ProjectError):self.call('restore_revision',revision_id='foreign',summary='錯誤')
        with self.assertRaises(ProjectError) as error:self.call('finish_edit',summary='沒有觀看不能宣稱完成')
        self.assertEqual(error.exception.code,'agent_preview_review_required')

    def test_custom_text_style_controls_actual_raster_and_is_exported(self):
        from clipen.rendering.titles import TitleCardRenderer
        from clipen.editing.production_media import load_text_styles
        from PIL import Image
        self.call('add_title',start_frame=0,duration_frames=60,text='東京',style_id='title.chapter',summary='加入字卡')
        profile={'item_id':'agent-title-0001','font_size_ratio':.08,'x':.1,'y':.4,'width_ratio':.7,'align':'center',
                 'text_color':[255,30,30,255],'background_color':[0,0,0,0],'summary':'依畫面安排字體與位置'}
        self.call('configure_text_style',**profile)
        styles=load_text_styles(self.projects,self.project.project_id)
        directory=Path(self.fixture.temporary.name)/'custom-style';directory.mkdir()
        titles=TitleCardRenderer(styles).render(self.tools._timeline(),directory)
        image=Image.open(titles[0].path).convert('RGBA')
        colored=[(n%320,n//320) for n,p in enumerate(image.get_flattened_data()) if p[0]>200 and p[1]<70 and p[3]>180]
        self.assertGreater(len(colored),10)
        self.assertGreater(min(y for x,y in colored),60);self.assertLess(max(y for x,y in colored),110)
        result=self.call('export_edit').payload
        path=self.projects.root/self.project.project_id/'agent-production'/self.run.run_id/'exports'/(result['export_id']+'.json')
        self.assertEqual(json.loads(path.read_text())['text_styles'],styles)
        with self.assertRaises(ProjectError):self.call('configure_text_style',**{**profile,'x':.9})

    def test_failed_renders_spend_only_local_preview_budget_and_can_be_retried(self):
        class Failure:
            def render(self,*args):raise ProjectError('disk_unavailable','local failure')
        self.tools.preview_renderer=Failure()
        for count in (1,2):
            started=self.call('render_preview').payload
            result=self.call('inspect_preview',render_id=started['render']['render_id'],offsets_seconds=[],wait_seconds=1)
            self.assertEqual(result.payload['render']['state'],'failed')
            self.assertEqual(result.payload['budget']['preview_renders'],count)
        with self.assertRaises(ProjectError) as error:self.call('render_preview')
        self.assertEqual(error.exception.code,'preview_budget_exhausted')
        self.assertEqual(self.tools.repository.get(self.project.project_id,self.run.run_id).state,'running')

    def test_cancel_preview_does_not_cancel_or_mutate_the_edit(self):
        started_event=threading.Event()
        class Blocking:
            def render(self,project_id,timeline,output,cancel,progress):
                started_event.set();cancel.wait(5)
                if cancel.is_set():raise ProjectError('user_cancelled','cancelled')
        self.tools.preview_renderer=Blocking();original=self.tools._timeline()
        started=self.call('render_preview').payload
        self.assertTrue(started_event.wait(1))
        self.call('cancel_preview',render_id=started['render']['render_id'])
        result=self.call('inspect_preview',render_id=started['render']['render_id'],offsets_seconds=[],wait_seconds=1)
        self.assertEqual(result.payload['render']['state'],'cancelled')
        self.assertEqual(self.tools._timeline(),original)

    def test_music_search_sends_only_generic_tags_and_retains_source_license(self):
        candidate=evidence_fixtures.fixture()[1]['assets'][0]
        self.tools._asset_store().add([{**candidate,'asset_id':'older-track','name':'先前搜尋的舊歌'}],{'canonical_path':str(self.projects.root)})
        def prepare(brief,directory,cancel,progress):
            self.assertEqual(brief,{'music_tags':['ambient','piano']})
            directory.mkdir()
            return [candidate],{'canonical_path':str(directory)}
        with patch('clipen.finishing.catalog.PublicMusicCatalog.search_and_prepare',side_effect=prepare):
            result=self.call('search_music',tags=['ambient','piano'])
        self.assertEqual(result.payload['assets'][0]['provenance']['license'],'cc0')
        self.assertEqual(len(result.payload['assets']),1)
        self.assertEqual(result.payload['other_available_asset_ids'],['older-track'])
        self.assertNotIn(str(self.projects.root),json.dumps(result.payload))

    def test_native_export_download_requires_session_and_run_owned_revision(self):
        from clipen.controller import ControllerConfig,LocalSessionAuthority,ProviderStatus,ProviderState,create_app
        from clipen.editing import EditAgentCoordinator
        from test_project_api import request_app
        result=self.call('export_edit').payload
        root=Path(self.fixture.temporary.name);attempts=root/'attempts';attempts.mkdir();static=root/'static';static.mkdir();(static/'index.html').write_text('test')
        coordinator=EditAgentCoordinator(self.fixture.repository,attempts)
        path=coordinator.export_path(self.project.project_id,self.run.run_id,result['export_id'])
        self.assertEqual(json.loads(path.read_text())['format'],'clipen.native.v1')
        with self.assertRaises(ProjectError):coordinator.export_path(self.project.project_id,self.run.run_id,'../outside')
        authority=LocalSessionAuthority();credentials=authority.connect_local_browser()
        app=create_app(ControllerConfig(static),provider=ProviderStatus('Codex',ProviderState.READY,'ready'),authority=authority,edit_agents=coordinator)
        code,_,_=asyncio.run(request_app(app,'GET',result['download_url']));self.assertEqual(code,401)
        headers={'cookie':f'clipen_session={credentials.session_token}'}
        code,_,body=asyncio.run(request_app(app,'GET',result['download_url'],headers=headers))
        self.assertEqual(code,200);self.assertEqual(json.loads(body)['format'],'clipen.native.v1')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg needed')
    def test_fractional_source_ranges_keep_exact_frames_across_batches(self):
        root=Path(self.fixture.temporary.name);source=root/'fractional.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=30000/1001:duration=10',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=10','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)],check=True)
        projects=self.projects
        class Media:
            def __init__(self):self.projects=projects
            def open(self,*args):return MediaPreview(os.open(source,os.O_RDONLY),source.stat().st_size,'video/mp4',(1,),(0,))
        self.call('replace_sequence',clips=[{'candidate_id':'candidate-street','start_offset_seconds':n*.13,'duration_seconds':4.017} for n in range(9)],summary='29.97fps來源精剪到30fps時間軸，不能逐鏡累積四捨五入誤差')
        timeline=self.tools._timeline();output=root/'fractional-output.mp4'
        FfmpegPreviewRenderer(Media()).render(self.project.project_id,timeline,output,threading.Event(),lambda _:None)
        info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(output)]))
        video=next(s for s in info['streams'] if s['codec_type']=='video')
        self.assertEqual(video['avg_frame_rate'],'30/1',video)
        self.assertEqual(int(video['nb_frames']),timeline.duration_frames,video)
        from clipen.finishing.renderer import verify_output
        verify_output(output,timeline,threading.Event())

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg needed')
    def test_fractional_cuts_show_next_shot_on_its_first_frame(self):
        root=Path(self.fixture.temporary.name);source=root/'cut-colors.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=red:size=320x180:rate=30000/1001:duration=4',
            '-f','lavfi','-i','color=blue:size=320x180:rate=30000/1001:duration=4',
            '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]','-c:v','libx264','-preset','ultrafast',str(source)],check=True)
        projects=self.projects
        class Media:
            def __init__(self):self.projects=projects
            def open(self,*args):return MediaPreview(os.open(source,os.O_RDONLY),source.stat().st_size,'video/mp4',(),(0,))
        self.call('replace_sequence',clips=[{'candidate_id':'candidate-street','start_offset_seconds':.015+4*(n%2),'duration_seconds':2.97125} for n in range(12)],
            reprise_reason='交替色塊驗證分數影格剪點與分批邊界',summary='字幕切換時不應仍顯示上一鏡')
        timeline=self.tools._timeline();output=root/'cut-colors-output.mp4'
        FfmpegPreviewRenderer(Media()).render(self.project.project_id,timeline,output,threading.Event(),lambda _:None)
        selection='+'.join(f'eq(n\\,{i.start_frame})' for i in self.tools._video().items)
        frames=subprocess.check_output(['ffmpeg','-v','error','-i',str(output),'-vf','select='+selection,'-fps_mode','passthrough','-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
        size=timeline.width*timeline.height*3
        self.assertEqual(len(frames),12*size)
        for n in range(12):
            red,_,blue=frames[n*size:n*size+3]
            self.assertGreater(blue if n%2 else red,180,f'wrong source at cut {n}')
            self.assertLess(red if n%2 else blue,40,f'wrong source at cut {n}')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg needed')
    def test_real_tools_sample_extract_mix_mosaic_grade_render_inspect_revise_and_finish(self):
        root=Path(self.fixture.temporary.name)
        source=root/'source.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=30:duration=10',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=10','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac',str(source)],check=True)
        projects=self.projects
        class Media:
            def __init__(self):self.projects=projects
            def open(self,*args):return MediaPreview(os.open(source,os.O_RDONLY),source.stat().st_size,'video/mp4',(1,),(0,))
        self.tools.media=Media();self.tools.preview_renderer=FfmpegPreviewRenderer(self.tools.media)
        self.call('insert_clip',candidate_id='candidate-street',start_offset_seconds=4,duration_seconds=4,before_item_id=None,summary='檢查分批渲染的剪點')
        ninth=self.tools._video().items[-1].item_id
        self.call('set_transition',item_id=ninth,kind='dip_black',duration_frames=12,summary='跨分批邊界的轉場')
        sampled=self.call('sample_source',candidate_id='candidate-street',offsets_seconds=[.5,2,5])
        self.assertEqual(len(sampled.images),3)
        with self.assertRaises(ProjectError):self.call('sample_source',candidate_id='candidate-street',offsets_seconds=[11])
        prepared=self.call('extract_audio',candidate_id='candidate-street',start_offset_seconds=1,duration_seconds=2)
        asset=prepared.payload['assets'][0]
        self.assertEqual(asset['provenance']['license'],'user_source_rights_unknown')
        inspected=self.call('inspect_audio',asset_id=asset['asset_id'])
        self.assertFalse(inspected.payload['heard_by_model'])
        self.assertNotIn(str(root),json.dumps(inspected.payload))
        self.call('set_mosaic',item_id='agent-video-0001',regions=[{'start_frame':0,'duration_frames':120,'x':.1,'y':.1,'width':.5,'height':.6,'block_size':24}],summary='對測試圖案加上馬賽克')
        self.call('set_color_grade',item_id='agent-video-0001',contrast=1.1,saturation=.8,brightness=.01,summary='測試顏色')
        self.call('set_source_audio',item_id='agent-video-0001',mode='mute',gain_db=0,summary='測試靜音')
        self.call('set_transition',item_id='agent-video-0002',kind='dip_white',duration_frames=12,summary='測試轉場')
        self.call('place_audio',item_id='agent-video-0001',layer_id='ambient-one',asset_id=asset['asset_id'],start_frame=0,duration_frames=60,source_offset_seconds=0,gain_db=-6,duck=True,fade_seconds=.1,summary='局部混入環境聲')
        self.assertEqual(len(self.call('list_audio_assets').payload['assets']),1)
        reviewed_id=self.tools.repository.current_revision(self.project.project_id,self.run.run_id).revision_id
        started=self.call('render_preview').payload
        self.assertEqual(self.call('render_preview').payload['render']['render_id'],started['render']['render_id'])
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            result=self.call('inspect_preview',render_id=started['render']['render_id'],offsets_seconds=[1,4.1,31.95,32.05],wait_seconds=5)
            if result.payload['render']['state'] not in {'running','queued'}:break
        self.assertEqual(result.payload['render']['state'],'succeeded',result.payload)
        self.assertEqual(len(result.images),4);self.assertFalse(result.payload['stale'])
        exported=self.call('export_edit').payload
        self.assertEqual(len(exported['credits']),1)
        # The masked area must contain blockwise-constant pixels, not just a black rectangle.
        from PIL import Image,ImageStat
        image=Image.open(result.images[0]).convert('RGB')
        for frame in result.images[-2:]:
            self.assertLess(sum(ImageStat.Stat(Image.open(frame).convert('RGB')).mean)/3,60)
        region=image.crop((80,50,250,150))
        self.assertGreater(sum(ImageStat.Stat(region).var),100)
        self.call('remove_audio',layer_id='ambient-one',summary='移除聲音作為第二版')
        stale=self.call('inspect_preview',render_id=started['render']['render_id'],offsets_seconds=[]).payload
        self.assertTrue(stale['stale'])
        with self.assertRaises(ProjectError) as error:self.call('finish_edit',summary='舊預覽不算目前版本')
        self.assertEqual(error.exception.code,'agent_preview_review_required')
        self.call('restore_revision',revision_id=reviewed_id,summary='選回已檢視版本')
        # Sparse output samples cannot certify an entire cut. Cover every shot,
        # then record concrete revision-bound findings before finishing.
        with self.assertRaises(ProjectError):self.call('finish_edit',summary='只看幾張圖還不能完成')
        ids=[i.item_id for i in self.tools._video().items]
        for n in range(0,len(ids),3):
            self.call('inspect_cut',item_ids=ids[n:n+3],render_id=started['render']['render_id'])
        shots=[{'item_id':i,'observation':'測試圖案在三個時間點皆可見，轉場前後位置正確','decision':'keep'} for i in ids]
        self.call('review_cut',shots=shots,text_checks=[],story_assessment='技術測試素材連續排列，沒有敘事品質判定',audio_limitations='沒有聽音訊，只驗證本機混音')
        finished=self.call('finish_edit',summary='已檢視的版本')
        self.assertEqual(finished.payload['state'],'succeeded')
        self.assertEqual(finished.payload['budget']['preview_renders'],1)
        self.assertEqual(finished.payload['export']['revision_id'],finished.payload['final_revision_id'])
        exported=self.projects.root/self.project.project_id/'agent-production'/self.run.run_id/'exports'/(finished.payload['final_revision_id']+'.json')
        self.assertEqual(json.loads(exported.read_text())['timeline'],self.tools._timeline().to_payload())
        # Dense subtitle tracks use one event stream rather than 33 live image inputs.
        from clipen.finishing.renderer import verify_output
        current=self.tools._timeline();clip=self.tools._video().items[0]
        dense=replace(current,duration_frames=120,tracks=(Track('video',TrackKind.VIDEO,(clip,)),
                    Track('captions',TrackKind.TITLE,tuple(TextClip('cap-'+str(n),n*3,2,'字'+str(n),'title.caption') for n in range(33)))))
        dense_output=root/'dense.mp4'
        self.tools.preview_renderer.render(self.project.project_id,dense,dense_output,threading.Event(),lambda _:None)
        verify_output(dense_output,dense,threading.Event())


class ProductionTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.fixture=evidence_fixtures.FinishingEvidenceMappingTests();self.fixture.setUp()
    def tearDown(self):self.fixture.tearDown()
    def test_remap_review_and_partial_speech_are_respected(self):
        f=self.fixture
        cues,limits=transcript_cues(f.projects,f.timeline.project_id,f.timeline)
        self.assertEqual(len(cues),8);self.assertEqual(cues[0]['start_frame'],15)
        self.assertEqual(cues[0]['text'],'我們抵達臺北了')
        f.db.execute("UPDATE transcript_segments SET end_pts='400000'")
        cues,limits=transcript_cues(f.projects,f.timeline.project_id,f.timeline)
        self.assertFalse(cues);self.assertEqual(limits[0]['reason'],'partial_utterance')
        self.assertEqual(limits[0]['text'],'我們抵達臺北了')
        self.assertIn('complete_source_span',limits[0])
        # A fast-cut montage hands the slice to music: muted camera sound needs no caption.
        muted=replace(f.timeline,tracks=tuple(replace(t,items=tuple(replace(c,effects=c.effects+(EffectRequest('audio.source',{'mode':'mute','gain_db':0}),))
                                                                    for c in t.items)) if t.kind is TrackKind.VIDEO else t for t in f.timeline.tracks))
        self.assertEqual(transcript_cues(f.projects,f.timeline.project_id,muted),([],[]))
        f.db.execute('UPDATE preparation_items SET review_required=1');f.db.execute('UPDATE transcript_annotations SET confirmed=0')
        cues,limits=transcript_cues(f.projects,f.timeline.project_id,f.timeline)
        self.assertFalse(cues);self.assertEqual(limits[0]['reason'],'transcript_needs_review')
        # Real low-confidence revisions have status=needs_review, not merely a
        # preparation flag. They must remain visible as limitations, not silence.
        f.db.execute("UPDATE transcription_revisions SET status='needs_review'")
        cues,limits=transcript_cues(f.projects,f.timeline.project_id,f.timeline)
        self.assertFalse(cues);self.assertEqual(limits[0]['reason'],'transcript_needs_review')
        self.assertEqual(limits[0]['provisional_text'],'我們抵達臺北了')

if __name__=='__main__':unittest.main()
