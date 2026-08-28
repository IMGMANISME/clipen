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
        self.binding = None
        self.client = None
        self.polling = False
        self.last_poll = None
        self.stop = threading.Event()
        self.session = SessionBinding()

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
        self.disconnect()
        self.store.close()
        self._encoded_state = None
        self._state_key = None
