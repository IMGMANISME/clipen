"""ClipenProject: ties the `.clipen/` directory layout to the three-stage
analyze / edit / render pipeline. `init` behaves like `git init` -- it
creates `.clipen/` next to the user's own footage folder, not under
~/.clipen (which holds only global, cross-project state)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, Field

from clipen.analysis.asr import transcribe
from clipen.analysis.hashing import fingerprint
from clipen.analysis.shots import detect_shots
from clipen.analysis.silence import (
    DEFAULT_MIN_SILENCE_DURATION,
    DEFAULT_NOISE_THRESHOLD_DB,
    detect_silence,
)
from clipen.auth.credential_store import resolve_credential_store
from clipen.config import paths as clipen_paths
from clipen.config.settings import load_config
from clipen.editing.captions import build_captions, write_captions
from clipen.editing.narrative import build_narrative_edit_decisions
from clipen.editing.timeline_builder import build_timeline, write_timeline
from clipen.llm.factory import build_llm_client
from clipen.llm.tracking import TrackingLLMClient
from clipen.render.ffmpeg_driver import RenderResult
from clipen.render.ffmpeg_driver import render as ffmpeg_render

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class ProjectNotFoundError(RuntimeError):
    pass


class NoFootageFoundError(RuntimeError):
    pass


# Common raw-footage extensions from consumer/prosumer cameras. Matched
# case-insensitively so a plain `clipen project init` with no --footage
# flag just works, instead of requiring the user to know glob syntax.
DEFAULT_VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".mxf", ".avi", ".mkv")


class ProjectState(BaseModel):
    footage_glob: str | None = None  # None means auto-detected by extension
    source_files: list[str] = Field(default_factory=list)  # project-relative paths
    created_at: str
    analyzed_at: str | None = None
    edited_at: str | None = None
    rendered_at: str | None = None
    last_edit_provider: str | None = None
    last_edit_vision_provider: str | None = None
    last_edit_instructions: str = ""
    last_edit_caption_mode: Literal["soft_sub", "burn_in"] = "soft_sub"
    last_edit_llm_calls: int = 0
    last_edit_cache_hits: int = 0
    last_edit_input_tokens: int = 0
    last_edit_output_tokens: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClipenProject:
    def __init__(self, root: Path, state: ProjectState) -> None:
        self.root = root
        self.state = state

    # -- lifecycle ---------------------------------------------------

    @classmethod
    def init(cls, root: Path, footage_glob: str | None = None) -> "ClipenProject":
        """footage_glob=None auto-detects common video extensions in
        `root` (non-recursive). Pass an explicit glob (e.g. "raw/**/*.mp4")
        only when auto-detection isn't the right fit."""
        root = root.resolve()
        project_dir = clipen_paths.project_dir(root)
        (project_dir / "analysis").mkdir(parents=True, exist_ok=True)

        if footage_glob:
            source_files = sorted(
                str(p.relative_to(root)) for p in root.glob(footage_glob) if p.is_file()
            )
        else:
            source_files = sorted(
                str(p.relative_to(root))
                for p in root.iterdir()
                if p.is_file() and p.suffix.lower() in DEFAULT_VIDEO_EXTENSIONS
            )

        if not source_files:
            hint = (
                f"glob '{footage_glob}' matched nothing"
                if footage_glob
                else f"no {'/'.join(DEFAULT_VIDEO_EXTENSIONS)} files found in {root}"
            )
            raise NoFootageFoundError(
                f"No footage to work with: {hint}. Pass --footage with an explicit "
                "glob if your files use a different extension or live in a subfolder."
            )

        state = ProjectState(footage_glob=footage_glob, source_files=source_files, created_at=_now())
        project = cls(root, state)
        project._save()
        return project

    @classmethod
    def load(cls, root: Path) -> "ClipenProject":
        root = root.resolve()
        toml_path = clipen_paths.project_toml_path(root)
        if not toml_path.exists():
            raise ProjectNotFoundError(
                f"No clipen project at {root} (run `clipen project init` first)"
            )
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
        return cls(root, ProjectState.model_validate(data))

    def _save(self) -> None:
        toml_path = clipen_paths.project_toml_path(self.root)
        with toml_path.open("wb") as f:
            tomli_w.dump(self.state.model_dump(exclude_none=True), f)

    def source_paths(self) -> list[Path]:
        return [self.root / rel for rel in self.state.source_files]

    # -- stages --------------------------------------------------------

    def analyze(self) -> dict[str, str]:
        """Local-only: speech-to-text + shots + silence, cache-aware. No
        LLM calls -- not even vision. Vision only ever runs during
        `edit(vision_provider_name=...)`, so a project that never asks
        for it never pays for or waits on it here."""
        manifest: dict[str, str] = {}
        for rel, path in zip(self.state.source_files, self.source_paths()):
            transcribe(path)
            detect_shots(path)
            detect_silence(path)
            manifest[rel] = fingerprint(path)

        manifest_path = clipen_paths.analysis_manifest_path(self.root)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

        self.state.analyzed_at = _now()
        self._save()
        return manifest

    def edit(
        self,
        provider_name: str | None = None,
        *,
        vision_provider_name: str | None = None,
        instructions: str = "",
        target_duration_seconds: float | None = None,
        noise_threshold_db: float = DEFAULT_NOISE_THRESHOLD_DB,
        min_silence_duration: float = DEFAULT_MIN_SILENCE_DURATION,
        transition_style: Literal["hard_cut", "crossfade"] = "hard_cut",
        transition_duration: float = 0.5,
        caption_mode: Literal["soft_sub", "burn_in"] = "soft_sub",
        max_chars_per_line: int | None = 42,
    ) -> None:
        """Whole-project narrative assembly: decides which clips to keep,
        what order to play them in, and roughly which sub-span of each is
        worth keeping. The ONLY stage that may call an LLM -- both the
        narrative call and, if vision_provider_name is given, one extra
        local vision call per kept clip."""
        config = load_config()
        provider_name = provider_name or config.default_provider
        if provider_name is None or provider_name not in config.providers:
            raise ValueError(
                "No provider bound. Run `clipen auth add <provider>` first, "
                "or pass --provider explicitly."
            )
        provider = config.providers[provider_name]
        credential_store = resolve_credential_store(warn=print)
        tracked_client = TrackingLLMClient(build_llm_client(provider, credential_store))

        vision_client = None
        if vision_provider_name is not None:
            if vision_provider_name not in config.providers:
                raise ValueError(
                    f"No provider named '{vision_provider_name}'. Run "
                    f"`clipen auth add {vision_provider_name}` first."
                )
            vision_provider = config.providers[vision_provider_name]
            vision_client = TrackingLLMClient(build_llm_client(vision_provider, credential_store))

        effective_transition_duration = (
            transition_duration if transition_style == "crossfade" else 0.0
        )

        decisions = build_narrative_edit_decisions(
            self.source_paths(),
            tracked_client,
            vision_client=vision_client,
            vision_model_tag=vision_provider_name or "",
            instructions=instructions,
            target_duration_seconds=target_duration_seconds,
            transition_style=transition_style if transition_style == "crossfade" else None,
            noise_threshold_db=noise_threshold_db,
            min_silence_duration=min_silence_duration,
        )

        timeline = build_timeline(
            decisions, project_root=self.root, transition_duration=effective_transition_duration
        )
        write_timeline(timeline, clipen_paths.timeline_path(self.root))

        captions = build_captions(
            decisions,
            transition_duration=effective_transition_duration,
            max_chars_per_line=max_chars_per_line,
        )
        write_captions(captions, clipen_paths.captions_path(self.root))

        self.state.edited_at = _now()
        self.state.last_edit_provider = provider_name
        self.state.last_edit_vision_provider = vision_provider_name
        self.state.last_edit_instructions = instructions
        self.state.last_edit_caption_mode = caption_mode
        self.state.last_edit_llm_calls = tracked_client.stats.calls
        self.state.last_edit_cache_hits = tracked_client.stats.cache_hits
        self.state.last_edit_input_tokens = tracked_client.stats.input_tokens
        self.state.last_edit_output_tokens = tracked_client.stats.output_tokens
        if vision_client is not None:
            self.state.last_edit_llm_calls += vision_client.stats.calls
            self.state.last_edit_cache_hits += vision_client.stats.cache_hits
            self.state.last_edit_input_tokens += vision_client.stats.input_tokens
            self.state.last_edit_output_tokens += vision_client.stats.output_tokens
        self._save()

    def render(
        self, *, output_name: str = "output.mp4", burn_in: bool | None = None
    ) -> RenderResult:
        """Reads ONLY timeline.otio + captions.srt. ffmpeg only -- never
        touches analysis/ or any LLM client. This is what makes a hand
        edit to either file reproducible with `render` alone."""
        if burn_in is None:
            burn_in = self.state.last_edit_caption_mode == "burn_in"
        output_path = clipen_paths.render_output_dir(self.root) / output_name
        result = ffmpeg_render(
            clipen_paths.timeline_path(self.root),
            clipen_paths.captions_path(self.root),
            output_path,
            burn_in=burn_in,
        )
        self.state.rendered_at = _now()
        self._save()
        return result
