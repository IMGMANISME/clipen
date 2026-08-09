from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_clipen_home(tmp_path, monkeypatch):
    """Every test gets its own ~/.clipen equivalent -- keeps provider
    config, credentials, and analysis/LLM caches from one test leaking
    into another."""
    home = tmp_path / "clipen_home"
    monkeypatch.setenv("CLIPEN_HOME", str(home))
    yield home


@pytest.fixture
def footage_dir(tmp_path):
    dest = tmp_path / "footage"
    dest.mkdir()
    clips = list(FIXTURES_DIR.glob("*.mp4"))
    if not clips:
        pytest.skip("No fixture clips found -- run tests/fixtures/generate_fixtures.sh first")
    for clip in clips:
        shutil.copy(clip, dest / clip.name)
    return dest
