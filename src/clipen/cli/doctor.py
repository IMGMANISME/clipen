"""`clipen doctor` -- catches environment problems (missing ffmpeg, no
secure credential storage) before they turn into a confusing failure
mid-pipeline, with a concrete fix rather than a bare error."""

from __future__ import annotations

import shutil

import typer
from rich.console import Console

from clipen.auth.credential_store import _keyring_available

app = typer.Typer(help="Check that your environment is set up correctly.")
console = Console()


@app.callback(invoke_without_command=True)
def doctor() -> None:
    """Check ffmpeg/ffprobe and credential storage."""
    ok = True

    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if path:
            console.print(f"[green]OK[/green]  {tool} found at {path}")
        else:
            console.print(
                f"[red]MISSING[/red]  {tool} not found on PATH -- install ffmpeg "
                "(e.g. `brew install ffmpeg`) and try again."
            )
            ok = False

    if _keyring_available():
        console.print("[green]OK[/green]  secure keyring available for storing API keys")
    else:
        console.print(
            "[yellow]NOTE[/yellow]  no secure keyring backend found -- API keys will "
            "be stored in a local file instead."
        )

    if ok:
        console.print("\n[green]Environment looks good.[/green]")
    else:
        console.print("\n[red]Fix the issues above before running `clipen project edit`.[/red]")
        raise typer.Exit(code=1)
