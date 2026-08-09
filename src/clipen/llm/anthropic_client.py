from __future__ import annotations

import base64

from clipen.llm.base import LLMClient, LLMMessage, LLMResponse, ResponseFormat


def _content_for(message: LLMMessage) -> str | list[dict]:
    if not message.images:
        return message.content
    blocks: list[dict] = [{"type": "text", "text": message.content}]
    for image_bytes in message.images:
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode(),
                },
            }
        )
    return blocks


class AnthropicClient(LLMClient):
    def __init__(self, api_key: str, model: str) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.0,
        response_format: ResponseFormat = "text",
    ) -> LLMResponse:
        system = "\n".join(m.content for m in messages if m.role == "system") or None
        conversation = [
            {"role": m.role, "content": _content_for(m)} for m in messages if m.role != "system"
        ]
        if response_format == "json":
            instruction = "Respond with valid JSON only, no other text."
            system = f"{system}\n\n{instruction}" if system else instruction

        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=conversation,
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        return LLMResponse(
            text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            raw=response.model_dump(),
        )
