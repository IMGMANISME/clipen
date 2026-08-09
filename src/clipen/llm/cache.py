"""Caches LLM responses by a hash of the exact request (messages, images
included, plus generation params). Re-running `project edit` on unchanged
footage and instructions never re-spends tokens -- the narrative call and
every per-clip vision call are both cached this way."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from clipen.config import paths as clipen_paths
from clipen.llm.base import LLMClient, LLMMessage, LLMResponse, ResponseFormat


class CachingLLMClient(LLMClient):
    def __init__(self, inner: LLMClient, *, cache_namespace: str) -> None:
        self._inner = inner
        self._namespace = cache_namespace

    def _cache_key(
        self,
        messages: list[LLMMessage],
        max_tokens: int,
        temperature: float,
        response_format: ResponseFormat,
    ) -> str:
        hasher = hashlib.sha256()
        hasher.update(self._namespace.encode())
        for message in messages:
            hasher.update(message.role.encode())
            hasher.update(message.content.encode())
            for image_bytes in message.images:
                hasher.update(image_bytes)
        hasher.update(f"{max_tokens}:{temperature}:{response_format}".encode())
        return hasher.hexdigest()

    def _cache_path(self, key: str) -> Path:
        return clipen_paths.llm_cache_dir() / f"{key}.json"

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.0,
        response_format: ResponseFormat = "text",
    ) -> LLMResponse:
        key = self._cache_key(messages, max_tokens, temperature, response_format)
        path = self._cache_path(key)
        if path.exists():
            data = json.loads(path.read_text())
            return LLMResponse(
                text=data["text"],
                model=data["model"],
                input_tokens=data["input_tokens"],
                output_tokens=data["output_tokens"],
                raw=data["raw"],
                cached=True,
            )

        response = self._inner.complete(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
        )
        path.write_text(
            json.dumps(
                {
                    "text": response.text,
                    "model": response.model,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "raw": response.raw,
                }
            )
        )
        return response
