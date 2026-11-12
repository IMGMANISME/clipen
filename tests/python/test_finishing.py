from array import array
from contextlib import contextmanager
from dataclasses import replace
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest

from clipen.domain import ClipSource, EditTimeline, EffectRequest, MediaClip, Rational, SourceSpan, TextClip, Track, TrackKind
from clipen.finishing.audio import detect_beats, run_process
from clipen.finishing.models import encoded
from clipen.projects import ProjectError


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


class FinishingAudioTests(unittest.TestCase):
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


class FinishingEvidenceMappingTests(unittest.TestCase):
    """A project database with one reviewed transcript, shared by the production transcript tests."""
    def setUp(self):
        self.timeline,_,_=fixture()
        video=self.timeline.tracks[0]
        self.timeline=replace(self.timeline,tracks=(replace(video,items=tuple(replace(c,source=ClipSource(SourceSpan(c.source.span.version_id,0,'90000','630000',Rational(1,90000)))) for c in video.items)),self.timeline.tracks[1]))
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript('''
            CREATE TABLE media_streams(version_id,stream_index,kind,details_json);
            CREATE TABLE transcription_revisions(revision_id,version_id,stream_index,status,source_span_json);
            CREATE TABLE preparation_items(transcript_revision_id,run_id,review_required);
            CREATE TABLE current_preparation(run_id);
            CREATE TABLE transcript_annotations(version_id,stream_index,source_revision_id,discarded,corrections_json,confirmed);
            CREATE TABLE transcript_segments(revision_id,segment_id,start_pts,end_pts,text,ordinal,confidence);
        ''')
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
        self.agents=SimpleNamespace(latest_successful=lambda _:SimpleNamespace(run_id='run'),final_revision=lambda *_:SimpleNamespace(revision_id='editrev-one',timeline=self.timeline))
        self.library={'music':{'directory':{},'assets':[{'role':'music','duration_seconds':64,'beats_seconds':[6.]}]}}

    def tearDown(self):self.db.close()


if __name__ == '__main__':
    unittest.main()
