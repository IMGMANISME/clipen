from __future__ import annotations

from clipen.analysis.visual import describe_clip_visually
from clipen.llm.base import LLMResponse


class _StubVlmClient:
    def __init__(self, text: str = "a description") -> None:
        self.text = text
        self.calls = 0

    def complete(self, messages, *, max_tokens, temperature=0.0, response_format="text"):
        self.calls += 1
        return LLMResponse(text=self.text, model="stub", input_tokens=1, output_tokens=1, raw={})


def test_describe_clip_visually_is_cached(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake video content")
    monkeypatch.setattr("clipen.analysis.visual.extract_frame_jpeg", lambda path, t: b"fake-jpeg")

    stub = _StubVlmClient()
    shots = [{"start": 0.0, "end": 2.0}]

    first = describe_clip_visually(clip, shots, stub, model_tag="test")
    second = describe_clip_visually(clip, shots, stub, model_tag="test")

    assert first == "a description"
    assert second == "a description"
    assert stub.calls == 1


def test_describe_clip_visually_returns_empty_string_on_failure(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake video content")

    def _raise(path, t):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr("clipen.analysis.visual.extract_frame_jpeg", _raise)

    stub = _StubVlmClient()
    shots = [{"start": 0.0, "end": 2.0}]

    result = describe_clip_visually(clip, shots, stub, model_tag="test-failure")
    assert result == ""


def test_describe_clip_visually_different_model_tag_busts_cache(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake video content")
    monkeypatch.setattr("clipen.analysis.visual.extract_frame_jpeg", lambda path, t: b"fake-jpeg")

    stub = _StubVlmClient()
    shots = [{"start": 0.0, "end": 2.0}]

    describe_clip_visually(clip, shots, stub, model_tag="model-a")
    describe_clip_visually(clip, shots, stub, model_tag="model-b")

    assert stub.calls == 2
