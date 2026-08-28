"""Durable text-only work queue. No media, model API, or shell execution."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time


class Rejected(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


def text_field(value, maximum=4000):
    try:
        valid = isinstance(value, str) and value.strip() and len(value.encode('utf-8')) <= maximum
    except UnicodeError:
        valid = False
    if not valid:
        raise Rejected('invalid_text', 422)
    return value


def private_dir(path: Path):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Rejected('state_directory_must_be_private', 503)


def checked_file(path: Path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise Rejected('unsafe_state_file', 503)
    return fd


class Store:
    def __init__(self, workspace: Path):
        self.directory = workspace / '.clipen'
        private_dir(self.directory)
        self.directory /= 'prototype'
        private_dir(self.directory)
        self.lock_fd = checked_file(self.directory / 'writer.lock')
        self.db = None
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            raise Rejected('workspace_already_running', 503) from None
        try:
            path = self.directory / 'state.sqlite'
            os.close(checked_file(path))
            # SQLite opens its own sidecars. Reject hostile pre-existing sidecars too.
            for suffix in ('-journal', '-wal', '-shm'):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    os.close(checked_file(sidecar))
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            # Deliberately avoid WAL in this small prototype; no WAL version assumption.
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise Rejected('unsupported_state_version', 503)
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, prompt TEXT NOT NULL, status TEXT NOT NULL,
                    version INTEGER NOT NULL, attempt TEXT, binding TEXT, result TEXT,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    kind TEXT NOT NULL, created REAL NOT NULL);
                PRAGMA user_version=1;
            ''')
            self.mutex = threading.RLock()
            with self.transaction():
                for row in self.db.execute("SELECT id FROM jobs WHERE status IN ('queued','delivered')").fetchall():
                    self.db.execute("UPDATE jobs SET status='uncertain',version=version+1 WHERE id=?", (row['id'],))
                    self.event(row['id'], 'recovery_required')
        except Exception:
            self.close()
            raise

    @contextmanager
    def transaction(self):
        with self.mutex:
            try:
                self.db.execute('BEGIN IMMEDIATE')
                yield
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def event(self, job, kind):
        self.db.execute('INSERT INTO events(job_id,kind,created) VALUES(?,?,?)', (job, kind, time.time()))

    def command(self, body, action, operation):
        request = text_field(body.get('request_id'), 128)
        try:
            payload = json.dumps([action, body], sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        except (ValueError, TypeError, UnicodeError):
            raise Rejected('invalid_command', 400) from None
        fingerprint = hashlib.sha256(payload).hexdigest()
        with self.transaction():
            saved = self.db.execute('SELECT * FROM commands WHERE id=?', (request,)).fetchone()
            if saved:
                if saved['fingerprint'] != fingerprint:
                    raise Rejected('request_id_conflict')
                return json.loads(saved['receipt'])
            receipt = operation()
            self.db.execute('INSERT INTO commands VALUES(?,?,?)', (request, fingerprint, json.dumps(receipt)))
            return receipt

    def create(self, body, binding):
        def operation():
            if not binding:
                raise Rejected('agent_not_connected', 503)
            prompt = text_field(body.get('prompt'))
            if body.get('consent') is not True or body.get('binding_id') != binding:
                raise Rejected('explicit_current_agent_consent_required', 403)
            if self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] >= 100:
                raise Rejected('prototype_job_limit', 422)
            job = 'job_' + secrets.token_hex(12)
            self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',
                            (job, prompt, 'queued', 1, None, binding, None, time.time()))
            self.event(job, 'queued')
            return {'job_id': job, 'job_version': 1, 'status': 'queued'}
        return self.command(body, 'create', operation)

    def change(self, job, action, body, binding):
        def operation():
            row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
            if not row:
                raise Rejected('job_not_found', 404)
            expected = body.get('expected_job_version')
            if type(expected) is not int:
                raise Rejected('expected_job_version_required', 400)
            if expected != row['version']:
                raise Rejected('version_conflict')
            if action == 'cancel':
                if row['status'] not in ('queued', 'delivered', 'uncertain'):
                    raise Rejected('cannot_cancel')
                status = 'cancelled'
            elif action == 'retry':
                if row['status'] != 'uncertain':
                    raise Rejected('cannot_retry')
                if not binding or body.get('binding_id') != binding or body.get('consent') is not True:
                    raise Rejected('explicit_current_agent_consent_required', 403)
                status = 'queued'
            else:
                raise Rejected('unknown_action', 404)
            self.db.execute('UPDATE jobs SET status=?,version=version+1,attempt=NULL,binding=? WHERE id=?',
                            (status, binding, job))
            self.event(job, status)
            return {'job_id': job, 'job_version': row['version'] + 1, 'status': status}
        return self.command(body, [job, action], operation)

    def claim(self, binding):
        with self.transaction():
            # At most one in-flight operation. A repeated poll never redispatches it.
            if self.db.execute("SELECT 1 FROM jobs WHERE status='delivered'").fetchone():
                return None
            row = self.db.execute("SELECT * FROM jobs WHERE status='queued' AND binding=? ORDER BY created LIMIT 1",
                                  (binding,)).fetchone()
            if not row:
                return None
            attempt = 'attempt_' + secrets.token_hex(12)
            self.db.execute("UPDATE jobs SET status='delivered',version=version+1,attempt=? WHERE id=?",
                            (attempt, row['id']))
            self.event(row['id'], 'delivered')
            return {'job_id': row['id'], 'attempt_id': attempt, 'binding_id': binding,
                    'user_text': row['prompt'], 'instruction': 'Reply to this text only; no files, shell, or other tools.'}

    def complete(self, body, binding):
        result = text_field(body.get('result'), 16000)
        job, attempt = text_field(body.get('job_id'), 128), text_field(body.get('attempt_id'), 128)
        with self.transaction():
            row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
            if not row or row['binding'] != binding or row['attempt'] != attempt:
                raise Rejected('stale_attempt')
            if row['status'] == 'completed' and row['result'] == result:
                return {'status': 'completed', 'job_id': job}
            if row['status'] != 'delivered':
                raise Rejected('result_not_accepted')
            self.db.execute("UPDATE jobs SET status='completed',result=?,version=version+1 WHERE id=?", (result, job))
            self.event(job, 'completed')
            return {'status': 'completed', 'job_id': job}

    def disconnect(self, binding):
        with self.transaction():
            rows = self.db.execute("SELECT id FROM jobs WHERE binding=? AND status IN ('queued','delivered')",
                                   (binding,)).fetchall()
            for row in rows:
                self.db.execute("UPDATE jobs SET status='uncertain',version=version+1 WHERE id=?", (row['id'],))
                self.event(row['id'], 'connection_lost')

    def snapshot(self):
        with self.mutex:
            return {'jobs': [dict(r) for r in self.db.execute('SELECT * FROM jobs ORDER BY created DESC')],
                    'events': [dict(r) for r in self.db.execute('SELECT * FROM events ORDER BY seq DESC LIMIT 100')]}

    @property
    def revision(self):
        # This connection is the sole writer. Rollbacks may invalidate a cache
        # unnecessarily, but can never make committed changes invisible.
        with self.mutex:
            return self.db.total_changes

    def close(self):
        if self.db:
            self.db.close()
            self.db = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
