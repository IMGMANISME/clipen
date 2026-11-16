"""Shot coverage and revision-bound, explicitly model-authored editorial findings.

Coverage proves which stills were delivered, never that motion or sound was
understood. Findings expose the model's decisions rather than inventing a score.
"""

import hashlib
import json
from fractions import Fraction

from clipen.domain import EditTimeline, TrackKind
from clipen.media import MediaPreviewRepository
from clipen.projects import ProjectError

from .agent_models import ToolResult
from .production_media import sample_frames
from .production_preview import _event, inspect_preview


def shot_offsets(item, fps):
    # First/last real frames, not the following shot at the outgoing boundary.
    return [
        item.start_frame / fps,
        (item.start_frame + (item.duration_frames - 1) / 2) / fps,
        (item.start_frame + item.duration_frames - 1) / fps,
    ]


def _effects(item):
    return [(e.effect_id, e.version, e._parameters_json) for e in item.effects]


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def shot_fingerprints(timeline):
    """What each shot looks like on screen, independent of where it sits in the film.

    Covers the source range, length, the shot's own effects, the next shot's
    incoming transition and every text or component drawn over it (relative to
    the shot). A shot with the same fingerprint renders the same stills.
    """
    video = [t for t in timeline.tracks if t.kind is TrackKind.VIDEO]
    clips = [item for track in video for item in track.items]
    texts = [item for t in timeline.tracks if t.kind is TrackKind.TITLE for item in t.items]
    result = {}
    for index, clip in enumerate(clips):
        end = clip.start_frame + clip.duration_frames
        following = clips[index + 1] if index + 1 < len(clips) else None
        over = sorted(
            (t.text, t.style_id, t.start_frame - clip.start_frame, t.duration_frames)
            for t in texts
            if t.start_frame < end and clip.start_frame < t.start_frame + t.duration_frames
        )
        result[clip.item_id] = _fingerprint(
            {
                "source": clip.source.span.to_storage(),
                "duration": clip.duration_frames,
                "effects": _effects(clip),
                "next_transition": [e for e in _effects(following) if e[0] == "video.transition"]
                if following
                else None,
                "texts": over,
                "canvas": [
                    timeline.width,
                    timeline.height,
                    timeline.frame_rate.numerator,
                    timeline.frame_rate.denominator,
                ],
            }
        )
    return result


def text_fingerprints(timeline):
    """A text matches an earlier review when it and the shot under its midpoint are unchanged."""
    shots = shot_fingerprints(timeline)
    clips = [item for t in timeline.tracks if t.kind is TrackKind.VIDEO for item in t.items]
    result = {}
    for t in timeline.tracks:
        if t.kind is not TrackKind.TITLE:
            continue
        for item in t.items:
            middle = item.start_frame + (item.duration_frames - 1) / 2
            under = next((c for c in clips if c.start_frame <= middle < c.start_frame + c.duration_frames), None)
            result[item.item_id] = _fingerprint(
                {
                    "text": item.text,
                    "style": item.style_id,
                    "duration": item.duration_frames,
                    "offset": None if under is None else middle - under.start_frame,
                    "shot": None if under is None else shots[under.item_id],
                }
            )
    return result


