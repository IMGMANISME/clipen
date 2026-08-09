from __future__ import annotations

import json
from pathlib import Path

from clipen.editing.narrative import (
    build_narrative_prompt,
    parse_narrative_response,
    summarize_clip_for_narrative,
)


def _seg(start: float, end: float, text: str) -> dict:
    return {"start": start, "end": end, "text": text, "words": []}


# -- summarize_clip_for_narrative ---------------------------------------


def test_summarize_short_clip_passes_through_unmodified():
    transcript = [_seg(0.0, 2.0, "hello there"), _seg(2.0, 4.0, "welcome back")]
    summary = summarize_clip_for_narrative(
        Path("clip.mp4"), transcript, (0.0, 4.0), 4.0, -20.0, None, char_budget=10_000
    )
    assert "clip.mp4" in summary
    assert "hello there" in summary
    assert "welcome back" in summary
    assert "visual:" not in summary


def test_summarize_includes_visual_line_when_given():
    summary = summarize_clip_for_narrative(
        Path("clip.mp4"), [], (0.0, 1.0), 1.0, -20.0, "a person waves", char_budget=10_000
    )
    assert "visual: a person waves" in summary


def test_summarize_long_transcript_stays_within_budget_and_keeps_ends():
    transcript = [_seg(float(i), float(i + 1), f"segment number {i}") for i in range(50)]
    summary = summarize_clip_for_narrative(
        Path("clip.mp4"), transcript, (0.0, 50.0), 50.0, -20.0, None, char_budget=400
    )
    assert len(summary) <= 400 + 200  # small slack for the header/omission note
    assert "segment number 0" in summary
    assert "segment number 49" in summary


# -- build_narrative_prompt ----------------------------------------------


def test_build_narrative_prompt_includes_instructions_verbatim():
    messages = build_narrative_prompt(
        [(Path("a.mp4"), "### a.mp4\nfoo")],
        instructions="fast paced, energetic",
        target_duration_seconds=None,
    )
    system = messages[0].content
    assert "fast paced, energetic" in system
    assert "Target total runtime" not in system


def test_build_narrative_prompt_target_duration_is_framed_as_soft():
    messages = build_narrative_prompt(
        [(Path("a.mp4"), "### a.mp4\nfoo")], instructions="", target_duration_seconds=60.0
    )
    system = messages[0].content
    assert "60" in system
    assert "soft goal" in system


# -- parse_narrative_response ---------------------------------------------


def test_parse_valid_response_assigns_order_and_transitions():
    candidates = [Path("a.mp4"), Path("b.mp4")]
    durations = {Path("a.mp4"): 10.0, Path("b.mp4"): 20.0}
    text = json.dumps(
        [
            {"file": "b.mp4", "keep": True, "start": 0.0, "end": 15.0},
            {"file": "a.mp4", "keep": True, "start": 1.0, "end": 5.0},
        ]
    )
    decisions = parse_narrative_response(text, candidates, durations, transition_style="crossfade")
    assert decisions is not None
    assert [d.source_path.name for d in decisions] == ["b.mp4", "a.mp4"]
    assert decisions[0].order == 0 and decisions[0].transition_in is None
    assert decisions[1].order == 1 and decisions[1].transition_in == "crossfade"


def test_parse_malformed_json_returns_none():
    assert parse_narrative_response("not json", [], {}, transition_style=None) is None


def test_parse_non_list_returns_none():
    assert parse_narrative_response(json.dumps({"foo": "bar"}), [], {}, transition_style=None) is None


def test_parse_skips_unknown_filenames():
    candidates = [Path("a.mp4")]
    durations = {Path("a.mp4"): 10.0}
    text = json.dumps(
        [
            {"file": "ghost.mp4", "keep": True, "start": 0.0, "end": 5.0},
            {"file": "a.mp4", "keep": True, "start": 0.0, "end": 5.0},
        ]
    )
    decisions = parse_narrative_response(text, candidates, durations, transition_style=None)
    assert decisions is not None
    assert len(decisions) == 1
    assert decisions[0].source_path.name == "a.mp4"


def test_parse_clamps_out_of_range_start_end():
    candidates = [Path("a.mp4")]
    durations = {Path("a.mp4"): 10.0}
    text = json.dumps([{"file": "a.mp4", "keep": True, "start": -5.0, "end": 999.0}])
    decisions = parse_narrative_response(text, candidates, durations, transition_style=None)
    assert decisions is not None
    assert decisions[0].start == 0.0
    assert decisions[0].end == 10.0


def test_parse_degenerate_span_falls_back_to_full_duration():
    candidates = [Path("a.mp4")]
    durations = {Path("a.mp4"): 10.0}
    text = json.dumps([{"file": "a.mp4", "keep": True, "start": 8.0, "end": 2.0}])
    decisions = parse_narrative_response(text, candidates, durations, transition_style=None)
    assert decisions is not None
    assert (decisions[0].start, decisions[0].end) == (0.0, 10.0)


def test_parse_duplicate_filename_keeps_first_occurrence():
    candidates = [Path("a.mp4")]
    durations = {Path("a.mp4"): 10.0}
    text = json.dumps(
        [
            {"file": "a.mp4", "keep": True, "start": 0.0, "end": 3.0},
            {"file": "a.mp4", "keep": True, "start": 5.0, "end": 8.0},
        ]
    )
    decisions = parse_narrative_response(text, candidates, durations, transition_style=None)
    assert decisions is not None
    assert len(decisions) == 1
    assert (decisions[0].start, decisions[0].end) == (0.0, 3.0)


def test_parse_all_dropped_returns_none():
    candidates = [Path("a.mp4")]
    durations = {Path("a.mp4"): 10.0}
    text = json.dumps([{"file": "a.mp4", "keep": False}])
    assert parse_narrative_response(text, candidates, durations, transition_style=None) is None
