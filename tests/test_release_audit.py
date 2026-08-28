import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.release_audit import audit, content_findings, forbidden


class ReleaseAuditTests(unittest.TestCase):
    def test_product_files_are_allowed(self):
        for path in ('.codex/config.toml', '.mcp.json', 'ARCHITECTURE.md',
                     'docs/SKILL_PROTECTION.md', 'skills/system/clipen-core/SKILL.md',
                     'skills/system-manifest.sig', '.github/workflows/prototype.yml', '.env.example'):
            self.assertFalse(forbidden(path), path)

    def test_development_and_private_paths_are_rejected(self):
        for path in ('docs/PLAN.md', 'skills/dev/SKILL.md', '.agents/skills/foo/SKILL.md',
                     '.codex/skills/foo/SKILL.md', '.codex/auth.json', '.claude/settings.local.json',
                     '.clipen/prototype/state.sqlite', 'key.pem', 'input.mov', '.env', 'AGENTS.md',
                     'node_modules/tool/index.js', '__pycache__/cache.pyc', 'outputs/result.json',
                     'input.braw', 'audio.m4a', 'model.safetensors', 'project.dra/Media/file'):
            self.assertTrue(forbidden(path), path)

    def test_content_scan_reports_kinds_not_secret_values(self):
        private_key = b'-----BEGIN ' + b'PRIVATE KEY-----'
        link = b'http://127.0.0.1:12345/#' + b'A' * 43
        self.assertEqual(content_findings(private_key), ['private-key'])
        self.assertEqual(content_findings(link), ['private-ui-link'])
        self.assertEqual(content_findings(b'-----BEGIN PUBLIC KEY-----'), [])

    def test_deleted_development_files_still_block_outgoing_history(self):
        with tempfile.TemporaryDirectory(prefix='clipen-release-audit-') as directory:
            root = Path(directory)
            env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
            def git(*args):
                return subprocess.check_output(['git', '-C', directory, '-c', 'core.hooksPath=' + os.devnull,
                    '-c', 'user.name=Test Fixture', '-c', 'user.email=fixture@example.invalid', *args], env=env, stderr=subprocess.PIPE)
            git('init')
            (root / 'README.md').write_text('Public product fixture')
            git('add', 'README.md'); git('commit', '-m', 'base')
            base = git('rev-parse', 'HEAD').decode().strip()
            (root / 'docs').mkdir()
            (root / 'docs/DEVELOPMENT.md').write_text('Local-only fixture')
            git('add', 'docs/DEVELOPMENT.md'); git('commit', '-m', 'development')
            (root / 'docs/DEVELOPMENT.md').unlink()
            git('add', '-u'); git('commit', '-m', 'remove development')
            before = git('status', '--porcelain')
            self.assertEqual(audit(root), [])
            findings = audit(root, base)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0][1:], ('docs/DEVELOPMENT.md', 'private-or-development-history'))
            self.assertEqual(git('status', '--porcelain'), before)
            (root / 'broken-link').symlink_to('missing-target')
            self.assertIn(('working-tree', 'broken-link', 'symlink'), audit(root))
            git('add', 'broken-link')
            git('commit', '-m', 'symlink fixture')
            self.assertTrue(any(item[1:] == ('broken-link', 'symlink-history')
                                for item in audit(root, base)))


if __name__ == '__main__':
    unittest.main()
