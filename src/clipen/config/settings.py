"""Global provider registry (`~/.clipen/config.toml`). A provider is just
a name -> (kind, model, base_url) binding; the actual secret lives in the
credential store, never here."""

from __future__ import annotations

import sys
from typing import Literal

import tomli_w
from pydantic import BaseModel, Field

from clipen.config import paths as clipen_paths

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ProviderKind = Literal["anthropic", "openai-compatible"]


class ProviderConfig(BaseModel):
    name: str
    kind: ProviderKind
    model: str
    base_url: str | None = None


class ClipenConfig(BaseModel):
    default_provider: str | None = None
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)


def load_config() -> ClipenConfig:
    path = clipen_paths.config_path()
    if not path.exists():
        return ClipenConfig()
    with path.open("rb") as f:
        data = tomllib.load(f)
    return ClipenConfig.model_validate(data)


def save_config(config: ClipenConfig) -> None:
    path = clipen_paths.config_path()
    with path.open("wb") as f:
        tomli_w.dump(config.model_dump(exclude_none=True), f)


def upsert_provider(provider: ProviderConfig, *, set_default: bool = False) -> None:
    config = load_config()
    config.providers[provider.name] = provider
    if set_default or config.default_provider is None:
        config.default_provider = provider.name
    save_config(config)


def remove_provider(name: str) -> None:
    config = load_config()
    config.providers.pop(name, None)
    if config.default_provider == name:
        config.default_provider = next(iter(config.providers), None)
    save_config(config)
