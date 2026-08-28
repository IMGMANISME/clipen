"""Synthetic protocol tests only. No provider process, credentials, or model."""
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock

from prototypes.local_ui.__main__ import ROOT
from prototypes.local_ui.codex_binding import proof_from_result, verify_mcp_route
from prototypes.local_ui.mcp import MCP
from prototypes.local_ui.runtime import Runtime
from prototypes.local_ui.session_binding import Candidate, SessionBinding
from prototypes.local_ui.store import Rejected


SESSION = '00000000-0000-4000-8000-000000000001'
THREAD = '00000000-0000-4000-8000-000000000002'
OTHER = '00000000-0000-4000-8000-000000000003'


def thread(candidate):
    return {'thread': {'id': candidate.thread_id, 'sessionId': candidate.session_id,
                       'parentThreadId': None, 'agentRole': None, 'agentNickname': None,
                       'source': 'cli', 'cwd': str(candidate.workspace), 'status': {'type': 'idle'}}}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.gate = SessionBinding(clock=lambda: self.now)
        self.candidate = Candidate(SESSION, THREAD, Path('/synthetic'))

    def test_identity_fields_are_distinct_and_canonical(self):
        self.candidate.check_thread(thread(self.candidate))
        for value in (None, 7, 'latest', '../session', SESSION.upper().replace('8000', 'AAAA'), '{'+SESSION+'}', 'session-name'):
            with self.subTest(value=value), self.assertRaises(Rejected):
                Candidate(value, THREAD, Path('/synthetic'))
        with self.assertRaises(Rejected):
            Candidate(SESSION, THREAD, Path('relative'))

    def test_cwd_or_history_never_proves_identity(self):
        variants = [
            ('id', OTHER), ('sessionId', THREAD), ('parentThreadId', SESSION),
            ('agentRole', 'worker'), ('agentNickname', 'worker'), ('source', 'exec'),
            ('cwd', '/another'), ('status', {'type': 'notLoaded'}),
            ('status', {'type': 'systemError'}), ('status', None),
        ]
        for key, value in variants:
            with self.subTest(key=key, value=value), self.assertRaises(Rejected):
                response = thread(self.candidate)
                response['thread'][key] = value
                self.candidate.check_thread(response)
        response = thread(self.candidate)
        del response['thread']['parentThreadId']
        with self.assertRaises(Rejected):
            self.candidate.check_thread(response)
        for value in (None, [], {}, {'thread': []}):
            with self.subTest(value=value), self.assertRaises(Rejected):
                self.candidate.check_thread(value)

    def test_random_challenge_must_reach_the_original_gate(self):
        challenge = self.gate.begin(self.candidate)
        other = SessionBinding()
        other.begin(self.candidate)
        with self.assertRaisesRegex(Rejected, 'provider_probe_mismatch'):
            other.answer(challenge)
        answer = self.gate.answer(challenge)
        self.assertNotEqual(answer['proof'], challenge)
        self.gate.finish(challenge, answer['proof'])
        self.assertTrue(self.gate.snapshot()['route_verified'])
        self.assertFalse(self.gate.snapshot()['automatic_submission'])

    def test_metadata_alone_or_echoing_challenge_cannot_verify(self):
        challenge = self.gate.begin(self.candidate)
        self.assertFalse(self.gate.snapshot()['route_verified'])
        with self.assertRaisesRegex(Rejected, 'provider_probe_mismatch'):
            self.gate.finish(challenge, challenge)
        self.assertEqual(self.gate.snapshot()['state'], 'unavailable')

    def test_probe_and_proof_are_single_use(self):
        challenge = self.gate.begin(self.candidate)
        with self.assertRaisesRegex(Rejected, 'provider_binding_already_started'):
            self.gate.begin(self.candidate)
        proof = self.gate.answer(challenge)['proof']
        with self.assertRaisesRegex(Rejected, 'provider_probe_already_used'):
            self.gate.answer(challenge)
        self.gate.finish(challenge, proof)
        with self.assertRaises(Rejected):
            self.gate.finish(challenge, proof)
        with self.assertRaises(Rejected):
            self.gate.begin(self.candidate)

    def test_old_challenge_does_not_bind_a_new_attempt(self):
        old = self.gate.begin(self.candidate)
        self.gate.fail('provider_control_unavailable')
        new = self.gate.begin(self.candidate)
        self.assertNotEqual(new, old)
        with self.assertRaises(Rejected):
            self.gate.answer(old)
        self.assertFalse(self.gate.snapshot()['route_verified'])

    def test_late_failure_cannot_revoke_another_attempt_or_finished_proof(self):
        old = self.gate.begin(self.candidate)
        self.now += 10
        self.gate.snapshot()
        current = self.gate.begin(self.candidate)
        self.gate.fail_if_current(old, 'provider_control_unavailable')
        self.assertEqual(self.gate.snapshot()['state'], 'checking')
        proof = self.gate.answer(current)['proof']
        self.gate.finish(current, proof)
        self.gate.fail_if_current(old, 'provider_control_unavailable')
        self.gate.fail_if_current(current, 'provider_control_unavailable')
        self.assertTrue(self.gate.snapshot()['route_verified'])

    def test_timeout_is_bounded_in_answer_finish_and_snapshot(self):
        for operation in ('answer', 'finish', 'snapshot'):
            with self.subTest(operation=operation):
                gate = SessionBinding(clock=lambda: self.now)
                challenge = gate.begin(self.candidate)
                proof = gate.answer(challenge)['proof'] if operation == 'finish' else None
                self.now += 10
                if operation == 'snapshot':
                    self.assertEqual(gate.snapshot()['reason'], 'provider_probe_expired')
                else:
                    with self.assertRaisesRegex(Rejected, 'provider_probe_expired'):
                        gate.answer(challenge) if operation == 'answer' else gate.finish(challenge, proof)
                self.assertFalse(gate.snapshot()['route_verified'])

    def test_untrusted_unicode_or_types_fail_without_crashing(self):
        challenge = self.gate.begin(self.candidate)
        for value in (None, {}, 1, 'é' * 43, 'a' * 5000, ''):
            with self.subTest(value=str(value)[:20]), self.assertRaises(Rejected):
                self.gate.answer(value)
        self.gate.answer(challenge)
        with self.assertRaises(Rejected):
            self.gate.finish(challenge, 'é' * 43)

    def test_close_revokes_proof_and_cannot_be_reopened(self):
        challenge = self.gate.begin(self.candidate)
        proof = self.gate.answer(challenge)['proof']
        self.gate.close()
        self.gate.fail('provider_thread_mismatch')
        self.assertEqual(self.gate.snapshot()['state'], 'closed')
        for operation in (lambda: self.gate.begin(self.candidate), lambda: self.gate.answer(challenge),
                          lambda: self.gate.finish(challenge, proof)):
            with self.assertRaises(Rejected):
                operation()

    def test_secrets_identity_and_raw_errors_never_enter_snapshot(self):
        challenge = self.gate.begin(self.candidate)
        proof = self.gate.answer(challenge)['proof']
        raw = json.dumps(self.gate.snapshot())
        for private in (challenge, proof, SESSION, THREAD, str(self.candidate.workspace)):
            self.assertNotIn(private, raw)
        self.gate.fail('provider error with private user text')
        self.assertEqual(self.gate.snapshot()['reason'], 'provider_control_unavailable')


class RuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='clipen-binding-')
        self.root = Path(self.directory.name).resolve()
        shutil.copytree(ROOT / 'skills', self.root / 'skills')
        self.runtime = Runtime(self.root)
        self.runtime.connect({'name': 'synthetic-not-AI', 'version': '1'})
        self.output = io.StringIO()
        self.mcp = MCP(self.runtime, output=self.output, open_browser=False)
        self.mcp.initialized = True
        self.candidate = Candidate(SESSION, THREAD, self.root)
        self.calls = []

    def tearDown(self):
        self.runtime.close()
        for directory, _, _ in os.walk(self.root):
            os.chmod(directory, 0o700)
        self.directory.cleanup()

    def request(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/read':
            self.assertIs(params['includeTurns'], False)
            return thread(self.candidate)
        self.assertEqual(method, 'mcpServer/tool/call')
        self.assertEqual(params['threadId'], THREAD)
        self.assertEqual(params['server'], 'clipen')
        self.assertEqual(params['tool'], 'clipen_session_probe')
        self.mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                         'params': {'name': params['tool'], 'arguments': params['arguments']}})
        return json.loads(self.output.getvalue().splitlines()[-1])['result']

    def test_synthetic_control_roundtrip_proves_same_mcp_without_work(self):
        binding = self.runtime.binding
        verify_mcp_route(self.runtime, self.candidate, self.request)
        snapshot = self.runtime.snapshot()
        self.assertTrue(snapshot['connection']['session_check']['route_verified'])
        self.assertFalse(snapshot['connection']['provider_session_verified'])
        self.assertFalse(snapshot['connection']['session_check']['automatic_submission'])
        self.assertFalse(snapshot['connection']['waiting_for_work'])
        self.assertEqual(snapshot['jobs'], [])
        self.assertEqual(self.runtime.binding, binding)
        self.assertEqual([c[0] for c in self.calls], ['thread/read', 'mcpServer/tool/call', 'thread/read'])

    def test_check_never_claims_or_replays_queued_work(self):
        self.runtime.command('/api/jobs', {'request_id': 'test', 'prompt': 'Synthetic private text',
                                         'consent': True, 'binding_id': self.runtime.binding})
        before = self.runtime.snapshot()['jobs']
        verify_mcp_route(self.runtime, self.candidate, self.request)
        self.assertEqual(self.runtime.snapshot()['jobs'], before)
        self.assertNotIn('Synthetic private text', json.dumps(self.calls))

    def test_wrong_thread_rejected_before_any_tool_call(self):
        request = Mock(return_value=thread(Candidate(OTHER, THREAD, self.root)))
        with self.assertRaisesRegex(Rejected, 'provider_thread_mismatch'):
            verify_mcp_route(self.runtime, self.candidate, request)
        request.assert_called_once_with('thread/read', {'threadId': THREAD, 'includeTurns': False})
        self.assertFalse(self.runtime.snapshot()['connection']['provider_session_verified'])

    def test_thread_is_rechecked_after_tool_approval_delay(self):
        def request(method, params):
            result = self.request(method, params)
            if len(self.calls) == 3:
                result['thread']['status'] = {'type': 'notLoaded'}
            return result
        with self.assertRaisesRegex(Rejected, 'provider_thread_mismatch'):
            verify_mcp_route(self.runtime, self.candidate, request)
        self.assertFalse(self.runtime.snapshot()['connection']['provider_session_verified'])

    def test_control_failure_has_no_fallback_resume_queue_or_model(self):
        request = Mock(side_effect=OSError('secret endpoint details'))
        with self.assertRaisesRegex(Rejected, '^provider_control_unavailable$'):
            verify_mcp_route(self.runtime, self.candidate, request)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn('secret', json.dumps(self.runtime.snapshot()))

    def test_unknown_probe_cannot_register_a_session(self):
        self.mcp.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                         'params': {'name': 'clipen_session_probe', 'arguments': {'challenge': 'a'*43}}})
        response = json.loads(self.output.getvalue().splitlines()[-1])['result']
        self.assertTrue(response['isError'])
        self.assertEqual(self.runtime.snapshot()['connection']['session_check']['state'], 'transport_only')

    def test_initialize_metadata_cannot_verify_or_start_a_check(self):
        self.mcp.handle({'jsonrpc': '2.0', 'id': 8, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'codex', 'version': 'synthetic', 'session_id': SESSION},
            '_meta': {'sessionId': SESSION, 'threadId': THREAD},
        }})
        self.assertEqual(self.mcp.client, {'name': 'codex', 'version': 'synthetic'})
        self.assertFalse(self.runtime.snapshot()['connection']['provider_session_verified'])
        self.assertEqual(self.runtime.snapshot()['connection']['session_check']['state'], 'transport_only')

    def test_rejected_probe_stops_without_retrying_or_reading_more_threads(self):
        def request(method, params):
            if method == 'thread/read':
                return self.request(method, params)
            self.calls.append((method, params))
            return {'isError': True, 'content': [{'type': 'text', 'text': 'permission denied'}]}
        with self.assertRaisesRegex(Rejected, 'provider_probe_rejected'):
            verify_mcp_route(self.runtime, self.candidate, request)
        self.assertEqual([call[0] for call in self.calls], ['thread/read', 'mcpServer/tool/call'])
        self.assertFalse(self.runtime.snapshot()['connection']['session_check']['route_verified'])

    def test_discovery_client_cannot_answer_the_owner_challenge(self):
        challenge = self.runtime.begin_session_check(self.candidate)
        self.mcp.discovery_only = True
        self.mcp.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                         'params': {'name': 'clipen_session_probe', 'arguments': {'challenge': challenge}}})
        response = json.loads(self.output.getvalue().splitlines()[-1])['result']
        self.assertTrue(response['isError'])
        self.assertNotIn('proof', str(response))

    def test_disconnect_during_check_never_reactivates_session(self):
        def request(method, params):
            result = self.request(method, params)
            if len(self.calls) == 3:
                self.runtime.disconnect()
            return result
        with self.assertRaisesRegex(Rejected, 'service_stopped'):
            verify_mcp_route(self.runtime, self.candidate, request)
        self.assertEqual(self.runtime.snapshot()['connection']['session_check']['state'], 'closed')

    def test_snapshot_cache_tracks_proof_state_and_expiry_without_leaking(self):
        now = [100]
        self.runtime.session = SessionBinding(clock=lambda: now[0])
        first = self.runtime.encoded_state('test-csrf')
        challenge = self.runtime.begin_session_check(self.candidate)
        checking = self.runtime.encoded_state('test-csrf')
        self.assertNotEqual(first[0], checking[0])
        self.assertIs(checking, self.runtime.encoded_state('test-csrf'))
        proof = self.runtime.answer_session_probe(challenge)['proof']
        for secret in (proof, challenge, SESSION, THREAD):
            self.assertNotIn(secret.encode(), checking[1])
        now[0] += 10
        expired = self.runtime.encoded_state('test-csrf')
        self.assertNotEqual(checking[0], expired[0])
        self.assertEqual(json.loads(expired[1])['connection']['session_check']['state'], 'unavailable')

    def test_no_binding_or_stopped_runtime_cannot_begin(self):
        self.runtime.binding = None
        with self.assertRaisesRegex(Rejected, 'agent_not_connected'):
            self.runtime.begin_session_check(self.candidate)
        self.runtime.disconnect()
        with self.assertRaisesRegex(Rejected, 'service_stopped'):
            self.runtime.begin_session_check(self.candidate)

    def test_model_arguments_or_http_cannot_supply_identity(self):
        with self.assertRaisesRegex(Rejected, 'not_found'):
            self.runtime.command('/api/provider-session', {'session_id': SESSION, 'thread_id': THREAD})
        self.mcp.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                         'params': {'name': 'clipen_session_probe', 'arguments': {'challenge': 'a'*43, 'session_id': SESSION}}})
        self.assertIn('error', json.loads(self.output.getvalue().splitlines()[-1]))
        self.assertFalse(self.runtime.snapshot()['connection']['provider_session_verified'])


class ResponseTests(unittest.TestCase):
    def test_malformed_tool_results_fail_closed(self):
        for response in (None, [], {}, {'isError': True}, {'content': []},
                         {'content': [{'type': 'image', 'text': '{}'}]},
                         {'content': [{'type': 'text', 'text': 'invalid'}]},
                         {'content': [{'type': 'text', 'text': '{"proof":true}'}]},
                         {'content': [{'type': 'text', 'text': '{"proof":"x","extra":1}'}]},
                         {'content': [{'type': 'text', 'text': 'x'*1025}]}):
            with self.subTest(response=str(response)[:100]), self.assertRaises(Rejected):
                proof_from_result(response)


if __name__ == '__main__':
    unittest.main()
