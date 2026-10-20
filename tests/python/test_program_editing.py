"""Programme direction, grounded narrative and real animated caption regressions."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch

from clipen.domain import TextClip,Track,TrackKind
from clipen.editing import EditAgentTools
from clipen.editing.program_direction import PROGRAM_DIRECTION
from clipen.editing.program_story import read_episode
from clipen.editing.production_media import load_text_styles,transcript_cues
from clipen.editing.program_subtitles import subtitle_status
from clipen.media import MediaPreview
from clipen.projects import ProjectError
from clipen.rendering import FfmpegPreviewRenderer
from clipen.rendering.titles import TitleCardRenderer
import test_edit_agent as fixtures
import test_finishing as evidence_fixtures


class ProgramEditingTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.EditAgentTests();self.f.setUp()
        self.run,_=self.f.repository.create_run(self.f.project.project_id,'programme-direction',max_tool_calls=120,creative_direction=PROGRAM_DIRECTION)
        self.tools=EditAgentTools(self.f.repository,self.f.project.project_id,self.run.run_id,
            planning=fixtures.StaticPlanningInput(self.f.plan,self.f.context),artifacts=fixtures.EmptyArtifacts())
        self.tools.activate()
        self.call('replace_sequence',clips=[{'candidate_id':'candidate-street','start_offset_seconds':o,'duration_seconds':4} for o in (0,.5,1,1.5,2,2.5,3,4)],summary='來源對應的節目粗剪')
    def tearDown(self):self.tools.close_production();self.f.tearDown()
    def call(self,name,**args):return self.tools.call(name,args)
    def episode(self):
        ids=[i.item_id for i in self.tools._video().items]
        return {'premise':'一段真實體驗','audience_question':'實際體驗後得到什麼？','payoff':'由原片的結果作回應','beats':[
            {'beat_id':'setup','role':'setup','item_ids':ids[:1],'dialogue_ids':[],'source_evidence':'原片交代當下情境','audience_takeaway':'知道期待'},
            {'beat_id':'try','role':'experience','item_ids':ids[1:-1],'dialogue_ids':[],'source_evidence':'原片具體動作','audience_takeaway':'跟上體驗'},
            {'beat_id':'answer','role':'payoff','item_ids':ids[-1:],'dialogue_ids':[],'source_evidence':'原片最後的回應','audience_takeaway':'看到結果'}]}
    def test_user_direction_is_immutable_idempotent_and_forbids_cards(self):
        self.assertEqual(self.call('get_edit_status').payload['creative_direction'],PROGRAM_DIRECTION)
        with self.assertRaises(ProjectError) as caught:self.call('add_title',start_frame=0,duration_frames=30,text='任意行程字卡',style_id='title.chapter',summary='不應加入')
        self.assertEqual(caught.exception.code,'title_cards_disabled')
        with self.assertRaises(ProjectError):self.f.repository.create_run(self.f.project.project_id,'programme-direction',max_tool_calls=120,creative_direction={**PROGRAM_DIRECTION,'direction':'同一請求偷偷改方向'})
        issues=self.call('validate_sequence').payload['issues']
        self.assertIn('episode_treatment_required',{i['code'] for i in issues})
        # The source evidence links remain model-authored, not user confirmation.
        self.assertNotEqual(self.tools._production_record_story_plan({'intent':'自訂','sections':[{'purpose':'體驗','candidate_ids':['candidate-street'],'treatment':'觀察來源'}],'uncertainties':[]}).payload['story_plan']['authority'],'user_confirmed')
    def test_episode_rejects_foreign_dialogue_and_stales_on_recut_or_muting(self):
        episode=self.episode();bad=json.loads(json.dumps(episode));bad['beats'][1]['dialogue_ids']=['invented-cue']
        with self.assertRaises(ProjectError):self.call('plan_episode',**bad)
        self.call('plan_episode',**episode)
        def current():return read_episode(self.f.projects,self.f.project.project_id,self.run.run_id,self.tools._timeline())
        self.assertIsNotNone(current())
        self.call('set_color_grade',item_id='agent-video-0001',contrast=1.1,saturation=1,brightness=0,summary='不改故事的顏色調整')
        self.assertIsNotNone(current())
        self.call('set_source_audio',item_id='agent-video-0001',mode='mute',gain_db=0,summary='拿掉支撐故事的口語')
        self.assertIsNone(current())
        self.call('plan_episode',**episode)
        self.call('trim_clip',item_id='agent-video-0001',start_offset_frames=0,duration_frames=90,summary='改變敘事剪點')
        self.assertIsNone(current())
    def test_story_beats_can_share_a_continuous_shot_without_artificial_cuts(self):
        items=list(self.tools._video().items);episode=self.episode()
        episode['beats']=[{'beat_id':str(n),'role':role,'start_frame':start,'duration_frames':duration,
            'item_ids':ids,'dialogue_ids':[],'source_evidence':'同一事件中依時間發生的不同敘事用途','audience_takeaway':role}
            for n,(role,start,duration,ids) in enumerate([
                ('setup',0,60,[items[0].item_id]),('experience',60,60,[items[0].item_id]),
                ('payoff',120,self.tools._timeline().duration_frames-120,[i.item_id for i in items[1:]])])]
        self.call('plan_episode',**episode)
        self.assertEqual(len(self.tools._video().items),8)
        episode['beats'][1]['start_frame']=59
        with self.assertRaises(ProjectError):self.call('plan_episode',**episode)
    def test_draft_subtitles_require_explicit_opt_in_and_keep_source_provenance(self):
        cue={'cue_id':'source-cue','item_id':'agent-video-0001','start_frame':5,'duration_frames':90,'text':'今天最期待的是吹冷氣','verification':'asr_draft','source_revision_id':'transcript-source','segment_id':'segment-source','source':{'version_id':'source'}}
        def source(*args,include_drafts=False):return ([cue] if include_drafts else [],[{'reason':'transcript_needs_review'}])
        with patch('clipen.editing.production_media.transcript_cues',side_effect=source):
            with self.assertRaises(ProjectError):self.call('replace_subtitles',captions=[{'cue_id':'source-cue','text':cue['text']}],summary='未同意草稿不可使用')
            with self.assertRaises(ProjectError) as mismatch:self.call('replace_subtitles',captions=[{'cue_id':'source-cue','text':'這餐三千元，給它十分'}],allow_asr_draft=True,summary='來源有ID也不能亂編台詞')
            self.assertEqual(mismatch.exception.code,'subtitle_source_mismatch')
            self.call('replace_subtitles',captions=[{'cue_id':'source-cue','text':cue['text'],'emphasis':['吹冷氣']}],allow_asr_draft=True,summary='只建立有來源的字幕草稿')
        profile=next(iter(load_text_styles(self.f.projects,self.f.project.project_id).values()))
        self.assertEqual(profile['verification'],'asr_draft');self.assertEqual(profile['segment_id'],'segment-source')
        self.assertEqual(profile['kind'],'program_subtitle');self.assertEqual(profile['emphasis'],['吹冷氣'])
        self.assertEqual(subtitle_status(self.f.projects,self.f.project.project_id,self.tools._timeline())['draft_count'],1)
        exported=self.call('export_edit').payload
        path=self.f.projects.root/self.f.project.project_id/'agent-production'/self.run.run_id/'exports'/(exported['export_id']+'.json')
        self.assertEqual(json.loads(path.read_text())['subtitle_review']['draft_count'],1)
    def test_programme_cold_open_reprise_is_intentional_and_documented(self):
        clips=[{'candidate_id':'candidate-street','start_offset_seconds':0,'duration_seconds':8}]*4
        with self.assertRaises(ProjectError):self.call('replace_sequence',clips=clips,summary='未說明的重複')
        self.call('replace_sequence',clips=clips,summary='明確的回扣測試',reprise_reason='節目開場預告與結尾回扣；此測試只檢查顯式授權，不代表四次重複有剪輯品質')
    def test_compound_speech_pages_preserve_words_and_cover_original_segment(self):
        from clipen.editing.program_subtitles import make_subtitle_items
        text='還有二十五週年的飲料，朋友最喜歡的行程，耶，好開心哦！吹冷氣。'
        cue={'cue_id':'compound','start_frame':17,'duration_frames':306,'text':text,'verification':'asr_draft'}
        pages=make_subtitle_items(self.tools,cue,{'text':text,'delivery':'quiet','emphasis':['吹冷氣']})
        self.assertEqual(''.join(i.text for i in pages),text)
        self.assertTrue(all(2<len(i.text)<=18 for i in pages))
        self.assertEqual(pages[0].start_frame,17)
        self.assertEqual(pages[-1].start_frame+pages[-1].duration_frames,323)
        self.assertTrue(all(a.start_frame+a.duration_frames==b.start_frame for a,b in zip(pages,pages[1:])))
        styles=load_text_styles(self.f.projects,self.f.project.project_id)
        self.assertTrue(all(styles[i.style_id]['motion']=='none' for i in pages))
    def test_uncertain_draft_can_be_excerpted_without_printing_repetition_or_rewriting(self):
        from clipen.editing.program_subtitles import make_subtitle_items
        text='太近。 poppy poppy poppy poppy。'
        cue={'cue_id':'noisy','start_frame':10,'duration_frames':300,'text':text,'verification':'asr_draft'}
        with self.assertRaises(ProjectError) as noisy:make_subtitle_items(self.tools,cue,{'text':text})
        self.assertEqual(noisy.exception.code,'subtitle_draft_unreliable')
        pages=make_subtitle_items(self.tools,cue,{'text':'太近。','source_excerpt':{'start':0,'end':3}})
        self.assertEqual(''.join(i.text for i in pages),'太近。');self.assertEqual(pages[0].start_frame,10)
        self.assertLess(sum(i.duration_frames for i in pages),300)
        profile=load_text_styles(self.f.projects,self.f.project.project_id)[pages[0].style_id]
        self.assertEqual(profile['source_text'],text);self.assertEqual(profile['source_excerpt'],{'start':0,'end':3})
        self.assertEqual(profile['timing_basis'],'character_proportional_within_asr_segment')
        with self.assertRaises(ProjectError):make_subtitle_items(self.tools,cue,{'text':'好漂亮。','source_excerpt':{'start':0,'end':3}})
        with self.assertRaises(ProjectError):make_subtitle_items(self.tools,cue,{'text':'太近。','source_excerpt':{'start':0,'end':300}})
        # Real, human-verified repetition must remain expressible.
        self.assertTrue(make_subtitle_items(self.tools,{**cue,'verification':'reviewed_source'},{'text':text}))
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg needed')
    def test_real_outlined_emphasis_and_animation_render_without_libass(self):
        from PIL import Image,ImageStat
        root=Path(self.f.temporary.name);source=root/'camera.mp4'
        subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-f','lavfi','-i','color=c=0x274858:size=640x360:rate=30:duration=3','-f','lavfi','-i','sine=frequency=220:duration=3','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)],check=True)
        cue={'cue_id':'real','item_id':'agent-video-0001','start_frame':3,'duration_frames':60,'text':'今天最期待的是吹冷氣','verification':'asr_draft','segment_id':'seg-real','source_revision_id':'tr-real'}
        with patch('clipen.editing.production_media.transcript_cues',return_value=([cue],[])):
            self.call('replace_subtitles',captions=[{'cue_id':'real','text':cue['text'],'emphasis':['吹冷氣']}],allow_asr_draft=True,summary='用實際影片驗證字幕元件')
        caption=next(t for t in self.tools._timeline().tracks if t.track_id=='agent-subtitles').items[0]
        timeline=replace(self.tools._timeline(),width=640,height=360,duration_frames=90,tracks=(Track('video',TrackKind.VIDEO,(replace(self.tools._video().items[0],duration_frames=90,source=replace(self.tools._video().items[0].source,span=replace(self.tools._video().items[0].source.span,end_pts='270000'))),)),Track('agent-subtitles',TrackKind.TITLE,(caption,))))
        styles=load_text_styles(self.f.projects,self.f.project.project_id);renderer=TitleCardRenderer(styles)
        images=[]
        for relative in (0,8):
            im=Image.new('RGBA',(640,360));from PIL import ImageDraw,ImageFont
            renderer._draw_card(ImageDraw.Draw(im),ImageFont,renderer._font_path(),timeline,caption,relative);images.append(im)
        self.assertNotEqual(images[0].getbbox(),images[1].getbbox())
        colors=list(images[1].get_flattened_data());self.assertGreater(sum(r>180 and g>140 and b<100 and a>200 for r,g,b,a in colors),20)
        self.assertEqual(images[1].getpixel((0,0))[3],0)
        projects=self.f.projects
        class Media:
            def __init__(self):self.projects=projects
            def open(self,*args):return MediaPreview(os.open(source,os.O_RDONLY),source.stat().st_size,'video/mp4',(1,),(0,))
        output=root/'programme.mp4';FfmpegPreviewRenderer(Media()).render(self.f.project.project_id,timeline,output,threading.Event(),lambda _:None)
        from clipen.finishing.renderer import verify_output
        verify_output(output,timeline,threading.Event())
        for time in (.1,.4,2.9):
            frame=root/f'output-{time}.png';subprocess.run(['ffmpeg','-v','error','-ss',str(time),'-i',str(output),'-frames:v','1','-y',str(frame)],check=True)
        early=Image.open(root/'output-0.1.png');settled=Image.open(root/'output-0.4.png')
        self.assertNotEqual(early.tobytes(),settled.tobytes())
        end=Image.open(root/'output-2.9.png');self.assertLess(max(ImageStat.Stat(end).stddev),2)


class ProgrammeTranscriptTests(unittest.TestCase):
    def test_provisional_cues_do_not_confirm_human_review_or_ignore_partial_speech(self):
        f=evidence_fixtures.FinishingEvidenceMappingTests();f.setUp()
        try:
            f.db.execute("UPDATE transcription_revisions SET status='needs_review'");f.db.execute('UPDATE preparation_items SET review_required=1');f.db.execute('UPDATE transcript_annotations SET confirmed=0')
            self.assertFalse(transcript_cues(f.projects,f.timeline.project_id,f.timeline)[0])
            cues,_=transcript_cues(f.projects,f.timeline.project_id,f.timeline,include_drafts=True)
            self.assertEqual(len(cues),8);self.assertEqual(cues[0]['verification'],'asr_draft')
            self.assertEqual(f.db.execute('SELECT confirmed FROM transcript_annotations').fetchone()[0],0)
            f.db.execute("UPDATE transcript_segments SET end_pts='900000'")
            self.assertFalse(transcript_cues(f.projects,f.timeline.project_id,f.timeline,include_drafts=True)[0])
        finally:f.tearDown()

class AutonomousProgrammeTests(unittest.TestCase):
    def test_full_production_cannot_finish_as_captions_only_or_ending_fade(self):
        from clipen.editing.program_direction import FULL_PRODUCTION_REQUIREMENTS,validate_direction
        from clipen.editing.production_requirements import requirement_issues,production_contents
        f=ProgramEditingTests();f.setUp()
        try:
            pid=f.f.project.project_id;repo=f.f.repository
            repo.cancel(pid,f.run.run_id)
            direction={**PROGRAM_DIRECTION,'production_requirements':FULL_PRODUCTION_REQUIREMENTS}
            with self.assertRaises(ProjectError):validate_direction({**direction,'production_requirements':None})
            run,_=repo.create_run(pid,'full-production',creative_direction=direction)
            f.tools=EditAgentTools(repo,pid,run.run_id,planning=fixtures.StaticPlanningInput(f.f.plan,f.f.context),artifacts=fixtures.EmptyArtifacts());f.tools.activate()
            codes=lambda:{i['code'] for i in requirement_issues(f.tools)}
            self.assertEqual(codes(),{'production_music_required','production_transition_required','production_editing_required'})
            f.call('replace_sequence',clips=[{'candidate_id':'candidate-street','start_offset_seconds':o,'duration_seconds':4} for o in (0,.5,1,1.5,2,2.5,3,4)],summary='實際精剪')
            f.call('set_fade',item_id='agent-video-0008',fade_in_frames=0,fade_out_frames=15,summary='只有片尾淡出仍未滿足轉場')
            self.assertIn('production_transition_required',codes())
            f.call('set_transition',item_id='agent-video-0002',kind='dip_black',duration_frames=12,summary='在時間跳接處安排短轉場')
            self.assertEqual(codes(),{'production_music_required'})
            f.tools._asset_store()
            layer={'layer_id':'music','asset_id':'public-song','start_frame':0,'duration_frames':300,'source_offset_seconds':0,'gain_db':-24,'duck':True,'fade_seconds':.3}
            f.tools._effect('agent-video-0002','audio.layers',{'layers':[layer]},'加入來源配樂')
            asset={'asset_id':'public-song','role':'music','name':'Test song','provenance':{'title':'Test song','creator':'Fixture','source_url':'https://example.org/song','license':'cc0','license_url':'https://creativecommons.org/publicdomain/zero/1.0/'}}
            with patch('clipen.editing.production_requirements.AgentAudioStore.public_assets',return_value=[asset]):
                self.assertFalse(codes())
                contents=production_contents(f.f.projects,pid,f.tools._timeline())
                self.assertEqual(contents['music'][0]['start_seconds'],4)
                self.assertEqual(contents['music'][0]['duration_seconds'],10)
                self.assertEqual(len(contents['transitions']),1)
                self.assertEqual(contents['cut_count'],7)
                asset['role']='sfx'
                self.assertIn('production_music_required',codes())
                asset['role']='music'
                asset['provenance']={}
                self.assertIn('production_music_required',codes())
        finally:f.tearDown()

    def test_reference_command_requires_browser_consent_and_exact_finished_revision(self):
        import asyncio
        from clipen.controller import ControllerConfig,LocalSessionAuthority,ProviderState,ProviderStatus,create_app
        from clipen.editing import EditAgentCoordinator
        from test_preview_rendering import request_app
        f=ProgramEditingTests();f.setUp()
        try:
            pid=f.f.project.project_id;repo=f.f.repository
            f.call('plan_episode',**f.episode());repo.finish(pid,f.run.run_id,'已完成測試版本')
            revision=repo.get(pid,f.run.run_id).final_revision_id
            static=Path(f.f.temporary.name)/'dist';static.mkdir();(static/'index.html').write_text('test')
            coordinator=EditAgentCoordinator(repo,Path(f.f.temporary.name))
            authority=LocalSessionAuthority();credentials=authority.connect_local_browser()
            app=create_app(ControllerConfig(static),provider=ProviderStatus('Codex',ProviderState.READY,'ready'),authority=authority,edit_agents=coordinator)
            url=f'/api/projects/{pid}/edit-agent/commands/accept-reference'
            def request(payload,headers=None):return asyncio.run(request_app(app,'POST',url,headers=headers,body=json.dumps(payload).encode()))
            payload={'run_id':f.run.run_id,'revision_id':revision}
            self.assertEqual(request(payload)[0],401)
            headers={'cookie':f'clipen_session={credentials.session_token}'}
            self.assertEqual(request(payload,headers)[0],403)
            headers.update({'origin':'http://localhost:4200','sec-fetch-site':'same-origin','x-clipen-csrf':credentials.csrf_token,'content-type':'application/json'})
            self.assertEqual(request({**payload,'revision_id':'editrev_'+'a'*32},headers)[0],409)
            self.assertEqual(request(payload,headers)[0],200)
            self.assertEqual(request(payload,headers)[0],200)
            with f.f.projects.connect(pid) as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM edit_agent_events WHERE event_type='user_program_reference'").fetchone()[0],1)
            coordinator.close()
        finally:f.tearDown()
    def test_reference_is_user_only_and_frozen_per_new_run(self):
        from clipen.editing.program_reference import accept_reference,reference_for
        f=ProgramEditingTests();f.setUp()
        try:
            pid=f.f.project.project_id;repo=f.f.repository
            self.assertIsNone(f.call('get_program_reference').payload['reference'])
            with self.assertRaises(ProjectError):accept_reference(repo,pid,f.run.run_id,f.tools._timeline().timeline_id)
            f.call('plan_episode',**f.episode())
            repo.finish(pid,f.run.run_id,'測試已完成節目版本')
            final=repo.get(pid,f.run.run_id).final_revision_id
            accepted=accept_reference(repo,pid,f.run.run_id,final)
            self.assertEqual(accepted['authority'],'user_selected_style_reference')
            self.assertIsNone(reference_for(repo,pid,f.run.run_id))
            next_run,_=repo.create_run(pid,'autonomous-followup',creative_direction=PROGRAM_DIRECTION)
            self.assertEqual(reference_for(repo,pid,next_run.run_id),accepted)
            # A later human choice cannot silently change the running brief.
            with f.f.projects.connect(pid) as db:db.execute("DELETE FROM edit_agent_events WHERE event_type='user_program_reference'")
            self.assertEqual(reference_for(repo,pid,next_run.run_id),accepted)
            self.assertNotIn('accept_program_reference',__import__('clipen.providers.codex_editing',fromlist=['_MCP_TOOLS'])._MCP_TOOLS)
        finally:f.tearDown()

    def test_dialogue_discovery_uses_live_sources_corrections_and_explicit_drafts(self):
        from types import SimpleNamespace
        from clipen.editing.source_dialogue import search_dialogue
        f=evidence_fixtures.FinishingEvidenceMappingTests();f.setUp()
        source={'version_id':'av_'+'b'*32,'display_name':'source.mp4','stream_index':0,'kind':'video','start_pts':'90000','end_pts':'630000','time_base':{'num':1,'den':90000}}
        tools=SimpleNamespace(repository=SimpleNamespace(projects=f.projects),project_id=f.timeline.project_id,run_id='test-run')
        try:
            with patch('clipen.editing.source_dialogue.source_rows',return_value=[source]),patch('clipen.editing.source_dialogue.reference_for',return_value=None):
                result=search_dialogue(tools,{'query':'臺北'}).payload
                self.assertEqual(result['total'],1);self.assertEqual(result['utterances'][0]['text'],'我們抵達臺北了')
                self.assertEqual(result['utterances'][0]['start_offset_seconds'],.5)
                self.assertEqual(result['utterances'][0]['source']['start_pts'],'135000')
                f.db.execute("UPDATE transcription_revisions SET status='needs_review'");f.db.execute('UPDATE transcript_annotations SET confirmed=0')
                self.assertEqual(search_dialogue(tools,{}).payload['unreviewed_omitted'],1)
                draft=search_dialogue(tools,{'include_drafts':True}).payload['utterances'][0]
                self.assertEqual(draft['verification'],'asr_draft');self.assertEqual(draft['context'][0]['text'],'我們抵達臺北了')
                self.assertEqual(search_dialogue(tools,{'include_drafts':True,'offset':1}).payload['utterances'],[])
                f.db.execute("UPDATE transcript_segments SET end_pts='900000'")
                self.assertEqual(search_dialogue(tools,{'include_drafts':True}).payload['incomplete_omitted'],1)
            with patch('clipen.editing.source_dialogue.source_rows',return_value=[source]),patch('clipen.editing.source_dialogue.reference_for',return_value={'source_version_ids':[source['version_id']]}):
                self.assertEqual(search_dialogue(tools,{'include_drafts':True}).payload['total'],0)
        finally:f.tearDown()


if __name__=='__main__':unittest.main()