def carried_reviews(tools, revision, recipe_id):
    """Shots and texts already reviewed and accepted in an earlier revision of this run, unchanged since.

    Only revisions with a recorded editorial review count: that review required
    complete coverage, so an accepted, unchanged shot need not be looked at again.
    """
    with tools.repository.projects.connect(tools.project_id) as db:
        reports = {}
        for row in db.execute(
            "SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='editorial_review' ORDER BY sequence",
            (tools.run_id,),
        ):
            report = json.loads(row["payload_json"])
            if report.get("recipe_id") == recipe_id and report["revision_id"] != revision.revision_id:
                reports[report["revision_id"]] = report
        payloads = (
            {
                row["revision_id"]: row["payload_json"]
                for row in db.execute(
                    f"SELECT revision_id,payload_json FROM edit_agent_revisions WHERE run_id=? AND revision_id IN ({','.join('?' for _ in reports)})",
                    (tools.run_id, *reports),
                )
            }
            if reports
            else {}
        )
    accepted_shots, accepted_texts = set(), set()
    for revision_id, report in reports.items():
        if revision_id not in payloads:
            continue
        earlier = EditTimeline.from_payload(json.loads(payloads[revision_id]))
        shots, texts = shot_fingerprints(earlier), text_fingerprints(earlier)
        accepted_shots |= {
            shots[s["item_id"]]
            for s in report.get("shots", [])
            if s.get("decision") != "revise" and s["item_id"] in shots
        }
        accepted_texts |= {
            texts[c["item_id"]] for c in report.get("text_checks", []) if c.get("matches") and c["item_id"] in texts
        }
        accepted_shots |= set(report.get("carried_shots", []))
        accepted_texts |= set(report.get("carried_texts", []))
    current_shots, current_texts = shot_fingerprints(revision.timeline), text_fingerprints(revision.timeline)
    return (
        {item: print_ for item, print_ in current_shots.items() if print_ in accepted_shots},
        {item: print_ for item, print_ in current_texts.items() if print_ in accepted_texts},
    )


def coverage_targets(timeline):
    fps = timeline.frame_rate.numerator / timeline.frame_rate.denominator
    return {
        item.item_id: shot_offsets(item, fps) for t in timeline.tracks if t.kind is TrackKind.VIDEO for item in t.items
    }


def text_targets(timeline):
    fps = timeline.frame_rate.numerator / timeline.frame_rate.denominator
    return {
        item.item_id: (item.start_frame + (item.duration_frames - 1) / 2) / fps
        for t in timeline.tracks
        if t.kind is TrackKind.TITLE
        for item in t.items
    }


def inspect_cut(tools, args):
    revision = tools.repository.current_revision(tools.project_id, tools.run_id)
    timeline = revision.timeline
    targets = coverage_targets(timeline)
    clips = {i.item_id: i for i in tools._video().items}
    if any(i not in clips for i in args["item_ids"]):
        raise ProjectError("timeline_item_not_found", "指定鏡頭不在目前剪輯。", status_code=404)
    fps = Fraction(timeline.frame_rate.numerator, timeline.frame_rate.denominator)
    speech = tools._production_list_transcripts({"include_drafts": True}).payload
    cues, limitations = speech["cues"], speech["limitations"]
    images = []
    shots = []
    for identifier in args["item_ids"]:
        item = clips[identifier]
        texts = [
            i
            for t in timeline.tracks
            if t.kind is TrackKind.TITLE
            for i in t.items
            if i.start_frame < item.start_frame + item.duration_frames
            and item.start_frame < i.start_frame + i.duration_frames
        ]
        if args.get("render_id"):
            offsets = targets[identifier] + [text_targets(timeline)[i.item_id] for i in texts[:1]]
            # Text can span shots; its midpoint must still be inspected, once.
            offsets = sorted(set(offsets))
            for start in range(0, len(offsets), 8):
                result = inspect_preview(
                    tools, {"render_id": args["render_id"], "offsets_seconds": offsets[start : start + 8]}
                )
                if result.payload["render"]["state"] != "succeeded" or result.payload["stale"]:
                    return ToolResult(result.payload)
                images.extend(result.images)
            samples = [{"timeline_seconds": o} for o in offsets]
        else:
            span = item.source.span
            unit = Fraction(span.time_base.numerator, span.time_base.denominator)
            duration = span.duration_pts * unit
            last = min(Fraction(item.duration_frames - 1, 1) / fps, duration - Fraction(1, 1) / fps)
            offsets = [0, float(max(0, last) / 2), float(max(0, last))]
            media = getattr(tools, "media", None) or MediaPreviewRepository(tools.repository.projects)
            paths, samples = sample_frames(
                media, tools.project_id, span, offsets, tools._workdir("samples"), tools._cancel_event()
            )
            images.extend(paths)
        shots.append(
            {
                "item_id": identifier,
                "source": item.source.span.to_storage(),
                "samples": samples,
                "image_count": len(samples),
                "texts": [
                    {
                        "item_id": i.item_id,
                        "text": i.text,
                        "start_frame": i.start_frame,
                        "duration_frames": i.duration_frames,
                    }
                    for i in texts
                ],
                "dialogue": [c for c in cues if c["item_id"] == identifier],
                "dialogue_problems": [c for c in limitations if c["item_id"] == identifier],
            }
        )
    from .programme_review import pacing

    return ToolResult(
        {
            "revision_id": revision.revision_id,
            "shots": shots,
            "pacing": pacing(timeline),
            "image_order": "shots in requested order; frames in sample order",
            "coverage": "start/middle/end stills; continuous motion and audio remain unreviewed",
            "text_coverage": "At most one additional text midpoint per shot; validate_sequence lists remaining text samples for inspect_preview.",
            "editorial_questions": [
                "Does this show the event or only preparation/empty wall?",
                "Does the scene establish a place and then deliver action/reaction?",
                "Is each location/day assertion supported by this exact shot?",
                "Does retained speech finish naturally? Repair source spans before captions.",
            ],
        },
        tuple(images),
    )


