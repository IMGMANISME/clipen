"""Field definitions for interactively binding a provider. A provider
can be used in either role -- narrative (usually cloud) or vision
(usually local) -- the binding mechanism itself doesn't care which."""

from __future__ import annotations

FIELD_API_KEY = "api_key"

PROVIDER_KINDS = ("anthropic", "openai-compatible")

# Local/self-hosted OpenAI-compatible servers (Ollama, LM Studio, vLLM...)
# commonly accept any non-empty string as the key -- this lets a user skip
# typing one for a purely local setup.
LOCAL_DUMMY_API_KEY = "not-needed-for-local-server"
