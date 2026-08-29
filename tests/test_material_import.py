"""Local material inventory boundaries; all media and paths are disposable fixtures."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from prototypes.local_ui.__main__ import ROOT
from prototypes.local_ui.media_import import ScanCancelled, scan_materials, validate_roots
from prototypes.local_ui.runtime import Runtime
from prototypes.local_ui.store import Rejected, Store
from tools.skill_guard import SkillStore


def draft(root, request='draft-1', version=0):
    return {'request_id': request, 'expected_version': version,
            'source_directory': str(root), 'reference_directory': '', 'brief': '合成測試'}


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clipen-media-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_bounded_inventory_reads_regular_media_but_never_follows_links(self):
        (self.root / 'nested').mkdir()
        (self.root / 'clip.mp4').write_bytes(b'synthetic')
        (self.root / 'still.png').write_bytes(b'\x89PNG\r\n\x1a\n\0\0\0\rIHDR' +
                                              (320).to_bytes(4,'big') + (180).to_bytes(4,'big'))
        (self.root / 'old.wmv').write_bytes(b'synthetic')
        os.symlink(self.root / 'clip.mp4', self.root / 'linked.mov')
        with patch('prototypes.local_ui.media_import._probe', return_value=(
                {'duration': 12.5, 'codec': 'fixture', 'width': 1920, 'height': 1080}, None)) as probe:
            entries = scan_materials(str(self.root), '', threading.Event(), ffprobe='/fixture/ffprobe')
        self.assertEqual([item['path'] for item in entries], ['clip.mp4', 'linked.mov', 'old.wmv', 'still.png'])
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(entries[0]['duration'], 12.5)
        self.assertEqual(entries[1]['issue'], 'symlink_skipped')
        self.assertEqual(entries[2]['issue'], 'unsupported_format')
        self.assertEqual(entries[3]['kind'], 'image')
        self.assertEqual((entries[3]['width'], entries[3]['height']), (320, 180))

    def test_invalid_root_and_cancellation_fail_closed(self):
        with self.assertRaisesRegex(Rejected, 'must_be_absolute'):
            validate_roots('relative/path')
        os.symlink(self.root, self.root.parent / (self.root.name + '-link'))
        self.addCleanup(lambda: (self.root.parent / (self.root.name + '-link')).unlink(missing_ok=True))
        with self.assertRaisesRegex(Rejected, 'unavailable'):
            validate_roots(str(self.root.parent / (self.root.name + '-link')))
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(ScanCancelled):
            scan_materials(str(self.root), '', cancel, ffprobe='')

    def test_file_and_depth_limits_are_reported_instead_of_silently_truncating(self):
        (self.root / 'one.png').write_bytes(b'bad')
        (self.root / 'two.png').write_bytes(b'bad')
        with patch('prototypes.local_ui.media_import.MAX_FILES', 1), self.assertRaisesRegex(Rejected, 'file_limit'):
            scan_materials(str(self.root), '', threading.Event(), ffprobe='')
        child = self.root / 'child'; child.mkdir()
        with patch('prototypes.local_ui.media_import.MAX_DEPTH', 0):
            entries = scan_materials(str(self.root), '', threading.Event(), ffprobe='')
        self.assertIn('directory_depth_limit', [item.get('issue') for item in entries])


class MaterialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clipen-import-store-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.materials = self.root / 'materials'; self.materials.mkdir()
        self.store = Store(self.root)
        self.addCleanup(lambda: self.store.close())
        self.store.save_preparation(draft(self.materials))

    def start(self, request='import-1', consent=True, version=1):
        return self.store.start_material_import({'request_id': request,
            'expected_draft_version': version, 'consent': consent})

    def test_explicit_consent_uses_saved_version_and_is_idempotent(self):
        with self.assertRaisesRegex(Rejected, 'consent'):
            self.start(consent=False)
        receipt = self.start()
        self.assertEqual(self.start(), receipt)
        state = self.store.material_import()
        self.assertEqual(state['roots']['source'], str(self.materials))
        self.assertEqual(state['draft_version'], 1)
        self.assertEqual(self.store.snapshot()['jobs'], [])
        with self.assertRaisesRegex(Rejected, 'request_id_conflict'):
            self.store.start_material_import({'request_id':'import-1', 'expected_draft_version':2, 'consent':True})

    def test_progress_completion_and_interruption_are_durable(self):
        import_id = self.start()['import_id']
        entries = [{'role':'source','path':'a.mp4','kind':'media','duration':3.25,'size':2}]
        self.store.update_material_import(import_id, entries)
        self.assertEqual(self.store.material_import()['duration'], 3.25)
        self.store.close(); self.store = Store(self.root)
        self.assertEqual(self.store.material_import()['status'], 'interrupted')
        self.assertEqual(self.store.material_import()['entries'], entries)

    def test_cancel_is_versioned_and_cannot_cancel_another_import(self):
        import_id = self.start()['import_id']
        body = {'request_id':'cancel-1', 'import_id':import_id}
        self.assertEqual(self.store.cancel_material_import(body)['status'], 'cancelling')
        self.assertEqual(self.store.cancel_material_import(body)['status'], 'cancelling')
        with self.assertRaisesRegex(Rejected, 'not_found'):
            self.store.cancel_material_import({'request_id':'cancel-2','import_id':'import_other'})


class MaterialRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clipen-import-runtime-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.materials = self.root / 'materials'; self.materials.mkdir()
        with patch('prototypes.local_ui.runtime.SkillStore', return_value=SkillStore(ROOT)):
            self.runtime = Runtime(self.root)
        self.addCleanup(self.runtime.close)
        self.runtime.command('/api/preparation', draft(self.materials))

    def start_body(self, request='start-1'):
        return {'request_id':request, 'expected_draft_version':1, 'consent':True}

    def test_worker_persists_completed_inventory_without_ai_work(self):
        entries = [{'role':'source','path':'fixture.mp4','kind':'media','duration':4.5,'size':12}]
        with patch('prototypes.local_ui.runtime.scan_materials', return_value=entries):
            receipt = self.runtime.command('/api/material-import', self.start_body())
            self.runtime._import_thread.join(2)
        state = self.runtime.store.material_import()
        self.assertEqual(state['import_id'], receipt['import_id'])
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['entries'], entries)
        self.assertEqual(self.runtime.store.snapshot()['jobs'], [])
        encoded = json.loads(self.runtime.encoded_preparation('csrf')[1])
        self.assertTrue(encoded['capabilities']['media_inventory'])
        self.assertFalse(encoded['capabilities']['media'])

    def test_cancel_stops_worker_and_preserves_partial_inventory(self):
        entered = threading.Event()
        def scanner(_source, _reference, cancel, progress):
            progress([{'role':'source','path':f'{index}.png','kind':'image','duration':0,'size':1}
                      for index in range(5)])
            entered.set(); cancel.wait(2); raise ScanCancelled()
        with patch('prototypes.local_ui.runtime.scan_materials', side_effect=scanner):
            receipt = self.runtime.command('/api/material-import', self.start_body())
            self.assertTrue(entered.wait(1))
            result = self.runtime.command('/api/material-import/cancel',
                {'request_id':'cancel-1','import_id':receipt['import_id']})
            self.assertEqual(result['status'], 'cancelling')
            self.runtime._import_thread.join(2)
        state = self.runtime.store.material_import()
        self.assertEqual(state['status'], 'cancelled')
        self.assertEqual(state['entries'][0]['path'], '0.png')

    def test_replayed_start_receipt_never_creates_a_second_scan(self):
        entered, release = threading.Event(), threading.Event()
        calls = 0
        def scanner(_source, _reference, cancel, progress):
            nonlocal calls
            calls += 1; entered.set(); release.wait(2); return []
        body = self.start_body('same-start')
        with patch('prototypes.local_ui.runtime.scan_materials', side_effect=scanner):
            first = self.runtime.command('/api/material-import', body)
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.runtime.command('/api/material-import', body), first)
            release.set(); self.runtime._import_thread.join(2)
        self.assertEqual(calls, 1)

    def test_service_close_marks_active_scan_interrupted(self):
        entered = threading.Event()
        def scanner(_source, _reference, cancel, progress):
            entered.set(); cancel.wait(2); raise ScanCancelled()
        with patch('prototypes.local_ui.runtime.scan_materials', side_effect=scanner):
            self.runtime.command('/api/material-import', self.start_body('closing-start'))
            self.assertTrue(entered.wait(1))
            self.runtime.close()
        reopened = Store(self.root)
        try:
            state = reopened.material_import()
            self.assertEqual(state['status'], 'interrupted')
            self.assertEqual(state['issue'], 'service_interrupted')
        finally:
            reopened.close()


if __name__ == '__main__':
    unittest.main()
