"""Whole-project narrative assembly: given a pile of unrelated raw clips,
ask the LLM to decide which to keep, what order to play them in, and
roughly which sub-span of each is worth keeping. This is the only
editorial decision clipen makes -- exactly one narrative LLM call per
edit, honestly documented as the one place prompt size scales with clip
count (unlike every other stage in this pipeline, which is free).

Take-grouping convention (documented, not inferred): source files sharing
a filename stem after stripping a trailing "-take<N>"/"_take<N>"/"-v<N>"/
"_v<N>" suffix are treated as alternate recordings of the same moment,
e.g. "intro-take1.mp4" and "intro-take2.mp4" both group under "intro". A
group's representative is chosen locally by loudest average level -- no
extra LLM call -- purely to avoid wasting prompt budget on near-duplicate
footage and to avoid visible repeats in the final cut.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from clipen.analysis.asr import TranscriptSegment, transcribe
from clipen.analysis.audio_energy import segment_rms_db
from clipen.analysis.shots import detect_shots
from clipen.analysis.silence import SilenceSpan, detect_silence
from clipen.analysis.visual import describe_clip_visually
from clipen.editing.models import EditDecision
from clipen.llm.base import LLMClient, LLMMessage
from clipen.utils.media import probe_duration

PROMPT_CHAR_BUDGET = 20_000
MIN_CLIP_CHAR_BUDGET = 300
_MIN_SAMPLED_SEGMENTS = 3

_TAKE_SUFFIX_RE = re.compile(r"[-_](?:take|v)\d+$", re.IGNORECASE)
_LEADING_TRAILING_TOLERANCE = 0.05  # seconds

_SYSTEM_PROMPT = (
    "You are assembling an ordered vlog edit from a pile of unrelated raw "
    "video clips. For each clip, decide whether it's worth keeping, what "
    "position it should occupy in the final edit, and roughly which time "
    "range within it is worth keeping. Base your judgment on the "
    "transcript, audio level, and (if present) a short visual description "
    "for each clip below -- prefer clips with clear, complete thoughts and "
    "natural pacing; drop redundant, low-value, or generic footage.\n\n"
    "Respond with a JSON array only, ordered exactly as you want the final "
    "edit to play, one object per clip you are keeping: "
    '{"file": "<filename>", "keep": true, "start": <seconds>, '
    '"end": <seconds>, "reason": "<short reason>"}. For clips you are '
    'dropping, use {"file": "<filename>", "keep": false, "reason": '
    '"<short reason>"}.'
)


def shot_group_key(path: Path) -> str:
    return _TAKE_SUFFIX_RE.sub("", path.stem)


def trim_silence(
    duration: float,
    silence_spans: list[SilenceSpan],
    *,
    min_kept_clip_duration: float = 0.0,
) -> tuple[float, float]:
    """Trim only leading/trailing silence -- mid-clip silence is left
    alone; the narrative LLM call itself decides which sub-range of a
    clip is worth keeping, using content it can see that this local
    heuristic can't."""
    start = 0.0
    end = duration
    for span in silence_spans:
        if span["start"] <= _LEADING_TRAILING_TOLERANCE:
            start = max(start, span["end"])
    for span in reversed(silence_spans):
        if span["end"] >= duration - _LEADING_TRAILING_TOLERANCE:
            end = min(end, span["start"])
    if end <= start or end - start < min_kept_clip_duration:
        return 0.0, duration
    return start, end


def summarize_clip_for_narrative(
    path: Path,
    transcript: list[TranscriptSegment],
    trimmed_span: tuple[float, float],
    duration: float,
    rms_db: float,
    visual_summary: str | None,
    *,
    char_budget: int,
) -> str:
    start, end = trimmed_span
    header = (
        f"### {path.name}\n"
        f"duration={duration:.1f}s trimmed=[{start:.1f}s-{end:.1f}s] avg_rms_db={rms_db:.1f}"
    )
    if visual_summary:
        header += f"\n  visual: {visual_summary}"

    relevant = [seg for seg in transcript if seg["end"] > start and seg["start"] < end]
    lines = [f'  [{seg["start"]:.1f}s-{seg["end"]:.1f}s] "{seg["text"][:200]}"' for seg in relevant]

    def assemble(selected_lines: list[str], omitted: int) -> str:
        block = header + ("\n" + "\n".join(selected_lines) if selected_lines else "")
        if omitted > 0:
            block += f"\n  ({omitted} segments omitted between shown excerpts)"
        return block

    full = assemble(lines, 0)
    if len(full) <= char_budget or len(lines) <= _MIN_SAMPLED_SEGMENTS:
        return full

    keep_n = min(12, len(lines))
    while keep_n > _MIN_SAMPLED_SEGMENTS:
        step = (len(lines) - 1) / (keep_n - 1)
        indices = sorted({round(i * step) for i in range(keep_n)})
        candidate = assemble([lines[i] for i in indices], len(lines) - len(indices))
        if len(candidate) <= char_budget:
            return candidate
        keep_n -= 1

    indices = sorted({0, len(lines) - 1})
    return assemble([lines[i] for i in indices], len(lines) - len(indices))


