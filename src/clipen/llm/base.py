"""Provider-agnostic LLM client interface.

Deliberately a single method: this codebase has exactly one call site
that matters (narrative assembly during `clipen project edit`), so
there's no need to abstract vendor-specific structured-output/tool-calling
APIs. Structured results are obtained by instructing the model to emit
JSON and parsing the text with a pydantic model on the caller's side.

`LLMMessage.images` is multimodal support baked in from the start (not
bolted on later): empty by default (pure text, the common case), or a
tuple of raw JPEG bytes when a caller wants the model to actually look at
frames (see `analysis/visual.py`). Concrete clients only build a
multimodal request when `images` is non-empty; an empty tuple produces
the exact same request shape as if the field didn't exist.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]
ResponseFormat = Literal["text", "json"]


@dataclass(frozen=True)
class LLMMessage:
    role: Role
    content: str
    images: tuple[bytes, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    raw: dict[str, Any]
    cached: bool = False


class LLMClient(ABC):
    @abstractmethod
    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.0,
        response_format: ResponseFormat = "text",
    ) -> LLMResponse: ...
