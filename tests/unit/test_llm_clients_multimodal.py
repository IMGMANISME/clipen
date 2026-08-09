from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from clipen.llm.anthropic_client import AnthropicClient
from clipen.llm.base import LLMMessage
from clipen.llm.openai_compatible_client import OpenAICompatibleClient


def _fake_anthropic_response() -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        model="test-model",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        model_dump=lambda: {},
    )


def _fake_openai_response() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        model="test-model",
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        model_dump=lambda: {},
    )


def test_anthropic_text_only_message_sends_bare_string():
    client = AnthropicClient(api_key="test", model="test-model")
    client._client.messages.create = MagicMock(return_value=_fake_anthropic_response())

    client.complete([LLMMessage(role="user", content="hello")], max_tokens=10)

    kwargs = client._client.messages.create.call_args.kwargs
    assert kwargs["messages"] == [{"role": "user", "content": "hello"}]


def test_anthropic_image_message_sends_content_blocks():
    client = AnthropicClient(api_key="test", model="test-model")
    client._client.messages.create = MagicMock(return_value=_fake_anthropic_response())

    client.complete(
        [LLMMessage(role="user", content="look", images=(b"fake-jpeg-bytes",))], max_tokens=10
    )

    kwargs = client._client.messages.create.call_args.kwargs
    content = kwargs["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/jpeg"


def test_openai_compatible_text_only_message_sends_bare_string():
    client = OpenAICompatibleClient(api_key="test", model="test-model")
    client._client.chat.completions.create = MagicMock(return_value=_fake_openai_response())

    client.complete([LLMMessage(role="user", content="hello")], max_tokens=10)

    kwargs = client._client.chat.completions.create.call_args.kwargs
    assert kwargs["messages"] == [{"role": "user", "content": "hello"}]


def test_openai_compatible_image_message_sends_image_url_blocks():
    client = OpenAICompatibleClient(api_key="test", model="test-model")
    client._client.chat.completions.create = MagicMock(return_value=_fake_openai_response())

    client.complete(
        [LLMMessage(role="user", content="look", images=(b"fake-jpeg-bytes",))], max_tokens=10
    )

    kwargs = client._client.chat.completions.create.call_args.kwargs
    content = kwargs["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_openai_compatible_json_instruction_appended_to_existing_system_message():
    client = OpenAICompatibleClient(api_key="test", model="test-model")
    client._client.chat.completions.create = MagicMock(return_value=_fake_openai_response())

    client.complete(
        [LLMMessage(role="system", content="be terse"), LLMMessage(role="user", content="hi")],
        max_tokens=10,
        response_format="json",
    )

    kwargs = client._client.chat.completions.create.call_args.kwargs
    assert kwargs["messages"][0]["role"] == "system"
    assert "be terse" in kwargs["messages"][0]["content"]
    assert "JSON" in kwargs["messages"][0]["content"]
    assert len(kwargs["messages"]) == 2
