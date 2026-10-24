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
            account = codex_account('/bin/codex', {'HOME': '/nonexistent'})
        self.assertEqual([method for method, _ in rpc.call_args.args[2]], ['account/rateLimits/read', 'model/list'])
        self.assertEqual(account['model'], {'id': 'gpt-5.6-sol', 'name': 'GPT-5.6-Sol', 'effort': 'low', 'source': 'default', 'unavailable': None})
        self.assertEqual(account['quota']['plan'], 'plus')
        self.assertEqual([(w['kind'], w['used_percent']) for w in account['quota']['windows']], [('five_hour', 17), ('weekly', 7)])
        self.assertFalse(account['quota']['limited'])

    def test_codex_without_answers_shows_nothing_rather_than_guessing(self):
        with patch('clipen.providers.account._rpc', return_value={}):
            self.assertEqual(codex_account('/bin/codex', {'HOME': '/nonexistent'}), {'model': None, 'quota': None})

    def test_codex_preferences_read_only_the_model_and_effort_honouring_the_profile(self):
        from clipen.providers.codex import codex_preferences
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / 'config.toml'
            self.assertEqual(codex_preferences({'CODEX_HOME': temporary}), (None, None))
            config.write_text('profile = "fast"\nmodel = "gpt-a"\nmodel_reasoning_effort = "high" # note\n'
                              '[profiles.fast]\nmodel = \'gpt-b\'\n[projects."/x"]\nmodel = "nope"\n')
            self.assertEqual(codex_preferences({'CODEX_HOME': temporary}), ('gpt-b', 'high'))
            config.write_text('model = "bad model; rm"\nmodel_reasoning_effort = "turbo"\n')
            self.assertEqual(codex_preferences({'CODEX_HOME': temporary}), (None, None))

    def test_the_users_choice_applies_only_when_the_account_supports_it(self):
        from clipen.providers.account import resolve_codex_model
        from clipen.providers.codex import model_config
        models = [{'model': 'gpt-5.6-sol', 'name': 'GPT-5.6-Sol', 'default': True, 'default_effort': 'low', 'efforts': ['low', 'medium', 'high']},
                  {'model': 'gpt-5.6-luna', 'name': 'GPT-5.6-Luna', 'default': False, 'default_effort': 'low', 'efforts': ['low', 'medium']}]
        # A model this account cannot run falls back to the default; a supported effort still applies.
        fallback = resolve_codex_model(('gpt-6.1-sol', 'high'), models)
        self.assertEqual((fallback['id'], fallback['effort'], fallback['apply_model'], fallback['unavailable']), ('gpt-5.6-sol', 'high', False, 'gpt-6.1-sol'))
        luna = resolve_codex_model(('gpt-5.6-luna', 'high'), models)
        self.assertEqual((luna['id'], luna['apply_model'], luna['apply_effort'], luna['effort']), ('gpt-5.6-luna', True, False, 'low'))
        self.assertIsNone(resolve_codex_model(('gpt-5.6-luna', 'high'), []))
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / 'config.toml').write_text('model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"\n')
            with patch('clipen.providers.account.available_models', return_value=models):
                self.assertEqual(model_config('/bin/codex', {'CODEX_HOME': temporary}), ('-c', 'model_reasoning_effort="high"'))
            with patch('clipen.providers.account.available_models', return_value=[]):
                self.assertEqual(model_config('/bin/codex', {'CODEX_HOME': temporary}), ())
            # Without a config there is nothing to carry over, and no account lookup.
            with patch('clipen.providers.account.available_models') as lookup:
                self.assertEqual(model_config('/bin/codex', {'HOME': '/nonexistent'}), ())
            lookup.assert_not_called()

    def test_claude_runs_with_the_model_and_effort_from_claude_code_settings(self):
        from clipen.providers.claude import claude_preferences, model_arguments
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / 'settings.json'
            self.assertEqual(model_arguments({'CLAUDE_CONFIG_DIR': temporary}), ())
            settings.write_text(json.dumps({'model': 'opus', 'effortLevel': 'high', 'hooks': {'Stop': []}}))
            self.assertEqual(model_arguments({'CLAUDE_CONFIG_DIR': temporary}), ('--model', 'opus', '--effort', 'high'))
            settings.write_text(json.dumps({'model': 'opus; rm -rf', 'effortLevel': 'turbo'}))
            self.assertEqual(claude_preferences({'CLAUDE_CONFIG_DIR': temporary}), (None, None))
            # The status bar names the chosen model before the first run, then the model that ran.
            settings.write_text(json.dumps({'model': 'opus', 'effortLevel': 'high'}))
            root = Path(temporary) / 'attempts'; root.mkdir()
            before = claude_account(root, {'CLAUDE_CONFIG_DIR': temporary})['model']
            self.assertEqual((before['name'], before['effort'], before['source']), ('Opus', 'high', 'claude_config'))
            record_claude_account(root, [{'type': 'system', 'subtype': 'init', 'model': 'claude-opus-5'}], 'opus')
            self.assertEqual(claude_account(root, {'CLAUDE_CONFIG_DIR': temporary})['model']['name'], 'Claude Opus 5')
            settings.write_text(json.dumps({'model': 'sonnet'}))
            self.assertEqual(claude_account(root, {'CLAUDE_CONFIG_DIR': temporary})['model']['name'], 'Sonnet')

    def test_claude_keeps_what_the_last_attempt_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(claude_account(root, {'HOME': '/nonexistent'}), {'model': None, 'quota': None})
            record_claude_account(root, [
                {'type': 'system', 'subtype': 'init', 'model': 'claude-sonnet-5-20261001'},
                {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'resetsAt': 1792767600, 'rateLimitType': 'five_hour'}},
                {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected', 'resetsAt': 1792771200, 'rateLimitType': 'five_hour'}},
            ])
            account = claude_account(root, {'HOME': '/nonexistent'})
            self.assertEqual((account['model']['id'], account['model']['name']), ('claude-sonnet-5-20261001', 'Claude Sonnet 5'))
            self.assertEqual((account['model']['source'], account['model']['effort']), ('default', None))
            self.assertEqual(account['quota']['source'], 'last_run')
            self.assertTrue(account['quota']['limited'])
            self.assertEqual(account['quota']['resets_at'], 1792771200)
            self.assertEqual(account['quota']['windows'], [])
            (root / 'claude-account.json').write_text('{not json')
            self.assertEqual(claude_account(root, {'HOME': '/nonexistent'}), {'model': None, 'quota': None})


if __name__ == '__main__':
    unittest.main()
