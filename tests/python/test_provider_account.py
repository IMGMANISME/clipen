"""The status bar names the running model and remaining quota without calling a model."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clipen.providers.account import claude_account, codex_account, record_claude_account


class ProviderAccountTests(unittest.TestCase):
    def test_codex_reports_the_default_model_and_live_limits(self):
        answers = {1: {'rateLimits': {'primary': {'usedPercent': 17, 'windowDurationMins': 300, 'resetsAt': 1792856503},
                                      'secondary': {'usedPercent': 7, 'windowDurationMins': 10080, 'resetsAt': 1793422936},
                                      'planType': 'plus', 'rateLimitReachedType': None}},
                   2: {'data': [{'model': 'gpt-6.1-sol', 'displayName': 'GPT-6.1-Sol', 'isDefault': False},
                                {'model': 'gpt-5.6-sol', 'displayName': 'GPT-5.6-Sol', 'defaultReasoningEffort': 'low', 'isDefault': True}]}}
        with patch('clipen.providers.account._rpc', return_value=answers) as rpc:
            account = codex_account('/bin/codex', {})
        self.assertEqual([method for method, _ in rpc.call_args.args[2]], ['account/rateLimits/read', 'model/list'])
        self.assertEqual(account['model'], {'id': 'gpt-5.6-sol', 'name': 'GPT-5.6-Sol', 'effort': 'low'})
        self.assertEqual(account['quota']['plan'], 'plus')
        self.assertEqual([(w['kind'], w['used_percent']) for w in account['quota']['windows']], [('five_hour', 17), ('weekly', 7)])
        self.assertFalse(account['quota']['limited'])

    def test_codex_without_answers_shows_nothing_rather_than_guessing(self):
        with patch('clipen.providers.account._rpc', return_value={}):
            self.assertEqual(codex_account('/bin/codex', {}), {'model': None, 'quota': None})

    def test_claude_keeps_what_the_last_attempt_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(claude_account(root), {'model': None, 'quota': None})
            record_claude_account(root, [
                {'type': 'system', 'subtype': 'init', 'model': 'claude-sonnet-5-20261001'},
                {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'resetsAt': 1792767600, 'rateLimitType': 'five_hour'}},
                {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected', 'resetsAt': 1792771200, 'rateLimitType': 'five_hour'}},
            ])
            account = claude_account(root)
            self.assertEqual((account['model']['id'], account['model']['name']), ('claude-sonnet-5-20261001', 'Claude Sonnet 5'))
            self.assertEqual(account['quota']['source'], 'last_run')
            self.assertTrue(account['quota']['limited'])
            self.assertEqual(account['quota']['resets_at'], 1792771200)
            self.assertEqual(account['quota']['windows'], [])
            (root / 'claude-account.json').write_text('{not json')
            self.assertEqual(claude_account(root), {'model': None, 'quota': None})


if __name__ == '__main__':
    unittest.main()
