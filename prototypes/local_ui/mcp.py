"""Small stdio MCP subset for the prototype. One original client per process."""
import json
import os
import sys
import threading

from .http_ui import Server, decode_json
from .lifecycle import announce_in_terminal, input_lines
from .store import Rejected

PROTOCOLS = {'2024-11-05', '2025-03-26', '2025-06-18'}
INSTRUCTIONS = ('Clipen is a text-only connection prototype, not a video editor. '
                'Only process work explicitly authorized in its UI. Call clipen_next_work, '
                'answer user_text yourself, then submit the exact job/attempt with clipen_complete_work. '
                'Do not use files, shell, other models, or invent work. Stop on permission denial or errors. '
                'An idle result is not work; do not poll indefinitely. Initialization does not start a model turn. '
                'A discovery-only connection cannot accept work; never bypass the workspace owner.')
TOOLS = [
    {'name': 'clipen_session_probe', 'description': 'Internal connection verification only. Do not call from a model turn or invent a challenge. Does not authorize work or start a model.',
     'inputSchema': {'type': 'object', 'properties': {'challenge': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{43}$'}},
                     'required': ['challenge'], 'additionalProperties': False}},
    {'name': 'clipen_next_work', 'description': 'Wait up to 20 seconds for a user-authorized text task. Never invent work. Only one active task.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'clipen_complete_work', 'description': 'Return your text answer for the exact job and attempt delivered by clipen_next_work. No shell or file operations.',
     'inputSchema': {'type': 'object', 'properties': {key: {'type': 'string'} for key in ('job_id', 'attempt_id', 'result')},
                     'required': ['job_id', 'attempt_id', 'result'], 'additionalProperties': False}},
    {'name': 'clipen_connection_status', 'description': 'Read connection status, without user text. Transport identity is not verified provider session identity.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
]


class MCP:
    def __init__(self, runtime=None, source=None, output=None, open_browser=True, opener=None,
                 notifier=announce_in_terminal, watch_parent=False, runtime_factory=None):
        self.runtime = runtime
        self.runtime_factory = runtime_factory
        self.stop = runtime.stop if runtime else threading.Event()
        self.discovery_only = False
        self.source = source if source is not None else sys.stdin.buffer
        self.output = output if output is not None else sys.stdout
        self.open_browser, self.opener = open_browser, opener
        self.notifier = notifier
        self.parent_pid = os.getppid() if watch_parent else None
        self.ui = None
        self.initialized = False
        self.client = None
        self.write_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending = {}

    def activate(self):
        if self.runtime is None:
            try:
                self.runtime = self.runtime_factory()
            except Rejected as error:
                if error.code != 'workspace_already_running':
                    raise
                # Hosts may spawn an extra client just to inspect tool inventory.
                # It has no DB, UI, owner identity, credentials, or execution rights.
                self.discovery_only = True
                self.initialized = True
                print('Clipen discovery only: another process owns this workspace. '
                      'Tool inventory is available; work execution is not.', file=sys.stderr)
                return
            self.runtime.stop = self.stop
        self.runtime.connect(self.client)
        self.ui = Server(self.runtime)
        self.initialized = True
        opened = self.ui.start(self.open_browser, self.opener)
        announced = self.notifier(self.ui.reopen_url()) if self.notifier else False
        print('Clipen local UI: ' + self.ui.origin, file=sys.stderr, flush=True)
        if not announced:
            print('No controlling terminal for the private reopen link. Use the opened browser tab.', file=sys.stderr)
        if not opened:
            print('Browser did not open. Use the complete link printed in your terminal.', file=sys.stderr)

    def send(self, request_id, result=None, error=None):
        message = {'jsonrpc': '2.0', 'id': request_id}
        message['error' if error else 'result'] = error if error else result
        with self.write_lock:
            self.output.write(json.dumps(message, ensure_ascii=False) + '\n')
            self.output.flush()

    def tool_result(self, request_id, result, is_error=False):
        self.send(request_id, {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}], 'isError': is_error})

    def wait_work(self, request_id, cancel):
        try:
            result = self.runtime.next_work(cancel)
            if cancel.is_set():
                # Cancellation can race with a durable delivery. Never requeue automatically.
                with self.runtime.lock:
                    self.runtime.store.disconnect(self.runtime.binding)
                return
            self.tool_result(request_id, result)
        except Rejected as error:
            self.tool_result(request_id, {'error': error.code}, True)
        except (BrokenPipeError, OSError):
            self.stop.set()
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def handle(self, message):
        rid = message.get('id')
        notification = 'id' not in message
        method, params = message.get('method'), message.get('params', {})
        if message.get('jsonrpc') != '2.0' or not isinstance(method, str) or not isinstance(params, dict) or (not notification and (type(rid) not in (str, int))):
            self.send(None, error={'code': -32600, 'message': 'Invalid request'})
            return
        if notification:
            if method == 'notifications/initialized' and self.client and not self.initialized:
                self.activate()
            elif method == 'notifications/cancelled':
                with self.pending_lock:
                    entry = self.pending.get(params.get('requestId')) if type(params.get('requestId')) in (str, int) else None
                    if entry:
                        entry[0].set()
            return
        if method == 'initialize' and not self.client:
            client = params.get('clientInfo')
            if not isinstance(client, dict) or not all(isinstance(client.get(k), str) and 0 < len(client[k]) <= 128 for k in ('name', 'version')):
                self.send(rid, error={'code': -32602, 'message': 'clientInfo required'})
                return
            self.client = {'name': client['name'], 'version': client['version']}
            requested = params.get('protocolVersion')
            self.send(rid, {'protocolVersion': requested if isinstance(requested, str) and requested in PROTOCOLS else '2025-06-18',
                            'capabilities': {'tools': {}}, 'serverInfo': {'name': 'clipen-prototype', 'version': '0.2.0'},
                            'instructions': INSTRUCTIONS})
        elif method == 'ping':
            self.send(rid, {})
        elif not self.initialized:
            self.send(rid, error={'code': -32002, 'message': 'Not initialized'})
        elif method == 'tools/list':
            self.send(rid, {'tools': TOOLS})
        elif method == 'tools/call':
            name, args = params.get('name'), params.get('arguments', {})
            if not isinstance(args, dict):
                self.send(rid, error={'code': -32602, 'message': 'Invalid arguments'})
                return
            try:
                if self.discovery_only:
                    if name == 'clipen_connection_status' and not args:
                        self.tool_result(rid, {'transport_connected': False, 'waiting_for_work': False,
                                              'provider_session_verified': False, 'mode': 'discovery_only',
                                              'error': 'workspace_already_running'})
                    else:
                        self.tool_result(rid, {'error': 'workspace_already_running', 'mode': 'discovery_only'}, True)
                    return
                if name == 'clipen_next_work' and not args:
                    with self.pending_lock:
                        if self.pending:
                            raise Rejected('poll_already_running')
                        cancel = threading.Event()
                        thread = threading.Thread(target=self.wait_work, args=(rid, cancel), daemon=True)
                        self.pending[rid] = (cancel, thread)
                        thread.start()
                elif name == 'clipen_complete_work' and set(args) == {'job_id', 'attempt_id', 'result'}:
                    self.tool_result(rid, self.runtime.complete(args))
                elif name == 'clipen_session_probe' and set(args) == {'challenge'}:
                    self.tool_result(rid, self.runtime.answer_session_probe(args['challenge']))
                elif name == 'clipen_connection_status' and not args:
                    self.tool_result(rid, self.runtime.snapshot()['connection'])
                else:
                    self.send(rid, error={'code': -32602, 'message': 'Unknown tool or invalid arguments'})
            except Rejected as error:
                self.tool_result(rid, {'error': error.code}, True)
        else:
            self.send(rid, error={'code': -32601, 'message': 'Method not found'})

    def run(self):
        try:
            for line in input_lines(self.source, self.stop, self.parent_pid):
                if len(line) > 65536:
                    self.send(None, error={'code': -32700, 'message': 'Message too large'})
                    break
                try:
                    message = decode_json(line)
                except (ValueError, UnicodeError, RecursionError):
                    self.send(None, error={'code': -32700, 'message': 'Invalid JSON'})
                    continue
                self.handle(message)
        finally:
            self.stop.set()
            with self.pending_lock:
                entries = list(self.pending.values())
                for cancel, _ in entries:
                    cancel.set()
            for _, thread in entries:
                thread.join(timeout=5)
            if self.ui:
                self.ui.close()
            if self.runtime:
                self.runtime.close()
