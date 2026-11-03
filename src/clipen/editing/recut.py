"""Map existing post-production through local cuts instead of clearing it all."""
from dataclasses import replace
from fractions import Fraction

from clipen.domain import EffectRequest, TextClip


def remap_cut(tools, items):
    from .production_media import load_text_styles, atomic_json, text_style_path
    from clipen.finishing.models import digest
    timeline = tools._timeline()
    old = list(tools._video().items)
    old_by_id = {item.item_id: item for item in old}
    fps = Fraction(timeline.frame_rate.numerator, timeline.frame_rate.denominator)
    cursor, clips, invalidated = 0, [], []
    for index, item in enumerate(items):
        previous = old_by_id.get(item.item_id)
        effects = list(previous.effects if previous else item.effects)
        changed = previous is not None and previous.source != item.source
        if changed:
            # Clip-local masks and transitions require explicit review if the
            # visible source changes. Preserve source treatment and music.
            unsafe = {"video.mosaic", "video.transition", "video.fade", "video.speed_ramp"}
            invalidated += [{"item_id": item.item_id, "effect_id": e.effect_id,
                             "reason": "changed_source_requires_review"} for e in effects if e.effect_id in unsafe]
            effects = [e for e in effects if e.effect_id not in unsafe]
        if previous and any(e.effect_id == "video.transition" for e in effects):
            before_old = old[old.index(previous)-1].item_id if old.index(previous) else None
            before_new = items[index-1].item_id if index else None
            if before_old != before_new:
                effects = [e for e in effects if e.effect_id != "video.transition"]
                invalidated.append({"item_id": item.item_id, "effect_id": "video.transition", "reason": "cut_changed"})
        clips.append(replace(item, start_frame=cursor, effects=tuple(effects)))
        cursor += item.duration_frames
    for n, clip in enumerate(clips):
        effects = []
        for effect in clip.effects:
            if effect.effect_id != "audio.layers":
                effects.append(effect)
                continue
            layers = []
            for layer in effect.parameters["layers"]:
                remaining = cursor-clip.start_frame-layer["start_frame"]
                if remaining <= 0:
                    invalidated.append({"layer_id": layer["layer_id"], "reason": "anchor_beyond_end"})
                else:
                    layers.append({**layer, "duration_frames": min(layer["duration_frames"], remaining)})
            if layers:
                effects.append(EffectRequest("audio.layers", {"layers": layers}))
        clips[n] = replace(clip, effects=tuple(effects))
    styles = load_text_styles(tools.repository.projects, tools.project_id)
    styles_changed = False
    retained_tracks = [replace(tools._video(), items=tuple(clips))]
    for track in timeline.tracks:
        if track is tools._video() or track.track_id == tools._video().track_id:
            continue
        mapped = []
        for element in track.items:
            if not isinstance(element, TextClip):
                invalidated.append({"item_id": element.item_id, "reason": "independent_track_needs_remapping"})
                continue
            profile = styles.get(element.style_id, {})
            anchor = next((i for i in old if i.item_id == profile.get("anchor_item_id")), None)
            if anchor is None:
                anchor = next((i for i in old if i.start_frame <= element.start_frame
                               and element.start_frame+element.duration_frames <= i.start_frame+i.duration_frames), None)
            if anchor is None:
                invalidated.append({"item_id": element.item_id, "reason": "no_source_anchor"})
                continue
            span = anchor.source.span
            unit = Fraction(span.time_base.numerator, span.time_base.denominator)
            left = int(span.start_pts)*unit+Fraction(element.start_frame-anchor.start_frame, 1)/fps
            right = left+Fraction(element.duration_frames, 1)/fps
            # Prefer stable clip identity; split children can carry an element
            # wholly contained in their source interval. Never duplicate it.
            candidates = sorted(clips, key=lambda i: i.item_id != anchor.item_id)
            target = next((i for i in candidates if i.source.span.version_id == span.version_id
                           and i.source.span.stream_index == span.stream_index
                           and (i.item_id == anchor.item_id or i.item_id not in old_by_id)
                           and int(i.source.span.start_pts)*unit <= left
                           and right <= int(i.source.span.end_pts)*unit), None)
            if target is None:
                invalidated.append({"item_id": element.item_id, "reason": "source_removed_or_trimmed"})
                continue
            offset = round((left-int(target.source.span.start_pts)*unit)*fps)
            style_id = element.style_id
            if profile.get("kind") in {"program_component","program_subtitle"}:
                updated = {**profile, "anchor_item_id": target.item_id}
                if profile['kind']=='program_component':updated['start_offset_frames']=offset
                style_id = ("agent.component." if profile['kind']=='program_component' else 'agent.text.')+digest(updated)[:24]
                styles[style_id] = updated
                styles_changed = True
            mapped.append(replace(element, start_frame=target.start_frame+offset, style_id=style_id))
        if mapped:
            retained_tracks.append(replace(track, items=tuple(mapped)))
    if styles_changed:
        atomic_json(text_style_path(tools.repository.projects, tools.project_id), styles)
    return replace(timeline, duration_frames=cursor, tracks=tuple(retained_tracks), markers=()), invalidated
