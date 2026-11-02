"""The DaVinci Resolve package rebuilds the film from original footage, with honest notes for what it cannot carry."""
from dataclasses import replace
from fractions import Fraction
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
import test_skill_production as fixtures
from clipen.domain import EffectRequest, TextClip, Track, TrackKind
from clipen.editing.resolve_export import export_resolve, timecode_seconds
from clipen.projects import ProjectError


class ResolveExportTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SkillProductionTests();self.f.setUp();self.f.cut()
        self.repository=self.f.repository;self.project=self.f.project.project_id;self.run=self.f.run.run_id
        self.out=tempfile.TemporaryDirectory();self.destination=Path(self.out.name)
        self.source=self.f.real_media()
    def tearDown(self):self.out.cleanup();self.f.tearDown()

    def film(self):
        """Two shots: a punched-in first shot and a mosaic second shot, a component, a caption and music."""
        from clipen.editing.workbench import import_music
        from clipen.editing.production_media import AgentAudioStore
        self.f.component(element_id='dish-label',x=.5,y=.12)
        directory=AgentAudioStore(self.f.projects,self.project).directory/'upload';directory.mkdir()
        music=directory/'bed.wav'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=9','-y',str(music)],check=True,capture_output=True)
        asset=import_music(self.repository,self.project,music,'背景音樂.wav')
        self.f.call('place_audio',item_id='agent-video-0001',layer_id='bed',asset_id=asset['asset_id'],start_frame=0,duration_frames=240,
                    source_offset_seconds=1,gain_db=-6,duck=True,fade_seconds=.5,summary='配樂')
        self.f.call('set_color_grade',item_id='agent-video-0001',contrast=1.1,saturation=.9,brightness=0,summary='調色')
        self.f.call('set_source_audio',item_id='agent-video-0001',mode='keep',gain_db=-6,summary='原音')
        self.f.call('set_mosaic',item_id='agent-video-0002',regions=[{'x':.1,'y':.1,'width':.2,'height':.2,'block_size':12,'start_frame':0,'duration_frames':30}],summary='遮臉')
        self.repository.cancel(self.project,self.run)
        revision=self.repository.current_revision(self.project,self.run);timeline=revision.timeline
        tracks=[]
        for track in timeline.tracks:
            items=tuple(replace(i,effects=i.effects+(EffectRequest('video.punch_in',{'scale':1.2,'center_x':.25,'center_y':.5}),)) if i.item_id=='agent-video-0001' else i for i in track.items)
            tracks.append(replace(track,items=items))
        tracks.append(Track('agent-subtitles',TrackKind.TITLE,(TextClip('caption-1',30,45,'第一句字幕\n第二行','title.caption'),)))
        return replace(revision,timeline=replace(timeline,tracks=tuple(tracks)))

    def export(self,revision):
        streams={0:{'kind':'video','start_pts':'90000','duration_ts':'1080000','time_base':'1/90000','r_frame_rate':'30/1','width':640,'height':360},
                 1:{'kind':'audio','channels':2,'sample_rate':48000}}
        rendered=[]
        def render(renderer,project_id,timeline,output,cancel,progress,preserve_audio_gain=False):
            rendered.append((renderer.master,timeline))
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=gray:size=640x360:rate=30:duration=4',
                            '-f','lavfi','-i','sine=duration=4','-c:v','prores_ks','-c:a','pcm_s16le','-shortest',str(output)],check=True)
        with patch.object(self.repository,'current_revision',return_value=revision),\
             patch('clipen.media.preview.MediaPreviewRepository.locate',return_value=(self.source,streams)),\
             patch('clipen.rendering.renderer.FfmpegPreviewRenderer._render_media',autospec=True,side_effect=render):
            return export_resolve(self.repository,self.project,self.run,self.destination),rendered

    def test_package_rebuilds_the_film_for_resolve(self):
        result,rendered=self.export(self.film())
        package=Path(result['folder'])
        self.assertEqual(package.parent,self.destination)
        root=ET.parse(result['fcpxml']).getroot()
        self.assertEqual(root.get('version'),'1.10')
        spine=root.find('.//spine');shots=spine.findall('asset-clip')
        self.assertEqual(len(shots),2)
        # The first shot points at the original file from its first frame, framed like Clipen.
        assets={a.get('id'):a for a in root.iter('asset')}
        first=shots[0];self.assertEqual(assets[first.get('ref')].find('media-rep').get('src'),self.source.resolve().as_uri())
        self.assertEqual((first.get('offset'),first.get('start'),first.get('duration')),('0s','0s','4s'))
        transform=first.find('adjust-transform')
        self.assertEqual(transform.get('scale'),'1.2000 1.2000')
        self.assertEqual(transform.get('position'),f'{.2*640*.25/360*100:.4f} {0:.4f}')
        self.assertEqual(first.find('adjust-volume').get('amount'),'-6dB')
        self.assertIn('Clipen 調色',[m.get('value') for m in first.iter('marker')])
        # The mosaic shot is rendered first so the original never reaches the timeline.
        self.assertEqual(len(rendered),1);master,single=rendered[0]
        self.assertTrue(master)
        clip=single.tracks[0].items[0]
        self.assertEqual((clip.start_frame,single.duration_frames),(0,120))
        self.assertEqual({e.effect_id for e in clip.effects},{'video.mosaic'})
        second=shots[1];self.assertIn('已處理片段',assets[second.get('ref')].find('media-rep').get('src').replace('%E5%B7%B2%E8%99%95%E7%90%86%E7%89%87%E6%AE%B5','已處理片段'))
        self.assertIsNone(second.find('adjust-transform'))
        # The component is a transparent still above the picture; the caption is an editable SRT.
        stills=list(root.iter('video'));self.assertEqual(len(stills),1)
        self.assertEqual(stills[0].get('lane'),'1')
        self.assertEqual(len(list((package/'元件').glob('*.png'))),1)
        self.assertEqual((package/'字幕.srt').read_text(encoding='utf-8'),'1\n00:00:01,000 --> 00:00:02,500\n第一句字幕\n第二行\n')
        self.assertEqual(result['captions'],1);self.assertEqual(result['overlays'],1)
        # Music: Clipen's ducked mix plays; the original source waits, disabled, for a remix.
        lanes={c.get('lane'):c for c in root.iter('asset-clip') if c.get('lane')}
        self.assertEqual(lanes['-1'].get('duration'),'8s');self.assertIsNone(lanes['-1'].get('enabled'))
        self.assertEqual((lanes['-2'].get('enabled'),lanes['-2'].get('start')),('0','1s'))
        self.assertTrue((package/'音樂'/'配樂（Clipen 混音）.wav').is_file())
        guide=(package/'說明.txt').read_text(encoding='utf-8')
        self.assertIn('匯入 > 時間線',guide);self.assertIn('字幕.srt',guide);self.assertIn('馬賽克',guide)

    def test_more_components_than_one_render_pass_still_export(self):
        revision=self.film();timeline=revision.timeline
        extra=Track('agent-titles',TrackKind.TITLE,(TextClip('chapter',90,30,'第二章','title.default'),))
        with patch('clipen.rendering.titles.TitleCardRenderer._MAX_TITLES',1):
            result,_=self.export(replace(revision,timeline=replace(timeline,tracks=timeline.tracks+(extra,))))
        self.assertEqual(result['overlays'],2)
        self.assertEqual(len(list((Path(result['folder'])/'元件').glob('*.png'))),2)

    def test_a_second_export_never_overwrites_the_first(self):
        revision=self.film()
        first,_=self.export(revision);second,_=self.export(revision)
        self.assertNotEqual(first['folder'],second['folder'])
        self.assertTrue(Path(first['fcpxml']).is_file())

    def test_failed_export_leaves_no_partial_folder(self):
        revision=self.film()
        with patch('clipen.editing.resolve_export.probe',side_effect=ProjectError('resolve_source_unreadable','x',status_code=409)):
            with self.assertRaises(ProjectError):self.export(revision)
        self.assertEqual(list(self.destination.iterdir()),[])

    def test_api_exports_beside_the_footage_and_reveals_the_timeline(self):
        import asyncio, json
        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from clipen.editing import EditAgentCoordinator
        from test_project_api import request_app
        self.repository.cancel(self.project,self.run)
        static=self.f.root/'api-dist';static.mkdir();(static/'index.html').write_text('Clipen')
        coordinator=EditAgentCoordinator(self.repository,self.f.root)
        app=create_app(ControllerConfig(static),ProviderStatus('Codex',ProviderState.READY,'ready'),LocalSessionAuthority(),edit_agents=coordinator)
        def request(method,path,payload=None,headers=None):
            return asyncio.run(request_app(app,method,path,headers=headers,body=b'' if payload is None else json.dumps(payload).encode()))
        try:
            _,headers,_=request('POST','/api/session/connect',headers={'origin':'http://localhost:4200','sec-fetch-site':'same-origin'})
            cookie=headers['set-cookie'].split(';',1)[0];_,_,body=request('GET','/api/status',headers={'cookie':cookie});csrf=json.loads(body)['session']['csrf_token']
            mutation={'cookie':cookie,'origin':'http://localhost:4200','sec-fetch-site':'same-origin','content-type':'application/json','x-clipen-csrf':csrf}
            path=f'/api/projects/{self.project}/production/{self.run}/workbench/resolve'
            with patch('clipen.editing.resolve_export.export_resolve',return_value={'fcpxml':str(self.destination/'a.fcpxml')}) as export,\
                 patch('clipen.editing.resolve_export.reveal') as reveal:
                code,_,body=request('POST',path,{},mutation);self.assertEqual(code,200,body)
                self.assertEqual(export.call_args.args[1:],(self.project,self.run))
                reveal.assert_called_once_with(self.destination/'a.fcpxml')
        finally:coordinator.close()

    def test_packages_go_into_clipen_exports_beside_the_footage(self):
        from clipen.editing.resolve_export import export_folder
        footage=self.f.root/'source'
        self.assertEqual(export_folder(self.repository,self.project),footage/'Clipen 匯出')
        self.assertTrue((footage/'Clipen 匯出').is_dir())
        footage.joinpath('Clipen 匯出').rmdir();footage.rename(self.f.root/'unplugged')
        with self.assertRaises(ProjectError) as offline:export_folder(self.repository,self.project)
        self.assertEqual(offline.exception.code,'resolve_source_offline')

    def test_source_time_follows_the_file_timecode(self):
        self.assertEqual(timecode_seconds('00:00:01:00',Fraction(25)),1)
        self.assertEqual(timecode_seconds('01:00:00;00',Fraction(30000,1001)),Fraction(107892*1001,30000))
        self.assertEqual(timecode_seconds('nonsense',Fraction(25)),0)


if __name__=='__main__':unittest.main()
