"""Validated word boundaries from an installed, pinned local model; no estimated words."""

import json
import math
import unicodedata
from pathlib import Path

from clipen.projects import ProjectError

MODEL = "mlx-community/Qwen3-ForcedAligner-0.6B-8bit"
REVISION = "0e1a68e91d815300c7c9754b2a7639378b23db15"
FILES = {
    "chat_template.json": 1161,
    "config.json": 6940,
    "generation_config.json": 115,
    "merges.txt": 1671853,
    "model.safetensors": 1271924386,
    "model.safetensors.index.json": 79108,
    "preprocessor_config.json": 330,
    "tokenizer_config.json": 12666,
    "vocab.json": 2776833,
}


def significant(text):
    return "".join(c.casefold() for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c)[0] not in "PZC")


# The pinned aligner reports times on an 80 ms grid. A zero-width token one
# step away from its neighbour still marks a boundary the model observed.
FRAME = 0.1
# A result where most characters collapse onto instants, or a run of them
# share one instant, means the model lost the speech; keep it for repair.
MAX_INSTANT_SHARE = 0.4
MAX_INSTANT_RUN = 4


def validate_alignment(words, text, duration):
    if not isinstance(words, list) or not 1 <= len(words) <= 1000:
        raise ProjectError("alignment_invalid", "對齊結果沒有有效字詞。", status_code=422)
    # Quantized aligners can emit a zero-width token at, or one grid step from,
    # an adjacent word boundary. Keep the observed interval as a grouped word;
    # never assign an invented duration to the individual character or bridge
    # a gap longer than one grid step. A token away from any neighbour stays an
    # instant: its observed time, with no duration added.
    grouped = []
    pending = []
    for raw in words:
        word = dict(raw)
        start, end = word.get("start"), word.get("end")
        if not significant(word.get("text", "")):
            continue
        finite = all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end))
        zero = finite and 0 <= start == end <= duration
        last = grouped[-1] if grouped else None
        if (
            zero
            and last
            and not pending
            and isinstance(last.get("end"), (int, float))
            and not isinstance(last.get("end"), bool)
            and 0 <= start - last["end"] <= FRAME
            and last.get("start", start) < start
        ):
            last["text"] += word["text"]
            last["end"] = start
        elif zero:
            pending.append(word)
        else:
            if pending and finite and start < end and all(0 <= start - w["end"] <= FRAME for w in pending):
                word["text"] = "".join(w["text"] for w in pending) + word["text"]
                word["start"] = min(start, pending[0]["start"])
            else:
                grouped.extend(pending)
            pending = []
            grouped.append(word)
    words = grouped + pending
    positions = [(index, significant(character)) for index, character in enumerate(text) if significant(character)]
    normalized = "".join(char for _, char in positions)
    # Require a stable one-to-one character normalization. Do not silently assign
    # fractional timing inside a word or fabricate missing/mismatched characters.
    if any(len(char) != 1 for _, char in positions):
        raise ProjectError("alignment_text_unsupported", "這段文字需要額外正規化核對。", status_code=422)
    cursor, previous, result = 0, 0.0, []
    for word in words:
        token = significant(word.get("text", ""))
        if not token:
            continue
        start, end = word.get("start"), word.get("end")
        if (
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (start, end))
            or not 0 <= start <= end <= duration + 0.05
            or start < previous - 0.02
            or end < previous
            or normalized[cursor : cursor + len(token)] != token
        ):
            raise ProjectError("alignment_invalid", "字詞對齊與文字或音訊範圍不符，已保留待修。", status_code=422)
        result.append(
            {
                "text": word["text"],
                "char_start": positions[cursor][0],
                "char_end": positions[cursor + len(token) - 1][0] + 1,
                "start_seconds": max(previous, start),
                "end_seconds": min(duration, end),
            }
        )
        cursor += len(token)
        previous = min(duration, end)
    if cursor != len(normalized) or not result:
        raise ProjectError("alignment_incomplete", "對齊未涵蓋完整文字。", status_code=422)
    instants = [w["start_seconds"] >= w["end_seconds"] for w in result]
    run = longest = 0
    for instant in instants:
        run = run + 1 if instant else 0
        longest = max(longest, run)
    if (
        sum(instants) > len(result) * MAX_INSTANT_SHARE
        or longest >= MAX_INSTANT_RUN
        or not any(not i for i in instants)
    ):
        raise ProjectError("alignment_invalid", "對齊結果多數字詞沒有時間長度，已保留待修。", status_code=422)
    return result


def run_alignment(data_root, audio, text, language, output, cancel):
    if data_root is None:
        raise ProjectError("alignment_unavailable", "本機字詞對齊環境尚未設定。", status_code=409)
    from .alignment_setup import root_for, verified_runtime

    binding = verified_runtime(root_for(data_root))
    if binding is None:
        raise ProjectError("alignment_unavailable", "缺少固定版本的本機字詞對齊環境。", status_code=409)
    python, model = binding
    from clipen.finishing.audio import run_process

    from .production_media import atomic_json

    request = output.with_suffix(".request.json")
    atomic_json(
        request, {"model": str(model), "audio": str(audio), "text": text, "language": language, "output": str(output)}
    )
    try:
        run_process(
            [str(python), str(Path(__file__).with_name("alignment_worker.py")), str(request)], cancel, timeout=180
        )
        if output.is_symlink() or not output.is_file() or output.stat().st_size > 1024 * 1024:
            raise ProjectError("alignment_invalid", "無法讀取對齊結果。", status_code=409)
        return json.loads(output.read_text())
    finally:
        request.unlink(missing_ok=True)
