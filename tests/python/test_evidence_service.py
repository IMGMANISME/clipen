import hashlib
import asyncio
import struct
import zlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

import test_analysis_storage as preparation_fixtures
from clipen.analysis.evidence_service import EvidenceService
from clipen.media import bounded_file_fingerprint
from clipen.projects import ProjectError
from clipen.providers.recovery import ProviderUsage

def png_chunk(kind, data):
    return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)

PNG = b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', struct.pack('!2I5B', 32, 32, 8, 2, 0, 0, 0)) + png_chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\0\0' * 32) * 32)) + png_chunk(b'IEND', b'')


class FakeExtractor:
    def version(self):
        return 'ffmpeg test'

    def frame(self, source, offset, output, cancel):
        output.write_bytes(PNG)
        span = source.source_span
        return int(span.start_pts) + int(offset * span.time_base.denominator / span.time_base.numerator)

    def scenes(self, source, cancel):
        return {'candidate_pts': [], 'maximum_window_seconds': 120, 'candidate_limit_reached': False}

    def audio_features(self, source, output, cancel):
        return {'sample_rate': 16000, 'windows': [{'start_sample': 0, 'end_sample': 16000, 'rms_dbfs': -120, 'peak': 0, 'low_energy': True}]}


class FakeProvider:
    calls = []
    def __init__(self, root):
        self.root = root
    def probe(self):
        return SimpleNamespace(available=True)
    def execute(self, attempt, cancel):
        self.calls.append(attempt)
        return SimpleNamespace(usage=ProviderUsage(30, 10, 15), payload={
            'observations': [{'index': i, 'description': '測試畫面', 'uncertainty': '只有取樣'} for i in range(len(attempt.images))],
            'style_summary': '僅描述畫面', 'limitations': '未分析音訊或中間畫面'})


