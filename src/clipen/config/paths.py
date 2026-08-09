"""Path helpers. `clipen_home()` holds global, cross-project state
(provider config, credentials, the analysis/LLM caches). Project-local
state lives under `<footage folder>/.clipen/`, next to the user's own
footage -- `project init` behaves like `git init`, not like a system
directory."""

from __future__ import annotations

import os
from pathlib import Path


def clipen_home() -> Path:
    override = os.environ.get("CLIPEN_HOME")
    home = Path(override) if override else Path.home() / ".clipen"
    home.mkdir(parents=True, exist_ok=True)
    return home


def config_path() -> Path:
    return clipen_home() / "config.toml"


def credentials_path() -> Path:
    return clipen_home() / "credentials.json"


def llm_cache_dir() -> Path:
    path = clipen_home() / "llm_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def analysis_cache_dir() -> Path:
    path = clipen_home() / "analysis_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_dir(root: Path) -> Path:
    path = root / ".clipen"
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_toml_path(root: Path) -> Path:
    return project_dir(root) / "project.toml"


def analysis_manifest_path(root: Path) -> Path:
    return project_dir(root) / "analysis" / "manifest.json"


def timeline_path(root: Path) -> Path:
    return project_dir(root) / "timeline.otio"


def captions_path(root: Path) -> Path:
    return project_dir(root) / "captions.srt"


def render_output_dir(root: Path) -> Path:
    path = project_dir(root) / "render"
    path.mkdir(parents=True, exist_ok=True)
    return path