def build_narrative_prompt(
    clip_summaries: list[tuple[Path, str]],
    *,
    instructions: str = "",
    target_duration_seconds: float | None = None,
) -> list[LLMMessage]:
    system = _SYSTEM_PROMPT
    if instructions:
        system += f"\n\nStyle guidance for this project:\n{instructions}"
    if target_duration_seconds:
        system += (
            f"\n\nTarget total runtime: approximately {target_duration_seconds:.0f}s. "
            "Treat this as a soft goal, not a hard constraint: prefer good "
            "pacing and complete thoughts over forcing an exact runtime. Do "
            "not pad with low-quality footage just to reach it, and do not "
            "cut mid-sentence just to come in under it."
        )
    user = "\n\n".join(summary for _, summary in clip_summaries)
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=user),
    ]


def parse_narrative_response(
    text: str,
    candidates: list[Path],
    durations: dict[Path, float],
    *,
    transition_style: str | None = None,
) -> list[EditDecision] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None

    name_to_path = {p.name: p for p in candidates}
    seen: set[Path] = set()
    kept: list[tuple[Path, float, float]] = []

    for item in data:
        if not isinstance(item, dict):
            continue
        path = name_to_path.get(item.get("file"))
        if path is None or path in seen:
            continue
        if item.get("keep", True) is False:
            continue
        seen.add(path)

        duration = durations[path]
        try:
            start = float(item.get("start", 0.0))
        except (TypeError, ValueError):
            start = 0.0
        try:
            end = float(item.get("end", duration))
        except (TypeError, ValueError):
            end = duration
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        if end <= start:
            start, end = 0.0, duration
        kept.append((path, start, end))

    if not kept:
        return None

    decisions = []
    for order, (path, start, end) in enumerate(kept):
        transition_in = transition_style if order > 0 else None
        decisions.append(
            EditDecision(source_path=path, start=start, end=end, order=order, transition_in=transition_in)
        )
    return decisions


def _local_take_winner(candidates: list[Path]) -> Path:
    if len(candidates) == 1:
        return candidates[0]
    return max(candidates, key=lambda p: segment_rms_db(p, 0.0, probe_duration(p)))


def build_narrative_edit_decisions(
    source_paths: list[Path],
    llm_client: LLMClient,
    *,
    vision_client: LLMClient | None = None,
    vision_model_tag: str = "vision",
    instructions: str = "",
    target_duration_seconds: float | None = None,
    transition_style: str | None = None,
) -> list[EditDecision]:
    groups: dict[str, list[Path]] = {}
    group_order: list[str] = []
    for path in source_paths:
        key = shot_group_key(path)
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(path)

    winners = [_local_take_winner(groups[key]) for key in group_order]

    durations: dict[Path, float] = {path: probe_duration(path) for path in winners}
    trimmed_spans: dict[Path, tuple[float, float]] = {}
    transcripts: dict[Path, list[TranscriptSegment]] = {}
    visual_summaries: dict[Path, str] = {}

    for path in winners:
        silence_spans = detect_silence(path)
        trimmed_spans[path] = trim_silence(durations[path], silence_spans)
        transcripts[path] = transcribe(path)
        if vision_client is not None:
            shots = detect_shots(path)
            visual_summaries[path] = describe_clip_visually(
                path, shots, vision_client, model_tag=vision_model_tag
            )

    per_clip_budget = max(MIN_CLIP_CHAR_BUDGET, PROMPT_CHAR_BUDGET // max(1, len(winners)))
    clip_summaries: list[tuple[Path, str]] = []
    for path in winners:
        rms = segment_rms_db(path, *trimmed_spans[path])
        summary = summarize_clip_for_narrative(
            path,
            transcripts[path],
            trimmed_spans[path],
            durations[path],
            rms,
            visual_summaries.get(path),
            char_budget=per_clip_budget,
        )
        clip_summaries.append((path, summary))

    messages = build_narrative_prompt(
        clip_summaries,
        instructions=instructions,
        target_duration_seconds=target_duration_seconds,
    )
    response = llm_client.complete(
        messages, max_tokens=max(1024, 150 * len(winners)), response_format="json"
    )
    decisions = parse_narrative_response(
        response.text, winners, durations, transition_style=transition_style
    )
    if decisions is not None:
        return decisions

    fallback: list[EditDecision] = []
    for order, path in enumerate(winners):
        transition_in = transition_style if order > 0 else None
        fallback.append(
            EditDecision(
                source_path=path,
                start=0.0,
                end=durations[path],
                order=order,
                transition_in=transition_in,
            )
        )
    return fallback
