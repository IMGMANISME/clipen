from __future__ import annotations

import base64

from clipen.llm.base import LLMClient, LLMMessage, LLMResponse, ResponseFormat


def _content_for(message: LLMMessage) -> str | list[dict]:
    if not message.images:
        return message.content
    blocks: list[dict] = [{"type": "text", "text": message.content}]
    for image_bytes in message.images:
        data_uri = f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode()}"
        blocks.append({"type": "image_url", "image_url": {"url": data_uri}})
    return blocks


class OpenAICompatibleClient(LLMClient):
    """Serves OpenAI/Codex and any self-hosted OpenAI-compatible `/v1`
    server (Ollama, LM Studio, vLLM, ...) -- just a different base_url and
    model, and often a dummy key for local servers. The image_url content-
    block shape used here is also what Ollama's vision models (qwen2.5vl,
    llava, ...) expect."""

    def __init__(self, api_key: str, model: str, base_url: str | None = None) -> None:
        import openai

        self._client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.0,
        response_format: ResponseFormat = "text",
    ) -> LLMResponse:
        chat_messages: list[dict] = [
            {"role": m.role, "content": _content_for(m)} for m in messages
        ]
        if response_format == "json":
            instruction = "Respond with valid JSON only, no other text."
            if chat_messages and chat_messages[0]["role"] == "system":
                first_content = chat_messages[0]["content"]
                if isinstance(first_content, str):
                    chat_messages[0]["content"] = f"{first_content}\n\n{instruction}"
                else:
                    first_content[0]["text"] = f"{first_content[0]['text']}\n\n{instruction}"
            else:
                chat_messages.insert(0, {"role": "system", "content": instruction})

        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=chat_messages,
        )
        choice = response.choices[0]
        usage = response.usage
        return LLMResponse(
            text=choice.message.content or "",
            model=response.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            raw=response.model_dump(),
        )