def review_cut(tools, args):
    from .production_preview import finish_review_status

    revision = tools.repository.current_revision(tools.project_id, tools.run_id)
    status = finish_review_status(tools)
    if not status["render_coverage_complete"]:
        raise ProjectError(
            "agent_preview_review_required", "請先檢視每個鏡頭的頭、中、尾及所有文字顯示畫面。", status_code=409
        )
    every_shot = set(coverage_targets(revision.timeline))
    every_text = set(text_targets(revision.timeline))
    carried_shots, carried_texts = set(status.get("carried_shots", ())), set(status.get("carried_texts", ()))
    shots = args["shots"]
    checks = args["text_checks"]
    listed_shots = [s["item_id"] for s in shots]
    listed_texts = [c["item_id"] for c in checks]
    # Every changed shot and text must be judged; unchanged, already accepted ones may be listed or left out.
    if (
        len(listed_shots) != len(set(listed_shots))
        or not every_shot - carried_shots <= set(listed_shots) <= every_shot
        or len(listed_texts) != len(set(listed_texts))
        or not every_text - carried_texts <= set(listed_texts) <= every_text
    ):
        raise ProjectError(
            "agent_review_incomplete",
            "審片紀錄必須逐一列出所有改過的鏡頭與文字，不可重複或遺漏；沒改過且已通過的可以省略。",
            status_code=422,
        )
    if status["unresolved"]:
        still_unresolved = {s["item_id"] for s in shots if s["decision"] == "revise"} | {
            c["item_id"] for c in checks if not c["matches"]
        }
        if not set(status["unresolved"]) <= still_unresolved:
            raise ProjectError(
                "agent_revision_required",
                "這個版本已記錄待修問題；請先實際修改時間軸，再輸出並檢視新版，不能只改評語清除問題。",
                status_code=409,
            )
    from .production_preview import render_recipe_id

    shot_prints, text_prints = shot_fingerprints(revision.timeline), text_fingerprints(revision.timeline)
    report = {
        "revision_id": revision.revision_id,
        "recipe_id": render_recipe_id(),
        "authority": "model_authored",
        **args,
        # Carried items keep their earlier acceptance for later revisions.
        "carried_shots": sorted(shot_prints[i] for i in carried_shots - set(listed_shots)),
        "carried_texts": sorted(text_prints[i] for i in carried_texts - set(listed_texts)),
    }
    _event(tools, "editorial_review", report)
    return ToolResult(
        {
            "assessment": report,
            "unresolved": [s["item_id"] for s in shots if s["decision"] == "revise"]
            + [c["item_id"] for c in checks if not c["matches"]],
            "human_review": "pending",
            "quality_claim": "This records model findings, not independent approval.",
        }
    )
