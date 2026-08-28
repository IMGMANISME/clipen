"""Clone-portable product configs. These are not provider approval/AI tests."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from prototypes.local_ui.__main__ import ROOT


def codex_config(path):
    # This shipped config deliberately uses only a table and JSON-compatible
    # scalar/array values, so tests also run on Python 3.10 without a TOML dep.
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('['):
            if line != '[mcp_servers.clipen]':
                raise AssertionError('Unexpected project config table')
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = json.loads(value)
    return values


class ProjectStartupTests(unittest.TestCase):
    def test_shipped_configs_do_not_change_models_credentials_or_approvals(self):
        codex = codex_config(ROOT / '.codex/config.toml')
        self.assertEqual(set(codex), {'command', 'args', 'cwd', 'startup_timeout_sec', 'tool_timeout_sec'})
        # Native Codex 0.150.1 passed a configured ".." through as the child
        # working directory, not relative to .codex/. Keep the repo-root
        # launch directory; do not emulate config anchoring.
        self.assertEqual(codex['cwd'], '.')
        claude = json.loads((ROOT / '.mcp.json').read_text())
        self.assertEqual(set(claude), {'mcpServers'})
        self.assertEqual(set(claude['mcpServers']), {'clipen'})
        self.assertEqual(set(claude['mcpServers']['clipen']), {'type', 'command', 'args'})
        self.assertEqual(claude['mcpServers']['clipen']['type'], 'stdio')
        for config in (codex, claude['mcpServers']['clipen']):
            self.assertEqual(config['command'], 'python3')
            self.assertEqual(config['args'], ['integrations/stdio_server.py'])
            self.assertTrue(all(not Path(arg).is_absolute() for arg in config['args']))
            self.assertNotIn('--no-open', config['args'])

    def test_shipped_command_survives_clone_path_spaces_without_development_docs(self):
        with tempfile.TemporaryDirectory(prefix='clipen-clone-test-') as directory:
            clone = Path(directory) / 'a different clone with spaces'
            clone.mkdir()
            clone = clone.resolve()
            for name in ('prototypes', 'integrations', 'tools', 'skills'):
                shutil.copytree(ROOT / name, clone / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            (clone / '.codex').mkdir()
            shutil.copyfile(ROOT / '.codex/config.toml', clone / '.codex/config.toml')
            shutil.copyfile(ROOT / '.mcp.json', clone / '.mcp.json')
            try:
                claude = json.loads((clone / '.mcp.json').read_text())['mcpServers']['clipen']
                codex = codex_config(clone / '.codex/config.toml')
                self.assertFalse((clone / 'docs').exists())
                for provider, config in [('claude', claude), ('codex', codex)]:
                    with self.subTest(provider=provider):
                        cwd = (clone / config.get('cwd', '.')).resolve()
                        self.assertEqual(cwd, clone)
                        # Verify the actual shipped entry point. Only this test adds
                        # --no-open, avoiding a browser launch during synthetic tests.
                        messages = [
                            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                                'protocolVersion': '2025-06-18',
                                'clientInfo': {'name': 'synthetic-startup-test', 'version': '1'}}},
                            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                        ]
                        result = subprocess.run([sys.executable, *config['args'], '--no-open'], cwd=cwd,
                                                input=''.join(json.dumps(m) + '\n' for m in messages),
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        replies = [json.loads(line) for line in result.stdout.splitlines()]
                        self.assertEqual(len(replies[1]['result']['tools']), 3)
                        self.assertTrue((clone / '.clipen/prototype/state.sqlite').is_file())
            finally:
                for root, _, _ in os.walk(clone):
                    os.chmod(root, 0o700)


if __name__ == '__main__':
    unittest.main()
