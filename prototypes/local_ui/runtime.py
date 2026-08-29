"""Application boundary shared by HTTP (user) and stdio MCP (agent)."""
from pathlib import Path
import hashlib
import json
import secrets
import threading
import time

from tools.skill_guard import SkillStore
from .store import Rejected, Store
from .session_binding import SessionBinding
from .media_import import DirectoryPicker, ScanCancelled, scan_materials


class Runtime:
    def __init__(self, workspace: Path):
        self.skills = SkillStore(workspace)
        # Verified instructions stay inside the runtime. The browser receives
        # only verification status, never the bundle or its instruction text.
        self._system_skills = self.skills.verify_system()
        self.store = Store(workspace)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.queue_revision = 0
        self._state_key = None
        self._encoded_state = None
        self._preparation_key = None
        self._encoded_preparation = None
        self.binding = None
        self.client = None
        self.polling = False
        self.last_poll = None
        self.stop = threading.Event()
        self.session = SessionBinding()
        self.directory_picker = DirectoryPicker()
        self._import_cancel = threading.Event()
        self._import_thread = None
        self._import_worker_id = None
        self._closing = False

    def connect(self, client):
        with self.lock:
            if self.binding:
                raise Rejected('already_connected')
            self.binding = 'connection_' + secrets.token_hex(12)
            self.client = client

    def snapshot(self):
        with self.lock:
            session = self.session.snapshot()
            return {**self.store.snapshot(), 'connection': {
                'binding_id': self.binding, 'client': self.client,
                'transport_connected': self.binding is not None,
                'waiting_for_work': self.polling, 'last_poll': self.last_poll,
                'provider_session_verified': session['native_identity_verified'],
                'session_check': session,
            }, 'system_policy': {'verified': True}, 'plan': None, 'prototype': True,
                'capabilities': {'text_roundtrip': True, 'media': False, 'editing': False,
                                 'resolve': False, 'automatic_reconnect': False,
                                 'plan_generation': False, 'plan_editing': False}}

    def command(self, path, body):
        with self.lock:
            if self.stop.is_set():
                raise Rejected('service_stopped', 503)
            if path == '/api/preparation':
                # Local-only: does not enqueue work, grant file access or wake an AI.
                return self.store.save_preparation(body)
            if path == '/api/directory-picker':
                if set(body) != {'role'}:
                    raise Rejected('invalid_directory_picker_fields', 422)
                return self.directory_picker.start(body['role'])
            if path == '/api/material-import':
                receipt = self.store.start_material_import(body)
                state = self.store.material_import()
                # A retried HTTP command returns its durable receipt. Never turn
                # that replay into a second filesystem scan.
                active = self._import_thread and self._import_thread.is_alive()
                if (state['import_id'] == receipt['import_id'] and state['status'] == 'running'
                        and not (active and self._import_worker_id == receipt['import_id'])):
                    self._import_cancel = threading.Event()
                    self._import_worker_id = receipt['import_id']
                    self._import_thread = threading.Thread(
                        target=self._scan_materials, args=(receipt['import_id'],), daemon=False)
                    self._import_thread.start()
                return receipt
            if path == '/api/material-import/cancel':
                receipt = self.store.cancel_material_import(body)
                self._import_cancel.set()
                return receipt
            if path == '/api/jobs':
                result = self.store.create(body, self.binding)
            else:
                parts = path.split('/')
                if len(parts) != 5 or parts[1:3] != ['api', 'jobs']:
                    raise Rejected('not_found', 404)
                result = self.store.change(parts[3], parts[4], body, self.binding)
            self.queue_revision += 1
            self.changed.notify_all()
            return result

    def encoded_state(self, csrf):
        with self.lock:
            session = self.session.snapshot()
            key = (self.store.revision, self.binding, self.polling, self.last_poll, csrf,
                   session['state'], session['reason'])
            if key != self._state_key:
                raw = json.dumps({**self.snapshot(), 'csrf': csrf}, ensure_ascii=False).encode('utf-8')
                self._encoded_state = ('"' + hashlib.sha256(raw).hexdigest() + '"', raw)
                self._state_key = key
            return self._encoded_state

    def encoded_preparation(self, csrf):
        """Compact browser-only state; never return the draft through MCP."""
        with self.lock:
            picker = self.directory_picker.snapshot()
            key = (self.store.revision, csrf, picker['id'], picker['status'], picker['path'])
            if key != self._preparation_key:
                state = {'draft': self.store.preparation(), 'material_import': self.store.material_import(),
                         'directory_picker': picker, 'csrf': csrf,
                         'capabilities': {'draft_storage': True,
                                          'directory_picker': self.directory_picker.available,
                                          'media_inventory': True, 'media': False, 'plan_generation': False,
                                          'editing': False, 'resolve': False}}
                raw = json.dumps(state, ensure_ascii=False).encode('utf-8')
                self._encoded_preparation = ('"' + hashlib.sha256(raw).hexdigest() + '"', raw)
                self._preparation_key = key
            return self._encoded_preparation

    def _scan_materials(self, import_id):
        state = self.store.material_import()
        if state['import_id'] != import_id:
            return
        roots, last_saved = state['roots'], 0

        def progress(entries):
            nonlocal last_saved
            if len(entries) - last_saved >= 5:
                self.store.update_material_import(import_id, list(entries))
                last_saved = len(entries)

        try:
            entries = scan_materials(roots['source'], roots['reference'], self._import_cancel, progress)
            status = ('interrupted' if self._closing else 'cancelled') if self._import_cancel.is_set() else 'completed'
            self.store.update_material_import(import_id, entries, status,
                                              'service_interrupted' if status == 'interrupted' else None)
        except ScanCancelled:
            current = self.store.material_import()
            self.store.update_material_import(import_id, current['entries'],
                                              'interrupted' if self._closing else 'cancelled',
                                              'service_interrupted' if self._closing else None)
        except Rejected as error:
            current = self.store.material_import()
            self.store.update_material_import(import_id, current['entries'], 'failed', error.code)
        except Exception:
            current = self.store.material_import()
            self.store.update_material_import(import_id, current['entries'], 'failed', 'material_scan_failed')

    def begin_session_check(self, candidate):
        with self.lock:
            if self.stop.is_set():
                raise Rejected('service_stopped', 503)
            if not self.binding:
                raise Rejected('agent_not_connected', 503)
            return self.session.begin(candidate)

    def answer_session_probe(self, challenge):
        with self.lock:
            if self.stop.is_set():
                raise Rejected('service_stopped', 503)
            return self.session.answer(challenge)

    def finish_session_check(self, challenge, proof):
        with self.lock:
            if self.stop.is_set():
                raise Rejected('service_stopped', 503)
            self.session.finish(challenge, proof)

    def fail_session_check(self, challenge, reason):
        with self.lock:
            self.session.fail_if_current(challenge, reason)

    def next_work(self, cancel, wait_seconds=20):
        with self.lock:
            if not self.binding:
                raise Rejected('agent_not_connected', 503)
            if self.polling:
                raise Rejected('poll_already_running')
            self.polling = True
            self.last_poll = time.time()
        deadline = time.monotonic() + wait_seconds
        observed = -1
        try:
            with self.changed:
                while not self.stop.is_set() and not cancel.is_set():
                    if observed != self.queue_revision:
                        work = self.store.claim(self.binding)
                        observed = self.queue_revision
                        if work:
                            return {'status': 'work', **work}
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return {'status': 'idle', 'instruction': 'No work; do not invent a result or poll indefinitely.'}
                    # Wake on mutations; a bounded timer only observes external
                    # cancellation/signals, without repeatedly querying SQLite.
                    self.changed.wait(min(0.25, remaining))
            return {'status': 'stopped'}
        finally:
            with self.lock:
                self.polling = False
                self.last_poll = time.time()

    def complete(self, body):
        with self.lock:
            if self.stop.is_set():
                raise Rejected('service_stopped', 503)
            result = self.store.complete(body, self.binding)
            self.queue_revision += 1
            self.changed.notify_all()
            return result

    def disconnect(self):
        self.stop.set()
        with self.lock:
            self.session.close()
            if self.binding:
                self.store.disconnect(self.binding)
            self.binding = None
            self.client = None
            self.changed.notify_all()

    def close(self):
        self._closing = True
        self.disconnect()
        self._import_cancel.set()
        self.directory_picker.close()
        if self._import_thread and self._import_thread.ident is not None:
            self._import_thread.join(timeout=20)
        self.store.close()
        self._encoded_state = None
        self._state_key = None
