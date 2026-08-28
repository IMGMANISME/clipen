"""Synthetic protocol/security tests. No provider, model, or private media access."""
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from prototypes.local_ui.__main__ import ROOT, codex_command
from prototypes.local_ui.http_ui import Server, decode_json
from prototypes.local_ui.mcp import MCP
from prototypes.local_ui.runtime import Runtime
from prototypes.local_ui.store import Rejected, Store


def cleanup(path):
    for root, dirs, files in os.walk(path):
        os.chmod(root, 0o700)
    shutil.rmtree(path)


class Temporary(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(cleanup, self.root)

    def make_runtime(self):
        shutil.copytree(ROOT / 'skills', self.root / 'skills')
        runtime = Runtime(self.root)
        self.addCleanup(runtime.close)
        return runtime


class StoreTests(Temporary):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root)
        self.addCleanup(self.close_store)
        self.binding = 'test-connection'

    def close_store(self):
        if self.store:
            self.store.close()
            self.store = None

    def create(self, request='request-1', **kw):
        return self.store.create({'request_id': request, 'consent': True, 'binding_id': self.binding,
                                  'prompt': 'Synthetic text only', **kw}, self.binding)

    def test_create_receipt_is_idempotent_and_payload_is_bound(self):
        one = self.create()
        self.assertEqual(one, self.create())
        with self.assertRaisesRegex(Rejected, 'request_id_conflict'):
            self.create(prompt='Different')
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)
        self.assertEqual(len(self.store.snapshot()['events']), 1)

    def test_consent_and_binding_are_required(self):
        for body in ({'consent': False}, {'consent': 'true'}, {'binding_id': 'old'}):
            with self.subTest(body=body), self.assertRaises(Rejected):
                self.create(**body)
        with self.assertRaises(Rejected):
            self.store.create({'request_id': 'x'}, None)
        self.assertEqual(self.store.snapshot()['jobs'], [])

    def test_text_limits_and_blank(self):
        for value in ('', '  ', 123, '中' * 1334, '\ud800'):
            with self.subTest(value=str(value)[:15]), self.assertRaises(Rejected):
                self.create(prompt=value)

    def test_only_one_delivery_and_completion_idempotency(self):
        self.create()
        self.create('request-2')
        work = self.store.claim(self.binding)
        self.assertIsNone(self.store.claim(self.binding))
        body = {'job_id': work['job_id'], 'attempt_id': work['attempt_id'], 'result': 'Test fixture response'}
        self.assertEqual(self.store.complete(body, self.binding), self.store.complete(body, self.binding))
        with self.assertRaises(Rejected):
            self.store.complete({**body, 'result': 'Changed'}, self.binding)
        self.assertIsNotNone(self.store.claim(self.binding))

    def test_cancel_stale_results_and_duplicate_cancel(self):
        self.create()
        work = self.store.claim(self.binding)
        body = {'request_id': 'cancel-1', 'expected_job_version': 2}
        receipt = self.store.change(work['job_id'], 'cancel', body, self.binding)
        self.assertEqual(receipt, self.store.change(work['job_id'], 'cancel', body, self.binding))
        with self.assertRaises(Rejected):
            self.store.complete({**work, 'result': 'Late'}, self.binding)
        self.assertEqual(self.store.snapshot()['jobs'][0]['status'], 'cancelled')

    def test_version_required_and_conflict(self):
        job = self.create()['job_id']
        for version in (None, True, 0, '1'):
            with self.subTest(version=version), self.assertRaises(Rejected):
                self.store.change(job, 'cancel', {'request_id': 'v', 'expected_job_version': version}, self.binding)
        self.assertEqual(self.store.snapshot()['jobs'][0]['status'], 'queued')

    def test_restart_preserves_completed_cancelled_and_marks_unknown(self):
        first = self.create()['job_id']
        work = self.store.claim(self.binding)
        self.store.complete({**work, 'result': 'Test result'}, self.binding)
        cancelled = self.create('cancelled')['job_id']
        self.store.change(cancelled, 'cancel', {'request_id': 'cancel', 'expected_job_version': 1}, self.binding)
        delivered = self.create('delivered')['job_id']
        self.store.claim(self.binding)
        queued = self.create('queued')['job_id']
        self.close_store()
        self.store = Store(self.root)
        statuses = {r['id']: r['status'] for r in self.store.snapshot()['jobs']}
        self.assertEqual(statuses, {first: 'completed', cancelled: 'cancelled', delivered: 'uncertain', queued: 'uncertain'})
        self.assertIsNone(self.store.claim('new'))

    def test_retry_requires_fresh_consent_and_new_attempt(self):
        job = self.create()['job_id']
        old = self.store.claim(self.binding)
        self.store.disconnect(self.binding)
        body = {'request_id': 'retry', 'expected_job_version': 3, 'consent': True, 'binding_id': 'new'}
        self.store.change(job, 'retry', body, 'new')
        work = self.store.claim('new')
        self.assertNotEqual(work['attempt_id'], old['attempt_id'])
        with self.assertRaises(Rejected):
            self.store.complete({**old, 'result': 'late'}, self.binding)

    def test_transaction_rolls_back_if_event_write_fails(self):
        with patch.object(self.store, 'event', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.create()
        self.assertEqual(self.store.snapshot()['jobs'], [])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 0)

    def test_single_writer_and_private_files(self):
        with self.assertRaisesRegex(Rejected, 'workspace_already_running'):
            Store(self.root)
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.store.directory / 'state.sqlite').stat().st_mode), 0o600)
        self.assertEqual(self.store.db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assertEqual(self.store.db.execute('PRAGMA synchronous').fetchone()[0], 2)

    def test_unsupported_schema_fails_without_recreation(self):
        self.store.db.execute('PRAGMA user_version=99')
        self.close_store()
        with self.assertRaisesRegex(Rejected, 'unsupported_state_version'):
            Store(self.root)


class FilesystemTests(Temporary):
    def test_symlink_state_directory_rejected(self):
        (self.root / '.clipen').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(Rejected):
            Store(self.root)

    def test_public_state_directory_rejected(self):
        (self.root / '.clipen').mkdir(mode=0o755)
        with self.assertRaises(Rejected):
            Store(self.root)

    def test_linked_database_and_sidecars_rejected(self):
        for name, kind in [('state.sqlite', 'symlink'), ('state.sqlite', 'hardlink'), ('state.sqlite-journal', 'symlink')]:
            with self.subTest(name=name, kind=kind):
                sub = self.root / (name + kind)
                sub.mkdir()
                (sub / '.clipen').mkdir(mode=0o700)
                directory = sub / '.clipen' / 'prototype'
                directory.mkdir(mode=0o700)
                target = sub / 'target'
                target.write_text('DO NOT TOUCH')
                target.chmod(0o600)
                if kind == 'symlink':
                    (directory / name).symlink_to(target)
                else:
                    os.link(target, directory / name)
                with self.assertRaises((Rejected, OSError)):
                    Store(sub)
                self.assertEqual(target.read_text(), 'DO NOT TOUCH')


class HTTPTests(Temporary):
    def setUp(self):
        super().setUp()
        self.runtime = self.make_runtime()
        self.server = Server(self.runtime)
        self.server.start(False)
        self.addCleanup(self.server.close)
        self.cookie = None
        self.csrf = None

    def request(self, path, body=None, extra=None, method=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Origin': self.server.origin}
        if self.cookie:
            headers['Cookie'] = self.cookie
        if self.csrf:
            headers['X-Clipen-CSRF'] = self.csrf
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(body).encode()
        headers.update(extra or {})
        connection.request(method or ('POST' if body is not None else 'GET'), path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, dict(response.getheaders()), raw)
        connection.close()
        return result

    def login(self):
        code, headers, raw = self.request('/api/session', {'token': self.server.bootstrap})
        self.assertEqual(code, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        code, _, raw = self.request('/api/state')
        self.assertEqual(code, 200)
        self.csrf = json.loads(raw)['csrf']

    def test_bootstrap_once_and_auth_required(self):
        token = self.server.bootstrap
        self.assertEqual(self.request('/api/state')[0], 401)
        self.login()
        self.assertEqual(self.request('/api/session', {'token': token})[0], 401)
        self.assertEqual(self.request('/api/state')[0], 200)

    def test_expired_token(self):
        self.server.expires = time.monotonic() - 1
        self.assertEqual(self.request('/api/session', {'token': self.server.bootstrap})[0], 401)

    def test_terminal_link_reopens_after_bootstrap_consumed_and_expired(self):
        token = self.server.entry
        self.login()
        self.server.expires = time.monotonic() - 1
        for _ in range(2):
            self.cookie = self.csrf = None  # A new browser session each time.
            code, headers, _ = self.request('/api/session', {'token': token})
            self.assertEqual(code, 200)
            self.cookie = headers['Set-Cookie'].split(';')[0]
            self.assertEqual(self.request('/api/state')[0], 200)

    def test_terminal_link_requires_exact_token_and_origin(self):
        for token in ('', None, {}, 'x' * 43, '錯誤連結'):
            self.assertEqual(self.request('/api/session', {'token': token})[0], 401)
        for extra in ({'Origin': 'http://attacker.example'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request('/api/session', {'token': self.server.entry}, extra)[0], 403)

    def test_shutdown_rejects_existing_session_and_terminal_link(self):
        self.login()
        token = self.server.entry
        self.runtime.stop.set()
        self.assertEqual(self.request('/api/state')[0], 503)
        self.assertEqual(self.request('/api/session', {'token': token})[0], 503)
        self.assertEqual(self.request('/api/jobs', {})[0], 503)

    def test_terminal_token_is_not_returned_by_any_public_or_authenticated_get(self):
        self.login()
        for path in ('/', '/app.js', '/health', '/api/state'):
            code, _, raw = self.request(path)
            self.assertEqual(code, 200)
            self.assertNotIn(self.server.entry.encode(), raw)

    def test_state_etag_is_authenticated_and_invalidated_on_changes(self):
        self.login()
        _, headers, raw = self.request('/api/state')
        etag = headers['ETag']
        with patch.object(self.runtime.store, 'snapshot', side_effect=AssertionError('idle DB read')):
            code, unchanged_headers, unchanged = self.request('/api/state', extra={'If-None-Match': etag})
        self.assertEqual((code, unchanged), (304, b''))
        self.assertNotIn('Content-Length', unchanged_headers)
        cookie = self.cookie
        self.cookie = None
        self.assertEqual(self.request('/api/state', extra={'If-None-Match': etag})[0], 401)
        self.cookie = cookie
        self.runtime.connect({'name': 'synthetic', 'version': '1'})
        code, changed_headers, changed = self.request('/api/state', extra={'If-None-Match': etag})
        self.assertEqual(code, 200)
        self.assertNotEqual(changed_headers['ETag'], etag)
        self.assertNotEqual(raw, changed)
        self.runtime.command('/api/jobs', {'request_id': 'cache-test', 'prompt': 'Synthetic cache task',
                                         'consent': True, 'binding_id': self.runtime.binding})
        code, job_headers, job_raw = self.request('/api/state', extra={'If-None-Match': changed_headers['ETag']})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(job_raw)['jobs'][0]['status'], 'queued')
        work = self.runtime.next_work(threading.Event(), 0)
        code, delivered_headers, delivered_raw = self.request('/api/state', extra={'If-None-Match': job_headers['ETag']})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(delivered_raw)['jobs'][0]['status'], 'delivered')
        self.runtime.complete({'job_id': work['job_id'], 'attempt_id': work['attempt_id'], 'result': 'Synthetic reply'})
        code, completed_headers, completed_raw = self.request('/api/state', extra={'If-None-Match': delivered_headers['ETag']})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(completed_raw)['jobs'][0]['result'], 'Synthetic reply')
        with patch.object(self.runtime.store, 'snapshot', side_effect=AssertionError('cached completion must not query')):
            self.assertEqual(self.request('/api/state', extra={'If-None-Match': completed_headers['ETag']})[0], 304)
        self.runtime.stop.set()
        self.assertEqual(self.request('/api/state', extra={'If-None-Match': completed_headers['ETag']})[0], 503)

    def test_non_ascii_authentication_fails_without_internal_error(self):
        self.login()
        self.assertEqual(self.request('/api/jobs', {}, {'X-Clipen-CSRF': 'é'})[0], 403)
        self.assertEqual(self.request('/api/state', extra={'Cookie': f'{self.server.cookie_name}="é"'})[0], 401)

    def test_host_origin_fetch_metadata_and_csrf(self):
        self.login()
        for header in ({'Host': 'attacker.example'}, {'Origin': 'http://127.0.0.1:9'}, {'Sec-Fetch-Site': 'same-site'}, {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(header=header):
                self.assertEqual(self.request('/api/state', extra=header)[0], 403)
        self.assertEqual(self.request('/api/jobs', {}, {'X-Clipen-CSRF': 'wrong'})[0], 403)
        self.assertEqual(self.request('/api/jobs', {}, {'Origin': ''})[0], 403)

    def test_body_type_limit_and_unknown_routes(self):
        self.login()
        self.assertEqual(self.request('/api/jobs', {}, {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('/api/jobs', {'prompt': 'x' * 33000})[0], 413)
        self.assertEqual(self.request('/api/agent/complete', {})[0], 404)
        self.assertEqual(self.request('/api/system-skills', {})[0], 404)
        self.assertEqual(self.request('/../../.env')[0], 404)

    def test_actual_queue_from_http_to_tool_and_back(self):
        self.login()
        self.runtime.connect({'name': 'synthetic-protocol-client', 'version': 'test'})
        code, _, raw = self.request('/api/jobs', {'request_id': 'http-job', 'prompt': '合成測試文字', 'consent': True,
                                                 'binding_id': self.runtime.binding})
        self.assertEqual(code, 202)
        work = self.runtime.next_work(threading.Event(), wait_seconds=0)
        self.assertEqual(work['user_text'], '合成測試文字')
        self.runtime.complete({**work, 'result': '合成協定回覆（不是 AI）'})
        data = json.loads(self.request('/api/state')[2])
        self.assertEqual(data['jobs'][0]['status'], 'completed')
        self.assertFalse(data['connection']['provider_session_verified'])
        self.assertNotIn('skills', data)
        self.assertEqual(data['system_policy'], {'verified': True})
        self.assertIsNone(data['plan'])
        self.assertFalse(data['capabilities']['plan_generation'])
        self.assertFalse(data['capabilities']['plan_editing'])

    def test_verified_system_instructions_never_appear_in_browser_responses(self):
        self.login()
        system_skills = self.runtime.skills.verify_system()
        self.assertEqual(len(system_skills), 5)
        for path in ('/', '/app.js', '/ui-state.js', '/app.css', '/health', '/api/state'):
            code, _, raw = self.request(path)
            self.assertEqual(code, 200)
            decoded = raw.decode()
            for skill in system_skills.values():
                self.assertNotIn(skill.id, decoded)
                self.assertNotIn(skill.content, decoded)
            if path == '/api/state':
                self.assertNotIn('skills', json.loads(raw))

    def test_system_files_and_skill_endpoints_are_not_browser_routes(self):
        self.login()
        for path in ('/api/skills', '/api/system-skills', '/api/skills/system/clipen-core',
                     '/skills/system/clipen-core/SKILL.md', '/skills/system-manifest.json',
                     '/tools/skill_guard.py', '/api/state?include=skills'):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/api/plans', {})[0], 404)

    def test_hidden_skills_still_fail_closed_when_bundle_is_tampered(self):
        root = self.root / 'tampered-copy'
        root.mkdir()
        shutil.copytree(ROOT / 'skills', root / 'skills')
        core = root / 'skills/system/clipen-core/SKILL.md'
        core.chmod(0o600)
        core.write_text('Tampered internal policy')
        from tools.skill_guard import SkillError
        with self.assertRaises(SkillError):
            Runtime(root)
        self.assertFalse((root / '.clipen').exists())

    def test_static_security_headers_and_no_token_in_body(self):
        code, headers, raw = self.request('/')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertNotIn(self.server.bootstrap.encode(), raw)
        self.assertNotIn(self.server.session.encode(), raw)
        self.assertEqual(self.request('/app.js')[0], 200)
        self.assertEqual(self.request('/health')[0], 200)

    def test_no_connected_agent_rejects_send(self):
        self.login()
        self.assertEqual(self.request('/api/jobs', {'request_id': 'x', 'prompt': 'test', 'consent': True})[0], 503)


class ProtocolTests(Temporary):
    def setUp(self):
        super().setUp()
        self.runtime = self.make_runtime()
        # MCP owns runtime.close; make Runtime close idempotent for test teardown.
        self.output = io.StringIO()
        self.opened = []
        self.announced = []
        self.mcp = MCP(self.runtime, output=self.output, opener=lambda url, **kw: self.opened.append(url) or True,
                       notifier=lambda url: self.announced.append(url) or True)
        self.addCleanup(self.close_mcp)

    def close_mcp(self):
        if self.mcp.ui:
            self.mcp.ui.close()
            self.mcp.ui = None

    def send(self, method, params=None, rid=1):
        message = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if rid is not None:
            message['id'] = rid
        self.mcp.handle(message)

    def initialize(self, version='2025-06-18'):
        self.send('initialize', {'protocolVersion': version, 'clientInfo': {'name': 'test-client', 'version': '1'}})
        self.send('notifications/initialized', rid=None)

    def last(self):
        return json.loads(self.output.getvalue().splitlines()[-1])

    def test_opens_only_after_initialized_and_once(self):
        self.send('initialize', {'protocolVersion': '2024-11-05', 'clientInfo': {'name': 'test', 'version': '1'}})
        self.assertEqual(self.last()['result']['protocolVersion'], '2024-11-05')
        self.assertEqual(self.opened, [])
        self.send('notifications/initialized', rid=None)
        self.send('notifications/initialized', rid=None)
        self.assertEqual(len(self.opened), 1)
        self.assertNotIn(self.mcp.ui.bootstrap, self.output.getvalue())

    def test_terminal_link_announced_once_and_never_in_mcp_or_stderr(self):
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            self.initialize()
            self.send('notifications/initialized', rid=None)
            self.send('tools/call', {'name': 'clipen_connection_status'})
        self.assertEqual(self.announced, [self.mcp.ui.reopen_url()])
        self.assertNotIn(self.mcp.ui.entry, self.output.getvalue())
        self.assertNotIn(self.mcp.ui.entry, stderr.getvalue())
        self.assertNotIn(self.mcp.ui.bootstrap, stderr.getvalue())

    def test_handshake_not_readiness_and_only_product_tools(self):
        self.initialize()
        state = self.runtime.snapshot()
        self.assertTrue(state['connection']['transport_connected'])
        self.assertFalse(state['connection']['waiting_for_work'])
        self.send('tools/list', rid=2)
        self.assertEqual(len(self.last()['result']['tools']), 4)
        self.assertNotIn('shell', [t['name'] for t in self.last()['result']['tools']])

    def test_two_tool_roundtrips_on_one_synthetic_connection(self):
        self.initialize()
        binding = self.runtime.binding
        for index in range(2):
            self.runtime.command('/api/jobs', {'request_id': f'roundtrip-{index}', 'prompt': 'Synthetic task',
                                              'consent': True, 'binding_id': binding})
            self.send('tools/call', {'name': 'clipen_next_work', 'arguments': {}}, rid=10 + index)
            with self.mcp.pending_lock:
                entries = list(self.mcp.pending.values())
            for _, thread in entries:
                thread.join(2)
                self.assertFalse(thread.is_alive())
            response = self.last()
            self.assertEqual(response['id'], 10 + index)
            work = json.loads(response['result']['content'][0]['text'])
            self.assertEqual(work['binding_id'], binding)
            self.send('tools/call', {'name': 'clipen_complete_work', 'arguments': {
                'job_id': work['job_id'], 'attempt_id': work['attempt_id'], 'result': 'Not AI; protocol fixture'}}, rid=20 + index)
            self.assertFalse(self.last()['result']['isError'])
        self.assertTrue(all(row['status'] == 'completed' for row in self.runtime.snapshot()['jobs']))

    def test_uninitialized_tools_rejected(self):
        self.send('tools/list')
        self.assertEqual(self.last()['error']['code'], -32002)
        self.assertEqual(self.opened, [])

    def test_malformed_protocol_version_never_crashes(self):
        self.send('initialize', {'protocolVersion': [], 'clientInfo': {'name': 'test', 'version': '1'}})
        self.assertEqual(self.last()['result']['protocolVersion'], '2025-06-18')

    def test_bad_tool_args_and_unknown_method(self):
        self.initialize()
        for args in ([], {'name': 'shell', 'arguments': {}}, {'name': 'clipen_next_work', 'arguments': {'path': '/'}}):
            if isinstance(args, list):
                params = {'name': 'clipen_next_work', 'arguments': args}
            else:
                params = args
            self.send('tools/call', params)
            self.assertEqual(self.last()['error']['code'], -32602)
        self.send('arbitrary_method')
        self.assertEqual(self.last()['error']['code'], -32601)

    def test_next_work_cancellable_without_blocking_ping(self):
        self.initialize()
        self.send('tools/call', {'name': 'clipen_next_work', 'arguments': {}}, rid=2)
        self.send('ping', rid=3)
        self.assertEqual(self.last()['id'], 3)
        self.send('notifications/cancelled', {'requestId': 2}, rid=None)
        with self.mcp.pending_lock:
            entries = list(self.mcp.pending.values())
        for _, thread in entries:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertFalse(self.runtime.polling)

    def test_strict_json_parser(self):
        for value in (b'[]', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'{"x":"\\ud800"}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_json(value)

    def test_launcher_has_ephemeral_config_and_no_permission_bypass(self):
        command = codex_command(ROOT, '/bin/codex')
        self.assertEqual(command[0], '/bin/codex')
        for flag in ('--dangerously-bypass-approvals-and-sandbox', '--full-auto', '--oss'):
            self.assertNotIn(flag, command)
        self.assertIn('clipen_next_work', command[-1])
        connection_only = codex_command(ROOT, '/bin/codex', True)
        self.assertFalse(any('You are the AI' in arg for arg in connection_only))


class SubprocessTests(Temporary):
    def test_duplicate_discovery_lists_tools_without_owning_or_reading_workspace(self):
        runtime = self.make_runtime()
        runtime.connect({'name': 'owner', 'version': '1'})
        runtime.command('/api/jobs', {'request_id': 'private-job', 'prompt': 'PRIVATE OWNER TEXT',
                                    'consent': True, 'binding_id': runtime.binding})
        output = io.StringIO()
        secondary = MCP(runtime_factory=lambda: Runtime(self.root), output=output,
                        opener=lambda *_: self.fail('Discovery must not open UI'),
                        notifier=lambda *_: self.fail('Discovery must not reveal link'))
        secondary.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'clientInfo': {'name': 'inventory', 'version': '1'}}})
        secondary.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        secondary.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        self.assertEqual(len(json.loads(output.getvalue().splitlines()[-1])['result']['tools']), 4)
        self.assertIsNone(secondary.runtime)
        self.assertIsNone(secondary.ui)
        for name in ('clipen_next_work', 'clipen_complete_work'):
            secondary.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': name}})
            result = json.loads(output.getvalue().splitlines()[-1])['result']
            self.assertTrue(result['isError'])
            self.assertIn('workspace_already_running', result['content'][0]['text'])
        secondary.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'clipen_connection_status'}})
        status = json.loads(json.loads(output.getvalue().splitlines()[-1])['result']['content'][0]['text'])
        self.assertFalse(status['transport_connected'])
        self.assertNotIn('PRIVATE OWNER TEXT', output.getvalue())
        self.assertNotIn(runtime.binding, output.getvalue())
        # Closing a secondary connection cannot stop or mutate its owner.
        secondary.source = io.BytesIO()
        secondary.run()
        self.assertFalse(runtime.stop.is_set())
        self.assertEqual(runtime.snapshot()['jobs'][0]['status'], 'queued')

    def test_uninitialized_discovery_does_not_create_state(self):
        output = io.StringIO()
        mcp = MCP(runtime_factory=lambda: self.fail('Must wait for initialized'), output=output,
                  source=io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'))
        mcp.run()
        self.assertFalse((self.root / '.clipen').exists())

    def test_stdio_entry_point_and_eof(self):
        shutil.copytree(ROOT / 'skills', self.root / 'skills')
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'clientInfo': {'name': 'test-fixture', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        ]
        output = subprocess.run([sys.executable, str(ROOT / 'integrations/stdio_server.py'), '--workspace', str(self.root), '--no-open'],
                                input=''.join(json.dumps(m) + '\n' for m in messages), capture_output=True, text=True, timeout=10, cwd='/tmp')
        self.assertEqual(output.returncode, 0, output.stderr)
        responses = [json.loads(line) for line in output.stdout.splitlines()]
        self.assertEqual([m['id'] for m in responses], [1, 2])
        self.assertEqual(len(responses[1]['result']['tools']), 4)
        store = Store(self.root)
        store.close()  # EOF released the writer lock.


if __name__ == '__main__':
    unittest.main()
