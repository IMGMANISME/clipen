"""The AI providers Clipen can be launched with, and what each one supports.

Adding a provider means implementing an editing agent with the same
constructor and ``execute(project_id, run_id, *, cancel)`` contract as
``CodexEditingAgent``, a model-free probe in the Controller, and an entry here.

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
    editing_agent: str | None
    # Plan generation, thumbnail observation and the two-pass finishing
    # workflow still call `codex exec` directly with an output schema.
    legacy_workflows: bool

    @property
    def implemented(self) -> bool:
        return self.editing_agent is not None


PROVIDERS: dict[str, ProviderDescriptor] = {
    "codex": ProviderDescriptor(
        "codex", "Codex", "clipen.providers.codex_editing:CodexEditingAgent", True
    ),
    "claude": ProviderDescriptor(
        "claude", "Claude", "clipen.providers.claude_editing:ClaudeEditingAgent", False
    ),
    # Reserved: a local model needs its own agent loop over the Clipen MCP
    # tools before it can be selected.
    "local": ProviderDescriptor("local", "本機模型", None, False),
}


def get_provider(provider_id: str) -> ProviderDescriptor:
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        raise ValueError(f"unknown provider: {provider_id}") from None


def implemented_providers() -> tuple[str, ...]:
    return tuple(key for key, value in PROVIDERS.items() if value.implemented)


def editing_agent_factory(provider_id: str):
    descriptor = get_provider(provider_id)
    if descriptor.editing_agent is None:
        raise ValueError(f"provider {provider_id} has no editing agent yet")
    module, attribute = descriptor.editing_agent.split(":")
    return getattr(import_module(module), attribute)
