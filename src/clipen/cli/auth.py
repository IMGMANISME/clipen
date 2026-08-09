"""`clipen auth` -- bind LLM providers (cloud or local) so `project edit`
can use them. A provider is just a name; bind as many as you like and
pick which one to use per command (e.g. one cloud provider for narrative
assembly, one local Ollama provider for vision)."""

from __future__ import annotations

import typer
from rich.console import Console

from clipen.auth.credential_store import resolve_credential_store
from clipen.auth.providers import FIELD_API_KEY, LOCAL_DUMMY_API_KEY, PROVIDER_KINDS
from clipen.config.settings import ProviderConfig, load_config, remove_provider, upsert_provider

app = typer.Typer(help="Bind and manage LLM providers (cloud or local).")
console = Console()


def bind_provider_interactively(name: str, *, set_default: bool = False) -> None:
    console.print(f"Binding provider '[bold]{name}[/bold]'.")
    kind = typer.prompt(f"Kind ({'/'.join(PROVIDER_KINDS)})", default="anthropic")
    if kind not in PROVIDER_KINDS:
        raise ValueError(f"Unknown provider kind '{kind}' -- must be one of {PROVIDER_KINDS}")

    if kind == "anthropic":
        model = typer.prompt("Model", default="claude-sonnet-5")
        base_url = None
    else:
        model = typer.prompt("Model (e.g. gpt-4o, qwen2.5vl, llama3)")
        base_url = (
            typer.prompt(
                "Base URL (blank for OpenAI's own API, or a local server e.g. "
                "http://localhost:11434/v1 for Ollama)",
                default="",
            )
            or None
        )

    is_local = bool(base_url) and ("localhost" in base_url or "127.0.0.1" in base_url)
    api_key = typer.prompt(
        "API key (leave blank for a local server that doesn't need one)",
        default=LOCAL_DUMMY_API_KEY if is_local else "",
        hide_input=True,
    )

    provider = ProviderConfig(name=name, kind=kind, model=model, base_url=base_url)
    upsert_provider(provider, set_default=set_default)

    credential_store = resolve_credential_store(warn=console.print)
    credential_store.set(name, FIELD_API_KEY, api_key or LOCAL_DUMMY_API_KEY)
    console.print(f"[green]Saved provider '{name}'.[/green]")


@app.command("add")
def add(
    name: str = typer.Argument(
        ..., help="A name for this provider, e.g. 'claude' or 'ollama-vlm'"
    ),
    default: bool = typer.Option(False, "--default", help="Make this the default provider"),
) -> None:
    """Bind a new LLM provider (asks a few questions interactively)."""
    bind_provider_interactively(name, set_default=default)


@app.command("list")
def list_providers() -> None:
    """Show every bound provider."""
    config = load_config()
    if not config.providers:
        console.print("No providers bound yet. Run `clipen auth add <name>` to add one.")
        raise typer.Exit(code=0)
    for name, provider in config.providers.items():
        marker = " (default)" if name == config.default_provider else ""
        details = f"{provider.kind}, model={provider.model}"
        if provider.base_url:
            details += f", base_url={provider.base_url}"
        console.print(f"  {name}{marker}: {details}")


@app.command("remove")
def remove(name: str = typer.Argument(..., help="Provider name to remove")) -> None:
    """Remove a bound provider and its stored credential."""
    config = load_config()
    if name not in config.providers:
        console.print(f"[yellow]No provider named '{name}'.[/yellow]")
        raise typer.Exit(code=1)
    remove_provider(name)
    credential_store = resolve_credential_store(warn=console.print)
    credential_store.delete(name)
    console.print(f"[green]Removed provider '{name}'.[/green]")
