"""The AI providers Clipen can be launched with, and what each one supports.

Adding a provider means implementing an editing agent with the same
constructor and ``execute(project_id, run_id, *, cancel)`` contract as
``CodexEditingAgent``, an exec adapter with ``CodexExecAdapter``'s
``probe``/``execute``/``reconcile`` contract, a model-free probe in the
Controller, and an entry here.

The source launcher imports this before the runtime exists, so it must only
use the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    display_name: str
    # "module:attribute" so selecting a provider never imports the others.
    # The editing agent drives AI production over the Clipen MCP tools.
    editing_agent: str
    # The exec adapter runs one schema-bound attempt (plan generation,
    # thumbnail observation, two-pass finishing).
    exec_adapter: str


PROVIDERS: dict[str, ProviderDescriptor] = {
    "codex": ProviderDescriptor(
        "codex",
        "Codex",
        "clipen.providers.codex_editing:CodexEditingAgent",
        "clipen.providers.codex:CodexExecAdapter",
    ),
    "claude": ProviderDescriptor(
        "claude",
        "Claude",
        "clipen.providers.claude_editing:ClaudeEditingAgent",
        "clipen.providers.claude:ClaudeExecAdapter",
    ),
}


def get_provider(provider_id: str) -> ProviderDescriptor:
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        raise ValueError(f"unknown provider: {provider_id}") from None


def _load(reference: str):
    module, attribute = reference.split(":")
    return getattr(import_module(module), attribute)


def editing_agent_factory(provider_id: str):
    return _load(get_provider(provider_id).editing_agent)


def exec_adapter_factory(provider_id: str):
    return _load(get_provider(provider_id).exec_adapter)
