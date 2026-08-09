from __future__ import annotations

from clipen.llm.base import LLMMessage, LLMResponse
from clipen.llm.cache import CachingLLMClient


class _CountingClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, *, max_tokens, temperature=0.0, response_format="text"):
        self.calls += 1
        return LLMResponse(text="result", model="m", input_tokens=1, output_tokens=1, raw={})


def test_second_identical_call_is_a_cache_hit():
    inner = _CountingClient()
    client = CachingLLMClient(inner, cache_namespace="test")
    messages = [LLMMessage(role="user", content="hi")]

    first = client.complete(messages, max_tokens=10)
    second = client.complete(messages, max_tokens=10)

    assert inner.calls == 1
    assert first.cached is False
    assert second.cached is True
    assert second.text == "result"


def test_different_message_content_is_not_a_cache_hit():
    inner = _CountingClient()
    client = CachingLLMClient(inner, cache_namespace="test")

    client.complete([LLMMessage(role="user", content="a")], max_tokens=10)
    client.complete([LLMMessage(role="user", content="b")], max_tokens=10)

    assert inner.calls == 2


def test_different_namespace_is_not_a_cache_hit():
    inner = _CountingClient()
    messages = [LLMMessage(role="user", content="same")]

    CachingLLMClient(inner, cache_namespace="ns-a").complete(messages, max_tokens=10)
    CachingLLMClient(inner, cache_namespace="ns-b").complete(messages, max_tokens=10)

    assert inner.calls == 2


def test_different_images_are_not_a_cache_hit():
    inner = _CountingClient()
    client = CachingLLMClient(inner, cache_namespace="test")

    client.complete([LLMMessage(role="user", content="look", images=(b"a",))], max_tokens=10)
    client.complete([LLMMessage(role="user", content="look", images=(b"b",))], max_tokens=10)

    assert inner.calls == 2
