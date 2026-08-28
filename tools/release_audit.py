"""Read-only publication checks. Does not stage, rewrite history, or push."""
import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess


def forbidden(name):
    path = PurePosixPath(name)
    if any(part in {'.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache',
                    '.mypy_cache', '.ruff_cache', '.cache'} for part in path.parts):
        return True
    if name.startswith(('exports/', 'output/', 'outputs/', 'renders/', 'tmp/', 'temp/', 'logs/')):
        return True
    if name.startswith('docs/'):
        return name != 'docs/SKILL_PROTECTION.md'
    if name.startswith('skills/'):
        return not (name.startswith('skills/system/') or name in ('skills/system-manifest.json', 'skills/system-manifest.sig'))
    if name.startswith(('.clipen/', '.agents/', '.claude/', '.codex/')):
        return name != '.codex/config.toml'
    return (path.name in ('AGENTS.md', 'CLAUDE.md', 'GEMINI.md', '.DS_Store') or
            (path.name.startswith('.env') and path.name not in ('.env.example', '.env.template')) or
            path.suffix.lower() in ('.pem', '.key', '.p12', '.pfx', '.sqlite', '.log', '.pyc',
                                    '.mp4', '.mov', '.m4v', '.mkv', '.avi', '.webm', '.mxf',
                                    '.mts', '.m2ts', '.braw', '.r3d', '.wav', '.mp3', '.aac',
                                    '.m4a', '.flac', '.aiff', '.drp', '.drt', '.onnx', '.safetensors') or
            any(part.endswith('.dra') for part in path.parts))


PATTERNS = {
    'private-key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'credential-like-value': rb'(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})',
    'personal-absolute-path': rb'/(?:Users|home)/[^/\s]+/',
    'private-ui-link': rb'http://(?:127\.0\.0\.1|localhost):\d+/#[-_A-Za-z0-9]{20,}',
}


def content_findings(data):
    return [name for name, pattern in PATTERNS.items() if re.search(pattern, data)]


def audit(root, base=None):
    def git(*args):
        return subprocess.check_output(['git', '--literal-pathspecs', '-C', str(root), *args], stderr=subprocess.PIPE)

    findings = []
    names = set(filter(None, git('ls-files', '-z').decode().split('\0')))
    names.update(filter(None, git('ls-files', '--others', '--exclude-standard', '-z').decode().split('\0')))
    for name in sorted(names):
        path = root / name
        if not path.is_symlink() and not path.exists():
            continue  # A tracked deletion has no content in the proposed tree.
        if forbidden(name):
            findings.append(('working-tree', name, 'private-or-development-path'))
        if path.is_symlink():
            findings.append(('working-tree', name, 'symlink'))
        elif path.is_file():
            findings.extend(('working-tree', name, kind) for kind in content_findings(path.read_bytes()))
    if base:
        # Resolve to a commit before using it in ranges. No shell interpolation.
        resolved = git('rev-parse', '--verify', '--end-of-options', base + '^{commit}').decode().strip()
        for commit in git('rev-list', resolved + '..HEAD').decode().split():
            changed = git('diff-tree', '--root', '-m', '--no-commit-id', '--diff-filter=AMRT',
                          '--name-only', '-r', '-z', commit).decode().split('\0')
            for name in sorted(set(filter(None, changed))):
                if forbidden(name):
                    findings.append((commit[:12], name, 'private-or-development-history'))
                mode = git('ls-tree', commit, '--', name).split(b' ', 1)[0]
                if mode == b'120000':
                    findings.append((commit[:12], name, 'symlink-history'))
                data = git('show', commit + ':' + name)
                findings.extend((commit[:12], name, kind) for kind in content_findings(data))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', help='Also inspect outgoing commits relative to this existing base, e.g. origin/main.')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        findings = audit(root, args.base)
    except (OSError, subprocess.CalledProcessError):
        print('Audit could not complete. Check the repository and base ref; nothing was published.')
        return 2
    for revision, path, kind in findings:
        print(f'{revision}: {path}: {kind}')  # Report locations, never secret values.
    if findings:
        print('Publication blocked. Keep local work; review before publishing. History was not modified.')
        return 1
    print('Publication scan passed' + (' (tree and outgoing history).' if args.base else ' (tree only; also run with --base before publishing).'))
    print('Pattern checks are not a guarantee that all secrets or private content were detected.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
