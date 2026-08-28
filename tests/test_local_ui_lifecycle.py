"""Synthetic process/PTY tests. No AI, credentials, browser, or user terminals."""
import http.client
import io
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from prototypes.local_ui.__main__ import ROOT
from prototypes.local_ui.lifecycle import announce_in_terminal, input_lines
from prototypes.local_ui.store import Store


MESSAGES = b''.join(json.dumps(message).encode() + b'\n' for message in [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-06-18', 'clientInfo': {'name': 'synthetic-lifetime-test', 'version': '1'}}},
    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
])


class InputTests(unittest.TestCase):
    def test_in_memory_and_split_pipe_lines(self):
        stop = threading.Event()
        self.assertEqual(list(input_lines(io.BytesIO(b'one\ntwo'), stop)), [b'one\n', b'two'])
        read, write = os.pipe()
        with os.fdopen(read, 'rb', buffering=0) as source:
            os.write(write, b'one\ntwo')
            os.close(write)
            self.assertEqual(list(input_lines(source, stop)), [b'one\n', b'two'])

    def test_oversize_partial_message_is_bounded(self):
        read, write = os.pipe()
        source = os.fdopen(read, 'rb', buffering=0)
        def writer():
            try:
                for _ in range(17):
                    os.write(write, b'x' * 4096)
            finally:
                os.close(write)
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            lines = list(input_lines(source, threading.Event()))
            self.assertEqual(len(lines), 1)
            self.assertGreater(len(lines[0]), 65536)
            self.assertLessEqual(len(lines[0]), 65536 + 4096)
        finally:
            source.close()
            thread.join(2)

    def test_no_terminal_does_not_print_credentials_to_streams(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('os.open', side_effect=OSError), patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            self.assertFalse(announce_in_terminal('http://127.0.0.1:1234/#private'))
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), '')


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='clipen-lifecycle-')
        self.root = Path(self.directory.name).resolve()
        shutil.copytree(ROOT / 'skills', self.root / 'skills')
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for root, _, _ in os.walk(self.root):
            os.chmod(root, 0o700)
        self.directory.cleanup()

    def command(self):
        return [sys.executable, str(ROOT / 'integrations/stdio_server.py'),
                '--workspace', str(self.root), '--no-open']

    def start(self, command=None, **kwargs):
        process = subprocess.Popen(command or self.command(), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                   start_new_session=True, **kwargs)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
        self.addCleanup(cleanup)
        process.stdin.write(MESSAGES)
        for rid in (1, 2):
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'MCP startup timeout')
            self.assertEqual(json.loads(process.stdout.readline())['id'], rid)
        line = process.stderr.readline().decode()
        match = re.search(r'http://127\.0\.0\.1:(\d+)', line)
        self.assertIsNotNone(match, line)
        self.port = int(match[1])
        self.assertTrue(self.health())
        return process

    def health(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=0.3)
        try:
            connection.request('GET', '/health')
            response = connection.getresponse()
            response.read()
            return response.status == 200
        except OSError:
            return False
        finally:
            connection.close()

    def assert_stopped(self):
        deadline = time.monotonic() + 5
        while self.health() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.health(), 'HTTP server survived its owner')
        store = Store(self.root)
        store.close()  # Writer lock released; not merely refusing health requests.

    def test_eof_stops_ui_and_releases_workspace(self):
        process = self.start()
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0)
        self.assert_stopped()

    def test_signals_stop_ui_even_while_stdin_is_open(self):
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                process = self.start()
                os.kill(process.pid, signum)
                self.assertEqual(process.wait(timeout=5), 0)
                self.assert_stopped()

    def test_parent_killed_while_another_process_keeps_stdin_open(self):
        # The test retains the writing end of stdin. EOF alone cannot detect
        # this simulated AI crash: the child must notice its parent is gone.
        harness = 'import subprocess,sys,time; p=subprocess.Popen(sys.argv[1:]); p.wait()'
        process = self.start([sys.executable, '-c', harness, *self.command()])
        process.kill()
        process.wait(timeout=5)
        self.assert_stopped()

    def test_sigkill_recovery_preserves_work_as_uncertain(self):
        store = Store(self.root)
        store.create({'request_id': 'before-crash', 'prompt': 'Synthetic only',
                      'consent': True, 'binding_id': 'test'}, 'test')
        store.close()
        process = self.start()
        process.kill()
        process.wait(timeout=5)
        self.assert_stopped()
        store = Store(self.root)
        try:
            job = store.snapshot()['jobs'][0]
            self.assertEqual(job['status'], 'uncertain')
            self.assertEqual(job['prompt'], 'Synthetic only')
        finally:
            store.close()

    def test_controlling_terminal_link_is_separate_from_stdio(self):
        # A throwaway PTY owned by this test; never writes into a user terminal.
        pid, master = pty.fork()
        if pid == 0:
            code = ('import subprocess,sys; p=subprocess.Popen(sys.argv[1:],stdin=subprocess.PIPE,'
                    'stdout=subprocess.PIPE,stderr=subprocess.PIPE); '
                    f'out,err=p.communicate({MESSAGES!r}); '
                    'sys.stdout.buffer.write(b"\\nCAPTURED_STDIO\\n"+out+err)')
            os.execv(sys.executable, [sys.executable, '-c', code, *self.command()])
        data = bytearray()
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 0.2)[0]:
                    continue
                try:
                    part = os.read(master, 65536)
                except OSError:
                    break
                if not part:
                    break
                data.extend(part)
            terminal, captured = bytes(data).split(b'CAPTURED_STDIO', 1)
            token = re.search(rb'http://127\.0\.0\.1:\d+/#([A-Za-z0-9_-]+)', terminal)
            self.assertIsNotNone(token)
            self.assertNotIn(token[1], captured)
            self.assertIn(b'"tools"', captured)
        finally:
            os.close(master)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)

    def test_terminal_hangup_stops_ui_even_if_parent_ignores_hup(self):
        pid, master = pty.fork()
        if pid == 0:
            code = ('import os,signal,subprocess,sys; signal.signal(signal.SIGHUP,signal.SIG_IGN); '
                    'p=subprocess.Popen(sys.argv[1:],stdin=subprocess.PIPE,stdout=subprocess.PIPE,'
                    'stderr=subprocess.PIPE,preexec_fn=lambda:os.setpgid(0,0)); '
                    f'p.stdin.write({MESSAGES!r}); p.stdin.flush(); '
                    'p.stdout.readline(); p.stdout.readline(); '
                    'print("LIFECYCLE_READY",flush=True); p.wait()')
            os.execv(sys.executable, [sys.executable, '-c', code, *self.command()])
        data = bytearray()
        try:
            deadline = time.monotonic() + 5
            while b'LIFECYCLE_READY' not in data and time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    data.extend(os.read(master, 65536))
            self.assertIn(b'LIFECYCLE_READY', data)
            match = re.search(rb'http://127\.0\.0\.1:(\d+)/#', data)
            self.assertIsNotNone(match)
            self.port = int(match[1])
            self.assertTrue(self.health())
            os.close(master)
            master = None
            self.assert_stopped()
        finally:
            if master is not None:
                os.close(master)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)


if __name__ == '__main__':
    unittest.main()
