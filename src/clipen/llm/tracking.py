"""Wraps a client purely to accumulate call/token stats so `project edit`
can print an honest cost summary (including any vision calls, folded into
the same totals -- see `project/manager.py`)."""

from __future__ import annotations

from dataclasses import dataclass

from clipen.llm.base import LLMClient, LLMMessage, LLMResponse, ResponseFormat


@dataclass
class LLMStats:
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class TrackingLLMClient(LLMClient):
    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.stats = LLMStats()

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.0,
        response_format: ResponseFormat = "text",
    ) -> LLMResponse:
        response = self._inner.complete(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
        )
        self.stats.calls += 1
        if response.cached:
            self.stats.cache_hits += 1
        self.stats.input_tokens += response.input_tokens
        self.stats.output_tokens += response.output_tokens
        return response
