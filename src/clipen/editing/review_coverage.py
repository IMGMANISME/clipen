"""Which texts and graphics of a draft the AI actually looked at.

An AI request can change many graphics at once (one look change restyles every
card) and still only inspect a few rendered stills. The workbench marks the rest
so a person knows where to look first. Only looks at this exact revision count,
plus the texts an editorial review carried over unchanged: a still of an earlier
revision may show a different look, so it says nothing about this one.
"""

from __future__ import annotations

import json

from clipen.domain import EditTimeline, TrackKind

from .editorial_review import text_fingerprints


def ai_review_coverage(db, run_id: str, timeline: EditTimeline, revision_id: str) -> dict:
    fps = timeline.frame_rate.numerator / timeline.frame_rate.denominator
    texts = [
        (item.item_id, item.start_frame, item.start_frame + item.duration_frames)
        for track in timeline.tracks
        if track.kind is TrackKind.TITLE
        for item in track.items
    ]
    prints = text_fingerprints(timeline)
    seen: set[str] = set()
    for row in db.execute(
        "SELECT event_type,payload_json FROM edit_agent_events WHERE run_id=? "
        "AND event_type IN ('production_review','editorial_review') ORDER BY sequence",
        (run_id,),
    ):
        payload = json.loads(row["payload_json"])
        if payload.get("revision_id") != revision_id:
            continue
        if row["event_type"] == "production_review":
            frames = [float(offset) * fps for offset in payload.get("offsets_seconds", [])]
            seen |= {item_id for item_id, start, end in texts if any(start <= frame < end for frame in frames)}
        else:
            seen |= {check["item_id"] for check in payload.get("text_checks", []) if "item_id" in check}
            carried = set(payload.get("carried_texts", []))
            seen |= {item_id for item_id, print_ in prints.items() if print_ in carried}
    return {
        "revision_id": revision_id,
        "total": len(texts),
        "unreviewed_item_ids": [item_id for item_id, _, _ in texts if item_id not in seen],
    }
