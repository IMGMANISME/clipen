"""Product MCP entry point. Resolve this checkout from the script, not a user path."""
from pathlib import Path
import sys

if sys.version_info < (3, 10):
    print('Clipen requires Python 3.10+ on PATH as python3. No UI was started.', file=sys.stderr)
    raise SystemExit(1)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prototypes.local_ui.__main__ import main

if __name__ == '__main__':
    raise SystemExit(main(['mcp', *sys.argv[1:]]))
