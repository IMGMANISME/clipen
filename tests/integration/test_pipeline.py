"""End-to-end tests against real fixture footage: ClipenProject.init ->
analyze -> edit -> render, with a fake OpenAI-compatible server standing
in for the real narrative/vision LLM calls."""

from __future__ import annotations

import sys
from pathlib import Path

import opentimelineio as otio
import pysrt
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fake_openai_server import FakeOpenAIServer  # noqa: E402

from clipen.auth.credential_store import FileCredentialStore  # noqa: E402
from clipen.auth.providers import FIELD_API_KEY  # noqa: E402
from clipen.config.settings import ProviderConfig, upsert_provider  # noqa: E402
from clipen.editing.narrative import build_narrative_edit_decisions  # noqa: E402
from clipen.llm.base import LLMResponse  # noqa: E402
from clipen.project.manager import ClipenProject  # noqa: E402

TEST_PROVIDER_NAME = "test-provider"


@pytest.fixture
def bound_test_provider(monkeypatch):
    import clipen.auth.credential_store as cs_module

    monkeypatch.setattr(cs_module, "_keyring_available", lambda: False)

    with FakeOpenAIServer() as server:
        provider = ProviderConfig(
            name=TEST_PROVIDER_NAME,
            kind="openai-compatible",
            model="fake-model",
            base_url=server.base_url,
        )
        upsert_provider(provider, set_default=True)
        FileCredentialStore().set(TEST_PROVIDER_NAME, FIELD_API_KEY, "dummy-key")
        yield server


def test_full_pipeline_reorders_and_excludes_clips(footage_dir, bound_test_provider):
    project = ClipenProject.init(footage_dir)
    project.analyze()
    project.edit(provider_name=TEST_PROVIDER_NAME)

    # exactly one narrative LLM call for the whole edit, no vision calls
    assert bound_test_provider.call_count == 1
    assert project.state.last_edit_llm_calls == 1
    assert project.state.last_edit_cache_hits == 0

    timeline = otio.adapters.read_from_file(
        str(footage_dir / ".clipen" / "timeline.otio"), adapter_name="otio_json"
    )
    clips = [item for item in timeline.tracks[0] if isinstance(item, otio.schema.Clip)]
    # take-collapsing leaves 2 candidates (intro-group winner, walk);
    # the fake server reverses order and drops the last -- 1 clip survives
    assert len(clips) == 1
    assert clips[0].name == "walk"

    captions_path = footage_dir / ".clipen" / "captions.srt"
    assert captions_path.exists()
    subs = pysrt.open(str(captions_path))
    assert len(subs) > 0

    result = project.render()
    assert result.output_path.exists()
    assert result.clip_count == 1


def test_render_alone_never_reanalyzes_or_calls_the_llm_again(footage_dir, bound_test_provider):
    project = ClipenProject.init(footage_dir)
    project.analyze()
    project.edit(provider_name=TEST_PROVIDER_NAME)
    assert bound_test_provider.call_count == 1

    project.render(output_name="output2.mp4")

    assert bound_test_provider.call_count == 1
    assert (footage_dir / ".clipen" / "render" / "output2.mp4").exists()


def test_repeat_edit_on_unchanged_footage_is_a_cache_hit(footage_dir, bound_test_provider):
    project = ClipenProject.init(footage_dir)
    project.analyze()
    project.edit(provider_name=TEST_PROVIDER_NAME)
    first_calls = bound_test_provider.call_count

    project.edit(provider_name=TEST_PROVIDER_NAME)

    # same HTTP call count on the wire (no new network request)...
    assert bound_test_provider.call_count == first_calls
    # ...but the second edit's own stats show it as a cache hit
    assert project.state.last_edit_cache_hits == 1


def test_vision_provider_runs_before_and_enriches_narrative_call(footage_dir, bound_test_provider):
    project = ClipenProject.init(footage_dir)
    project.analyze()
    project.edit(provider_name=TEST_PROVIDER_NAME, vision_provider_name=TEST_PROVIDER_NAME)

    assert project.state.last_edit_vision_provider == TEST_PROVIDER_NAME
    # 2 winners after take-collapsing -> 2 vision calls + 1 narrative call
    assert bound_test_provider.call_count == 3
    assert project.state.last_edit_llm_calls == 3

    narrative_call = bound_test_provider.call_log[-1]
    narrative_text = " ".join(
        m.get("content") if isinstance(m.get("content"), str) else ""
        for m in narrative_call["messages"]
    )
    assert "A person talks to the camera" in narrative_text


class _UnparseableClient:
    def complete(self, messages, *, max_tokens, temperature=0.0, response_format="text"):
        return LLMResponse(text="not valid json", model="broken", input_tokens=0, output_tokens=0, raw={})


def test_narrative_fallback_keeps_everything_on_unparseable_response(footage_dir):
    source_paths = sorted(footage_dir.glob("*.mp4"))
    decisions = build_narrative_edit_decisions(source_paths, _UnparseableClient())

    # take-collapsing still applies locally regardless of the LLM response
    assert len(decisions) == 2
    names = {d.source_path.name for d in decisions}
    assert "walk.mp4" in names
    for decision in decisions:
        assert decision.start == 0.0
        assert decision.end > 0.0
