"""Deterministic work/serialization bounds; timings are not CI pass criteria."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from prototypes.local_ui.__main__ import ROOT
from prototypes.local_ui.runtime import Runtime
from prototypes.local_ui.store import Rejected


class RuntimeEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='clipen-performance-')
        self.root = Path(self.directory.name)
        shutil.copytree(ROOT / 'skills', self.root / 'skills')
        self.runtime = Runtime(self.root)
        self.runtime.connect({'name': 'synthetic', 'version': '1'})

    def tearDown(self):
        self.runtime.close()
        for root, _, _ in os.walk(self.root):
            os.chmod(root, 0o700)
        self.directory.cleanup()

    def create(self, request='one', prompt='Synthetic'):
        return self.runtime.command('/api/jobs', {'request_id': request, 'prompt': prompt,
            'consent': True, 'binding_id': self.runtime.binding})

    def test_idle_wait_queries_queue_once_and_is_cancellable(self):
        cancel = threading.Event()
        results = []
        with patch.object(self.runtime.store, 'claim', wraps=self.runtime.store.claim) as claim:
            worker = threading.Thread(target=lambda: results.append(self.runtime.next_work(cancel, 5)))
            worker.start()
            try:
                time.sleep(0.8)
                self.assertEqual(claim.call_count, 1)
            finally:
                cancel.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [{'status': 'stopped'}])

    def test_command_wakes_waiter_without_waiting_for_poll_timeout(self):
        started = threading.Event()
        original = self.runtime.store.claim
        def claim(binding):
            result = original(binding)
            started.set()
            return result
        results = []
        with patch.object(self.runtime.store, 'claim', side_effect=claim):
            worker = threading.Thread(target=lambda: results.append(self.runtime.next_work(threading.Event(), 10)))
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                job = self.create()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results[0]['job_id'], job['job_id'])
            finally:
                self.runtime.stop.set()
                worker.join(2)

    def test_full_workspace_serializes_once_until_a_mutation(self):
        for i in range(100):
            self.create(str(i), '中' * 1000)
            work = self.runtime.next_work(threading.Event(), 0)
            self.runtime.complete({'job_id': work['job_id'], 'attempt_id': work['attempt_id'], 'result': 'x' * 16000})
        with patch.object(self.runtime.store, 'snapshot', wraps=self.runtime.store.snapshot) as read:
            state = self.runtime.encoded_state('test-csrf')
            for _ in range(100):
                self.assertIs(self.runtime.encoded_state('test-csrf'), state)
            self.assertEqual(read.call_count, 1)
        self.assertLess(len(state[1]), 2_100_000)
        self.assertEqual(len(json.loads(state[1])['jobs']), 100)
        with self.assertRaisesRegex(Rejected, 'prototype_job_limit'):
            self.create('overflow')

    def test_stopped_runtime_rejects_mutations(self):
        self.runtime.stop.set()
        with self.assertRaisesRegex(Rejected, 'service_stopped'):
            self.create()
        with self.assertRaisesRegex(Rejected, 'service_stopped'):
            self.runtime.complete({})


if __name__ == '__main__':
    unittest.main()
