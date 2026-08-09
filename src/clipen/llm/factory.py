from __future__ import annotations

from clipen.auth.credential_store import CredentialStore
from clipen.auth.providers import FIELD_API_KEY, LOCAL_DUMMY_API_KEY
from clipen.config.settings import ProviderConfig
from clipen.llm.anthropic_client import AnthropicClient
from clipen.llm.base import LLMClient
from clipen.llm.cache import CachingLLMClient
from clipen.llm.openai_compatible_client import OpenAICompatibleClient


def build_llm_client(
    provider: ProviderConfig, credential_store: CredentialStore, *, cache: bool = True
) -> LLMClient:
    api_key = credential_store.get(provider.name, FIELD_API_KEY) or LOCAL_DUMMY_API_KEY

    client: LLMClient
    if provider.kind == "anthropic":
        client = AnthropicClient(api_key=api_key, model=provider.model)
    else:
        client = OpenAICompatibleClient(
            api_key=api_key, model=provider.model, base_url=provider.base_url
        )

    if not cache:
        return client
    namespace = f"{provider.kind}:{provider.model}:{provider.base_url or ''}"
    return CachingLLMClient(client, cache_namespace=namespace)
