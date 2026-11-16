"""Content checks that keep programme graphics tied to a moment, not a template."""

import re
import unicodedata
from collections import Counter

# Short reactions land on one beat; a long hold reads as a static label.
RESTATE_RATIO = 0.5
SAME_POSITION_DISTANCE = 0.03
SAME_POSITION_RUN = 3
UNIFORM_DURATION_SHARE = 0.5
UNIFORM_DURATION_MINIMUM = 4
_PARALLEL = re.compile(r"^[^，,・]{2,8}[，,][^，,・]{2,8}$")


def significant(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c)[0] not in "PZCS")


def _bigrams(text):
    text = significant(text)
    return [text[n : n + 2] for n in range(len(text) - 1)]


def restated_ratio(text, subtitles):
    """Share of the graphic's character pairs copied from concurrent subtitles."""
    pairs = _bigrams(text)
    spoken = significant("".join(subtitles))
    return sum(pair in spoken for pair in pairs) / len(pairs) if pairs else 0.0


def concurrent_subtitles(timeline, start, duration):
    end = start + duration
    return [
        i.text
        for t in timeline.tracks
        if t.track_id == "agent-subtitles"
        for i in t.items
        if i.start_frame < end and start < i.start_frame + i.duration_frames
    ]


def design_notes(timeline, elements, styles):
    """Advisory findings; heuristics never block delivery on their own."""
    fps = timeline.frame_rate.numerator / timeline.frame_rate.denominator
    ordered = sorted(elements, key=lambda i: (i.start_frame, i.item_id))
    notes = []
    run = []
    for element in ordered:
        profile = styles.get(element.style_id, {})
        point = (profile.get("x"), profile.get("y"))
        if run and None not in point and all(abs(a - b) <= SAME_POSITION_DISTANCE for a, b in zip(point, run[-1][1])):
            run.append((element.item_id, point))
        else:
            if len(run) >= SAME_POSITION_RUN:
                notes.append({"code": "component_position_repeated", "item_ids": [i for i, _ in run]})
            run = [(element.item_id, point)] if None not in point else []
    if len(run) >= SAME_POSITION_RUN:
        notes.append({"code": "component_position_repeated", "item_ids": [i for i, _ in run]})
    if len(ordered) >= UNIFORM_DURATION_MINIMUM:
        duration, count = Counter(round(i.duration_frames / fps * 2) / 2 for i in ordered).most_common(1)[0]
        if count / len(ordered) > UNIFORM_DURATION_SHARE:
            notes.append({"code": "component_duration_uniform", "seconds": duration, "count": count})
    for element in ordered:
        kind = styles.get(element.style_id, {}).get("component_id")
        primary = element.text.partition("\n")[0]
        if kind not in {"program_title"} and _PARALLEL.match(primary):
            notes.append({"code": "component_parallel_phrasing", "item_id": element.item_id, "text": primary})
    messages = {
        "component_position_repeated": "連續多個元件擺在同一位置。請依每個畫面的人物、物件與空白處重新決定位置。",
        "component_duration_uniform": "多數元件的顯示時長相同，節奏像固定節拍。請依每個瞬間的閱讀需要調整。",
        "component_parallel_phrasing": "「A，B」對句常只是描述畫面。請改成畫面看不出的資訊或觀點，或移除這個元件。",
    }
    return [{**note, "message": messages[note["code"]]} for note in notes]