class EvidenceServiceTests(unittest.TestCase):
    def setUp(self):
        # Reuse the established inventory fixture, replacing its placeholder fingerprints.
        preparation_fixtures.PreparationStorageTests.setUp(self)
        with self.projects.connect(self.project.project_id) as db:
            rows = db.execute('SELECT a.relative_path,v.version_id FROM assets a JOIN asset_versions v ON a.asset_id=v.asset_id').fetchall()
            for row in rows:
                path = self.source / row['relative_path']
                fd = os.open(path, os.O_RDONLY)
                try:
                    fingerprint = bounded_file_fingerprint(fd, path.stat().st_size)
                finally:
                    os.close(fd)
                db.execute('UPDATE asset_versions SET fingerprint=? WHERE version_id=?', (fingerprint, row['version_id']))
        self.attempts = self.root / 'attempts'
        self.attempts.mkdir()
        FakeProvider.calls = []
        self.service = EvidenceService(self.projects, self.attempts, extractor_factory=FakeExtractor, adapter_factory=FakeProvider)
        self.pid = self.project.project_id

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def start(self):
        scope = self.service.scope(self.pid)
        return self.service.start(self.pid, 'evidence-request-1', scope['project_revision'], scope['scope_id'])

    def wait(self):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            status = self.service.status(self.pid)
            latest = status['latest']
            if latest and latest['state'] != 'running' and all(o['state'] != 'running' for o in latest['observations']):
                return status
            time.sleep(0.01)
        self.fail('evidence did not settle')

    def test_local_results_are_durable_source_bound_and_private(self):
        original = hashlib.sha256(self.clip.read_bytes()).hexdigest()
        self.start()
        status = self.wait()
        run = status['latest']
        self.assertEqual(run['state'], 'succeeded')
        self.assertEqual(len(run['results']), 3)
        video = next(r for r in run['results'] if r['kind'] == 'video')
        self.assertEqual(len(video['frames']), 8)
        self.assertEqual(video['keyframe_selection']['candidate_count'], 32)
        self.assertEqual(video['keyframe_selection']['method'], 'adaptive_relevance_coverage_v1')
        self.assertFalse(video['keyframe_selection']['semantic_relevance'])
        self.assertTrue(
            all(item['source'] == 'uniform' for item in video['keyframe_selection']['selected'])
        )
        self.assertFalse(video['coverage']['complete'])
        audio = next(r for r in run['results'] if r['kind'] == 'audio')
        self.assertEqual(audio['coverage']['inspected'], [{'start_pts': '48000', 'end_pts': '96000'}])
        self.assertNotIn(str(self.source), json.dumps(status))
        self.assertEqual(hashlib.sha256(self.clip.read_bytes()).hexdigest(), original)
        self.assertEqual(self.service.image(self.pid, run['run_id'], video['frames'][0]['artifact_id']).read_bytes(), PNG)
        self.assertEqual(FakeProvider.calls, [])

    def test_consent_idempotency_and_duplicate_observation(self):
        self.start()
        run = self.wait()['latest']
        key = next(r['key'] for r in run['results'] if r['kind'] == 'video')
        with self.assertRaises(ProjectError):
            self.service.observe(self.pid, run['run_id'], 'observe-request-1', key, False)
        self.service.observe(self.pid, run['run_id'], 'observe-request-1', key, True)
        final = self.wait()['latest']
        self.assertEqual(final['observations'][0]['state'], 'succeeded')
        self.assertEqual(final['observations'][0]['usage']['input_tokens'], 30)
        self.service.observe(self.pid, run['run_id'], 'observe-request-1', key, True)
        self.assertEqual(len(FakeProvider.calls), 1)
        with self.assertRaises(ProjectError):
            self.service.observe(self.pid, run['run_id'], 'observe-request-2', key, True)
        self.assertEqual(len(FakeProvider.calls), 1)

    def test_changed_source_is_never_sent_to_provider(self):
        self.start()
        run = self.wait()['latest']
        item = next(r for r in run['results'] if r['kind'] == 'video' and r['display_name'] == 'clip.mov')
        self.clip.write_bytes(b'changed source')
        self.service.observe(self.pid, run['run_id'], 'observe-request-1', item['key'], True)
        final = self.wait()['latest']['observations'][0]
        self.assertEqual(final['state'], 'failed')
        self.assertEqual(final['error_code'], 'source_changed')
        self.assertEqual(FakeProvider.calls, [])

    def test_recovery_preserves_completed_items_and_never_replays_ai(self):
        self.start()
        run = self.wait()['latest']
        with self.projects.connect(self.pid) as db:
            db.execute("UPDATE evidence_runs SET state='running' WHERE run_id=?", (run['run_id'],))
            db.execute("INSERT INTO evidence_observations(observation_id,run_id,item_key,request_id,state) VALUES('obs_unknown',?,'unknown','unknown-request','running')", (run['run_id'],))
        self.service.recover()
        recovered = self.service.status(self.pid)['latest']
        self.assertEqual(recovered['state'], 'interrupted')
        self.assertEqual(recovered['observations'][0]['state'], 'interrupted')
        self.service.resume(self.pid, run['run_id'])
        final = self.wait()['latest']
        self.assertEqual(final['results'], run['results'])
        self.assertEqual(FakeProvider.calls, [])

    def test_scope_change_blocks_old_results_and_resume(self):
        self.start()
        run = self.wait()['latest']
        with self.projects.connect(self.pid) as db:
            db.execute('UPDATE project SET revision=revision+1')
        self.assertTrue(self.service.status(self.pid)['latest']['stale'])
        key = next(r['key'] for r in run['results'] if r['kind'] == 'video')
        with self.assertRaises(ProjectError):
            self.service.observe(self.pid, run['run_id'], 'observe-request-1', key, True)
        self.assertFalse(FakeProvider.calls)

    def test_unchanged_local_results_reuse_verified_artifacts(self):
        self.start()
        first = self.wait()['latest']
        class NeverExtract(FakeExtractor):
            def frame(self, *args):
                raise AssertionError('cache should avoid extraction')
            def audio_features(self, *args):
                raise AssertionError('cache should avoid extraction')
        self.service.extractor_factory = NeverExtract
        scope = self.service.scope(self.pid)
        self.service.start(self.pid, 'evidence-request-2', scope['project_revision'], scope['scope_id'])
        second = self.wait()['latest']
        self.assertEqual(second['results'], first['results'])

    def test_scene_scores_feed_adaptive_selection_without_extra_ai_calls(self):
        class SceneExtractor(FakeExtractor):
            def scenes(self, source, cancel):
                span = source.source_span
                start = int(span.start_pts)
                duration = span.duration_pts
                candidates = [
                    {"pts": str(start + duration * numerator // 8), "score": score}
                    for numerator, score in ((1, 0.8), (3, 0.7), (5, 0.9), (7, 0.85))
                ]
                return {
                    "candidate_pts": [item["pts"] for item in candidates],
                    "candidates": candidates,
                    "maximum_window_seconds": 120,
                    "candidate_limit_reached": False,
                }

        self.service.extractor_factory = SceneExtractor
        self.start()
        video = next(
            result
            for result in self.wait()["latest"]["results"]
            if result["kind"] == "video"
        )
        selected = video["keyframe_selection"]["selected"]
        self.assertGreater(video["keyframe_selection"]["candidate_count"], 32)
        self.assertTrue(any(item["source"] == "scene" for item in selected))
        self.assertEqual(FakeProvider.calls, [])

    def test_invalid_command_identity_and_start_idempotency(self):
        initial = self.start()['latest']['run_id']
        self.wait()
        self.assertEqual(self.start()['latest']['run_id'], initial)
        for invalid in (None, {}, '../private', 'ev_wrong'):
            with self.subTest(invalid=invalid), self.assertRaises(ProjectError):
                self.service.cancel(self.pid, invalid)

    def test_replayed_start_returns_the_original_run(self):
        first = self.start()["latest"]["run_id"]
        self.wait()
        scope = self.service.scope(self.pid)
        second = self.service.start(
            self.pid,
            "evidence-request-2",
            scope["project_revision"],
            scope["scope_id"],
        )["latest"]["run_id"]
        self.wait()
        self.assertNotEqual(first, second)

        replay = self.start()["latest"]
        self.assertEqual(replay["run_id"], first)

    def test_scope_supports_more_than_one_hundred_streams(self):
        with self.projects.connect(self.pid) as db:
            row = db.execute("SELECT version_id,details_json FROM media_streams WHERE kind='video' LIMIT 1").fetchone()
            for index in range(2, 137):
                db.execute("INSERT INTO media_streams VALUES(?,?,'video','h264',?)", (row['version_id'], index, row['details_json']))
        self.assertEqual(len(self.service.scope(self.pid)['items']), 138)

    def test_scope_excludes_attached_cover_images(self):
        with self.projects.connect(self.pid) as db:
            row = db.execute("SELECT version_id,details_json FROM media_streams WHERE kind='video' LIMIT 1").fetchone()
            details = json.loads(row['details_json'])
            details['attached_pic'] = True
            db.execute("INSERT INTO media_streams VALUES(?,5,'video','mjpeg',?)", (row['version_id'], json.dumps(details)))
        self.assertEqual(len(self.service.scope(self.pid)['items']), 3)

    def test_cancel_and_resume_keep_completed_work(self):
        entered = threading.Event()
        class BlockExtractor(FakeExtractor):
            def frame(self, source, offset, output, cancel):
                entered.set()
                cancel.wait(3)
                from clipen.analysis.audio import AudioPreparationError
                raise AudioPreparationError('cancelled')
        self.service.extractor_factory = BlockExtractor
        run = self.start()['latest']['run_id']
        self.assertTrue(entered.wait(2))
        self.service.cancel(self.pid, run)
        self.assertEqual(self.wait()['latest']['state'], 'cancelled')
        self.service.extractor_factory = FakeExtractor
        self.service.resume(self.pid, run)
        self.assertEqual(self.wait()['latest']['state'], 'succeeded')

    def test_close_persists_interruption_and_drains_shutdown_sentinel(self):
        entered = threading.Event()
        release = threading.Event()

        class UncooperativeExtractor(FakeExtractor):
            def frame(self, source, offset, output, cancel):
                entered.set()
                release.wait(3)
                return super().frame(source, offset, output, cancel)

        self.service.extractor_factory = UncooperativeExtractor
        run_id = self.start()['latest']['run_id']
        self.assertTrue(entered.wait(2))

        self.service.close(timeout=0)
        interrupted = self.service.status(self.pid, run_id=run_id)['latest']
        self.assertEqual(interrupted['state'], 'interrupted')
        self.assertEqual(interrupted['error_code'], 'controller_stopped')

        release.set()
        self.service._worker.join(3)
        self.assertFalse(self.service._worker.is_alive())
        self.assertEqual(self.service._queue.unfinished_tasks, 0)
        self.service.close()

    def test_api_protects_image_and_mutation_routes(self):
        from clipen.controller import create_app, ControllerConfig, LocalSessionAuthority, ProviderStatus, ProviderState
        from test_project_api import request_app
        root = self.root / 'web'
        root.mkdir()
        (root / 'index.html').write_text('<html>test</html>')
        authority = LocalSessionAuthority()
        credentials = authority.connect_local_browser()
        app = create_app(ControllerConfig(root), ProviderStatus('Codex', ProviderState.READY, 'test'), authority, evidence_service=self.service)
        base = f'/api/projects/{self.pid}/evidence'
        headers = {'cookie': 'clipen_session=' + credentials.session_token}
        denied = asyncio.run(request_app(app, 'GET', base))
        self.assertEqual(denied[0], 401)
        status = asyncio.run(request_app(app, 'GET', base, headers=headers))
        self.assertEqual(status[0], 200)
        forbidden = asyncio.run(request_app(app, 'POST', base + '/commands/start', headers=headers, body=b'{}'))
        self.assertEqual(forbidden[0], 403)
        self.start()
        run = self.wait()['latest']
        frame = next(r for r in run['results'] if r['kind'] == 'video')['frames'][0]['artifact_id']
        image = asyncio.run(request_app(app, 'GET', base + '/' + run['run_id'] + '/images/' + frame, headers=headers))
        self.assertEqual(image[0], 200)
        self.assertEqual(image[2], PNG)
        self.assertIn('no-store', image[1]['cache-control'])
        rejected = asyncio.run(request_app(app, 'GET', base + '/' + run['run_id'] + '/images/art_unknown', headers=headers))
        self.assertEqual(rejected[0], 409)


    def test_recovery_reconciles_real_receipt_without_executing_again(self):
        from clipen.providers import CodexExecAdapter
        binary = self.root / 'receipt-provider'
        binary.write_text("#!/usr/bin/env python3\n" +
            "import json, pathlib, sys\n" +
            "args=sys.argv; prompt=sys.stdin.read()\n" +
            "output=pathlib.Path(args[args.index('--output-last-message')+1])\n" +
            "output.write_text(json.dumps({'observations':[{'index':i,'description':'test','uncertainty':'sample only'} for i in range(args.count('--image'))],'style_summary':'visible samples','limitations':'not full video'}))\n" +
            "pathlib.Path('executed-once').touch(exist_ok=False)\n" +
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':15,'cached_input_tokens':0,'output_tokens':8}}))\n")
        binary.chmod(0o700)
        class ReceiptProvider(CodexExecAdapter):
            def __init__(adapter, root):
                super().__init__(root, binary)
            def probe(adapter):
                return SimpleNamespace(available=True, version='fake-receipt')
        self.service.adapter_factory = ReceiptProvider
        self.start()
        run = self.wait()['latest']
        key = next(r['key'] for r in run['results'] if r['kind'] == 'video')
        self.service.observe(self.pid, run['run_id'], 'receipt-observation', key, True)
        original = self.wait()['latest']['observations'][0]
        self.assertEqual(original['state'], 'succeeded')
        with self.projects.connect(self.pid) as db:
            db.execute("UPDATE evidence_observations SET state='running',result_json=NULL,usage_json=NULL")
        self.service.recover()
        recovered = self.service.status(self.pid)['latest']['observations'][0]
        self.assertEqual(recovered['state'], 'succeeded')
        self.assertEqual(recovered['result'], original['result'])
        self.assertEqual(recovered['usage'], original['usage'])

    def test_observation_budget_is_reserved_before_provider_launch(self):
        self.start()
        run = self.wait()['latest']
        with self.projects.connect(self.pid) as db:
            for index in range(3):
                db.execute("INSERT INTO evidence_observations(observation_id,run_id,item_key,request_id,state) VALUES(?,?,?,?,'failed')",
                           ('obs_' + str(index)*32, run['run_id'], str(index), 'reserved-request-' + str(index)))
        key = next(r['key'] for r in run['results'] if r['kind'] == 'video')
        with self.assertRaises(ProjectError) as raised:
            self.service.observe(self.pid, run['run_id'], 'fourth-observation', key, True)
        self.assertEqual(raised.exception.code, 'observation_budget_exhausted')
        self.assertEqual(FakeProvider.calls, [])

    def test_results_are_paged_without_losing_total_progress(self):
        self.start()
        run = self.wait()['latest']
        first = run['results'][0]
        with self.projects.connect(self.pid) as db:
            for index in range(3, 30):
                copy = dict(first, key='extra-' + str(index))
                db.execute('INSERT INTO evidence_results VALUES(?,?,?,?,0)', (run['run_id'], index, copy['key'], json.dumps(copy)))
        page = self.service.status(self.pid, 25)['latest']
        self.assertEqual(len(page['results']), 5)
        self.assertEqual(page['completed_count'], 30)
        self.assertEqual(page['offset'], 25)
        with self.assertRaises(ProjectError):
            self.service.status(self.pid, -1)

    def test_v4_upgrade_preserves_project_and_preparation_tables(self):
        import sqlite3
        database = self.projects.root / self.pid / 'project.sqlite3'
        before = self.projects.get_project(self.pid)
        with sqlite3.connect(database) as db:
            for table in ('evidence_observations', 'evidence_images', 'evidence_results', 'evidence_cache', 'evidence_runs'):
                db.execute('DROP TABLE ' + table)
            db.execute('PRAGMA user_version=4')
        self.assertEqual(self.projects.get_project(self.pid), before)
        with self.projects.connect(self.pid) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 9)
            self.assertEqual(db.execute('SELECT count(*) FROM evidence_runs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM preparation_runs').fetchone()[0], 0)



if __name__ == '__main__':
    unittest.main()
