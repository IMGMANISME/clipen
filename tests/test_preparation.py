"""Real local draft persistence and HTTP boundaries; synthetic strings only."""
import http.client
import io
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from prototypes.local_ui.__main__ import ROOT
from prototypes.local_ui.http_ui import Server
from prototypes.local_ui.mcp import MCP
from prototypes.local_ui.runtime import Runtime
from prototypes.local_ui.store import Rejected, Store
from tools.skill_guard import SkillStore


def payload(request='draft-1', version=0, **fields):
    return {'request_id': request, 'expected_version': version,
            'source_directory': '/Volumes/Synthetic footage',
            'reference_directory': '', 'brief': '合成需求：保留三個重點。', **fields}


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clipen-preparation-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(lambda: self.store.close())

    def test_partial_draft_is_saved_without_reading_paths_or_creating_work(self):
        with patch.object(Path, 'open', side_effect=AssertionError('no file reads')):
            result = self.store.save_preparation(payload(brief='', source_directory='~/not yet chosen/$(no-shell)'))
        self.assertEqual(result, {'status': 'draft_saved', 'draft_version': 1})
        self.assertEqual(self.store.preparation()['source_directory'], '~/not yet chosen/$(no-shell)')
        self.assertEqual(self.store.snapshot(), {'jobs': [], 'events': []})
        self.assertIsNone(self.store.claim('any-agent'))

    def test_draft_reopens_without_provider_or_automatic_submission(self):
        self.store.save_preparation(payload())
        before = self.store.preparation()
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.preparation(), before)
        self.assertEqual(self.store.snapshot()['jobs'], [])

    def test_idempotent_receipt_does_not_overwrite_a_later_version(self):
        first = payload()
        receipt = self.store.save_preparation(first)
        self.store.save_preparation(payload('draft-2', 1, brief='New draft'))
        self.assertEqual(self.store.save_preparation(first), receipt)
        self.assertEqual(self.store.preparation()['brief'], 'New draft')
        self.assertEqual(self.store.preparation()['version'], 2)
        with self.assertRaisesRegex(Rejected, 'request_id_conflict'):
            self.store.save_preparation({**first, 'brief': 'Changed request'})

    def test_stale_version_does_not_replace_saved_draft(self):
        self.store.save_preparation(payload())
        with self.assertRaisesRegex(Rejected, 'draft_version_conflict'):
            self.store.save_preparation(payload('stale', 0, brief='Overwrite'))
        self.assertEqual(self.store.preparation()['version'], 1)

    def test_simultaneous_saves_only_accept_one_version(self):
        barrier = threading.Barrier(2)
        results = []

        def save(request):
            barrier.wait()
            try:
                results.append(self.store.save_preparation(payload(request)))
            except Rejected as error:
                results.append(error.code)

        threads = [threading.Thread(target=save, args=(str(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2)
        self.assertIn('draft_version_conflict', results)
        self.assertEqual(self.store.preparation()['version'], 1)

    def test_rejects_invalid_values_and_permission_fields(self):
        for changes in [
            {'brief': None}, {'brief': 'x' * 8001}, {'source_directory': '中' * 1366},
            {'source_directory': 'one\ntwo'}, {'reference_directory': '\0'},
            {'brief': '\ud800'}, {'brief': '\x1b[31m'},
            {'expected_version': True}, {'expected_version': -1}, {'expected_version': '0'},
            {'consent': True}, {'plan': {'approved': True}}, {'binding_id': 'pretend'},
        ]:
            with self.subTest(changes=str(changes)[:80]), self.assertRaises(Rejected):
                self.store.save_preparation(payload(**changes))
        self.assertEqual(self.store.preparation()['version'], 0)

    def test_utf8_boundaries_and_multiline_brief(self):
        self.store.save_preparation(payload(source_directory='😀' * 1024, brief='中' * 2666 + '\n\t'))
        self.assertEqual(self.store.preparation()['brief'], '中' * 2666 + '\n\t')

    def test_failed_receipt_write_rolls_back_draft(self):
        self.store.db.execute('''CREATE TRIGGER reject_receipt BEFORE INSERT ON commands
            BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_preparation(payload())
        self.assertEqual(self.store.preparation()['version'], 0)

    def test_v1_migration_preserves_text_jobs_and_receipts(self):
        body = {'request_id':'old-job', 'prompt':'Synthetic original',
                'consent':True, 'binding_id':'old'}
        receipt = self.store.create(body, 'old')
        work = self.store.claim('old')
        self.store.complete({**work, 'result':'Synthetic response'}, 'old')
        self.store.db.execute('DROP TABLE preparation')
        self.store.db.execute('PRAGMA user_version=1')
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertEqual(self.store.create(body, 'old'), receipt)
        self.assertEqual(self.store.snapshot()['jobs'][0]['result'], 'Synthetic response')
        self.assertEqual(self.store.preparation()['version'], 0)
        self.store.save_preparation(payload())
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)


class PreparationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clipen-draft-http-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Use the real immutable bundle without copying it into disposable storage.
        with patch('prototypes.local_ui.runtime.SkillStore', return_value=SkillStore(ROOT)):
            self.runtime = Runtime(self.root)
        self.addCleanup(self.runtime.close)
        self.server = Server(self.runtime)
        self.server.start(False)
        self.addCleanup(self.server.close)
        self.cookie = ''
        self.csrf = ''

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        actual = {'Origin':self.server.origin, 'Cookie':self.cookie,
                  'X-Clipen-CSRF':self.csrf, 'Content-Type':'application/json', **(headers or {})}
        try:
            connection.request('GET' if body is None else 'POST', path,
                               body=None if body is None else json.dumps(body), headers=actual)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def login(self):
        code, headers, _ = self.request('/api/session', {'token':self.server.entry})
        self.assertEqual(code, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        code, _, raw = self.request('/api/preparation')
        self.assertEqual(code, 200)
        self.csrf = json.loads(raw)['csrf']

    def test_read_and_write_require_session_origin_and_csrf(self):
        self.assertEqual(self.request('/api/preparation')[0], 401)
        self.assertEqual(self.request('/api/preparation', payload())[0], 401)
        self.login()
        for headers in [{'Origin':'http://attacker.invalid'}, {'Host':'attacker.invalid'},
                        {'X-Clipen-CSRF':'wrong'}, {'Sec-Fetch-Site':'cross-site'}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request('/api/preparation', payload(), headers)[0], 403)
        self.assertEqual(self.runtime.store.preparation()['version'], 0)

    def test_real_http_save_reload_and_version_conflict(self):
        self.login()
        self.assertEqual(self.request('/api/preparation', payload())[0], 202)
        saved = json.loads(self.request('/api/preparation')[2])
        self.assertEqual(saved['draft']['brief'], payload()['brief'])
        self.assertTrue(saved['capabilities']['draft_storage'])
        self.assertFalse(saved['capabilities']['media'])
        self.assertEqual(self.request('/api/preparation', payload('stale'))[0], 409)
        self.cookie = self.csrf = ''
        self.login()
        self.assertEqual(json.loads(self.request('/api/preparation')[2])['draft'], saved['draft'])
        self.assertEqual(self.runtime.queue_revision, 0)
        self.assertEqual(self.runtime.store.snapshot()['jobs'], [])

    def test_compact_etag_and_no_draft_in_mcp_or_public_routes(self):
        self.login()
        _, before, _ = self.request('/api/preparation')
        self.request('/api/preparation', payload())
        code, after, raw = self.request('/api/preparation', headers={'If-None-Match':before['ETag']})
        self.assertEqual(code, 200)
        self.assertNotIn('jobs', json.loads(raw))
        with patch.object(self.runtime.store, 'preparation', side_effect=AssertionError('idle DB read')):
            self.assertEqual(self.request('/api/preparation', headers={'If-None-Match':after['ETag']})[0], 304)
        self.runtime.connect({'name':'synthetic', 'version':'1'})
        self.assertNotIn(payload()['source_directory'], json.dumps(self.runtime.snapshot()))
        output = io.StringIO()
        mcp = MCP(self.runtime, output=output)
        mcp.initialized = True  # Synthetic initialized transport, not native AI verification.
        mcp.handle({'jsonrpc':'2.0', 'id':1, 'method':'tools/call',
                    'params':{'name':'clipen_connection_status', 'arguments':{}}})
        reply = json.loads(output.getvalue())['result']
        self.assertFalse(reply['isError'])
        self.assertTrue(json.loads(reply['content'][0]['text'])['transport_connected'])
        self.assertNotIn(payload()['source_directory'], output.getvalue())
        for path in ('/', '/diagnostics', '/workspace.js', '/workspace-state.js', '/workspace.css'):
            code, _, body = self.request(path)
            self.assertEqual(code, 200)
            self.assertNotIn(payload()['source_directory'].encode(), body)
            self.assertNotIn(self.server.entry.encode(), body)

    def test_shutdown_rejects_saved_session(self):
        self.login()
        self.runtime.stop.set()
        self.assertEqual(self.request('/api/preparation')[0], 503)
        self.assertEqual(self.request('/api/preparation', payload())[0], 503)

    def test_draft_is_not_an_executable_plan_or_media_endpoint(self):
        self.login()
        for path in ('/api/analyze', '/api/plans', '/api/preparation/start', '/api/directories'):
            self.assertEqual(self.request(path, payload())[0], 404)
        self.assertEqual(self.request('/api/preparation', payload(consent=True))[0], 422)


if __name__ == '__main__':
    unittest.main()
