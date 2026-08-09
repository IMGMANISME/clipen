"""Top-level CLI assembly. Wraps command execution so clipen's own errors
surface as short, friendly messages -- not raw Python tracebacks -- since
the target user is a professional videographer, not a developer."""

from __future__ import annotations

import typer
from rich.console import Console

from clipen.cli import auth, doctor, project

app = typer.Typer(
    help="clipen -- AI-assisted vlog auto-editing for raw video footage.",
    no_args_is_help=True,
)
app.add_typer(auth.app, name="auth")
app.add_typer(project.app, name="project")
app.add_typer(doctor.app, name="doctor")

console = Console()


def main() -> None:
    try:
        app()
    except (typer.Exit, SystemExit, typer.Abort):
        raise
    except Exception as exc:  # top-level friendly error boundary
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None


if __name__ == "__main__":
    main()
