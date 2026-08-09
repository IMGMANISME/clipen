"""`clipen project` -- the granular pipeline commands: init, analyze,
edit, render, status. `clipen project edit` is the only command that may
spend tokens; every parameter that used to live in a reusable Skill
object is now a plain, visible CLI flag with a sensible default, so a
first-time user never has to learn them to get a working edit."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import typer
from rich.console import Console

from clipen.analysis.silence import DEFAULT_MIN_SILENCE_DURATION, DEFAULT_NOISE_THRESHOLD_DB
from clipen.project.manager import ClipenProject, NoFootageFoundError, ProjectNotFoundError

app = typer.Typer(help="Run the pipeline one stage at a time: init, analyze, edit, render.")
console = Console()


@app.command("init")
def init(
    path: Path = typer.Argument(Path("."), help="Folder of raw footage"),
    footage: str | None = typer.Option(
        None, "--footage", help="Glob for footage files; omit to auto-detect mp4/mov/mxf/avi/mkv"
    ),
) -> None:
    """Set up a clipen project in a folder of raw footage."""
    try:
        project = ClipenProject.init(path, footage_glob=footage)
    except NoFootageFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    console.print(
        f"[green]Initialized project[/green] at {path} "
        f"({len(project.state.source_files)} footage file(s) found)."
    )


@app.command("analyze")
def analyze(path: Path = typer.Argument(Path("."))) -> None:
    """Run local-only analysis: speech-to-text, shot detection, silence
    detection. No AI calls, no tokens spent."""
    project = _load_or_exit(path)
    console.print("Analyzing locally (speech-to-text, shots, silence) -- no tokens spent...")
    project.analyze()
    console.print(f"[green]Done.[/green] Analyzed {len(project.state.source_files)} file(s).")


@app.command("edit")
def edit(
    path: Path = typer.Argument(Path(".")),
    provider: str | None = typer.Option(
        None, "--provider", help="Provider to use for narrative assembly (defaults to your default provider)"
    ),
    vision_provider: str | None = typer.Option(
        None,
        "--vision-provider",
        help="Optional: a second, usually local, vision-capable provider to look at each clip's frames",
    ),
    instructions: str | None = typer.Option(
        None, "--instructions", help="Free-text style guidance, e.g. 'energetic, fast cuts'"
    ),
    instructions_file: Path | None = typer.Option(
        None, "--instructions-file", help="Read style guidance from a text file instead"
    ),
    target_duration: float | None = typer.Option(
        None, "--target-duration", help="Soft target runtime in seconds -- not a hard guarantee"
    ),
    noise_threshold_db: float = typer.Option(
        DEFAULT_NOISE_THRESHOLD_DB, "--noise-threshold-db", help="Silence detection sensitivity"
    ),
    min_silence_duration: float = typer.Option(
        DEFAULT_MIN_SILENCE_DURATION, "--min-silence-duration", help="Minimum gap length counted as silence"
    ),
    transition: Literal["hard_cut", "crossfade"] = typer.Option(
        "hard_cut", "--transition", help="Cut style between clips"
    ),
    transition_duration: float = typer.Option(
        0.5, "--transition-duration", help="Crossfade length in seconds (ignored for hard_cut)"
    ),
    caption_mode: Literal["soft_sub", "burn_in"] = typer.Option(
        "soft_sub", "--caption-mode", help="soft_sub = separate subtitle track, burn_in = baked into the video"
    ),
    max_chars_per_line: int = typer.Option(
        42, "--max-chars-per-line", help="Caption line-wrap width"
    ),
) -> None:
    """Decide which clips to keep, what order to play them in, and
    roughly which part of each is worth keeping. Writes timeline.otio +
    captions.srt. This is the only command that may call an AI."""
    if instructions is not None and instructions_file is not None:
        console.print("[red]Error:[/red] pass only one of --instructions or --instructions-file.")
        raise typer.Exit(code=1)
    if instructions_file is not None:
        instructions = instructions_file.read_text()

    project = _load_or_exit(path)
    console.print("Making edit decisions (calls the AI)...")
    try:
        project.edit(
            provider_name=provider,
            vision_provider_name=vision_provider,
            instructions=instructions or "",
            target_duration_seconds=target_duration,
            noise_threshold_db=noise_threshold_db,
            min_silence_duration=min_silence_duration,
            transition_style=transition,
            transition_duration=transition_duration,
            caption_mode=caption_mode,
            max_chars_per_line=max_chars_per_line,
        )
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None

    s = project.state
    console.print(
        f"[green]Wrote timeline.otio + captions.srt[/green] "
        f"({s.last_edit_llm_calls} AI call(s), {s.last_edit_cache_hits} from cache) -- "
        f"tokens: {s.last_edit_input_tokens} in / {s.last_edit_output_tokens} out "
        f"({s.last_edit_input_tokens + s.last_edit_output_tokens} total)"
    )
    console.print("Not happy with a cut or caption? Edit .clipen/timeline.otio or "
                  ".clipen/captions.srt directly and re-run `clipen project render`.")


@app.command("render")
def render(
    path: Path = typer.Argument(Path(".")),
    output: str = typer.Option("output.mp4", "--output", help="Output filename under .clipen/render/"),
    burn_in: bool | None = typer.Option(
        None, "--burn-in/--soft-sub", help="Override this render's caption mode without re-running edit"
    ),
) -> None:
    """Render the timeline + captions to a video. Pure ffmpeg -- never
    touches analysis or any AI client, so a hand-edited timeline/caption
    file renders exactly as written."""
    project = _load_or_exit(path)
    console.print("Rendering...")
    try:
        result = project.render(output_name=output, burn_in=burn_in)
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    console.print(
        f"[green]Done![/green] {result.output_path} "
        f"({result.duration:.1f}s, {result.clip_count} clip(s))"
    )


@app.command("status")
def status(path: Path = typer.Argument(Path("."))) -> None:
    """Show what state this project is in."""
    project = _load_or_exit(path)
    s = project.state
    console.print(f"Footage: {len(s.source_files)} file(s), glob={s.footage_glob or 'auto-detected'}")
    console.print(f"Created:  {s.created_at}")
    console.print(f"Analyzed: {s.analyzed_at or 'not yet'}")
    console.print(f"Edited:   {s.edited_at or 'not yet'}")
    if s.edited_at:
        console.print(
            f"  provider={s.last_edit_provider}, vision_provider={s.last_edit_vision_provider or 'none'}, "
            f"caption_mode={s.last_edit_caption_mode}"
        )
        console.print(
            f"  {s.last_edit_llm_calls} AI call(s), {s.last_edit_cache_hits} from cache, "
            f"{s.last_edit_input_tokens + s.last_edit_output_tokens} tokens total"
        )
    console.print(f"Rendered: {s.rendered_at or 'not yet'}")


def _load_or_exit(path: Path) -> ClipenProject:
    try:
        return ClipenProject.load(path)
    except ProjectNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
