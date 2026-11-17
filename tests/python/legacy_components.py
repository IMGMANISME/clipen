"""Graphics made with the retired built-in templates, as older films still carry them.

The AI no longer makes these, but Clipen keeps drawing, editing, recutting and
exporting them, so tests place one directly the way the old tool stored it.
"""

from dataclasses import replace

from clipen.domain import TextClip
from clipen.editing.production_media import atomic_json, load_text_styles, text_style_path
from clipen.finishing.models import digest
from clipen.skills.visual_styles import font_presets, style_presets


def place(
    tools,
    *,
    element_id,
    item_id,
    component_id,
    start_offset_frames,
    duration_frames,
    text,
    detail="",
    evidence="",
    x=None,
    y=None,
    target=None,
    visual_style_id=None,
    font_style_id=None,
    summary="舊樣板元件",
):
    skill = tools._skill_value()
    component = next(c for c in skill["components"] if c["component_id"] == component_id)
    shot = next(i for i in tools._video().items if i.item_id == item_id)
    visual_style, presets = style_presets(skill, visual_style_id)
    font_style, presets = font_presets(skill, presets, font_style_id)
    spot = presets.get("component_positions", {}).get(component_id, component["default_position"])
    profile = {
        "kind": "program_component",
        "component_id": component_id,
        "skill_id": skill["skill_id"],
        "skill_version": skill["version"],
        "skill_hash": skill["content_hash"],
        "presets": presets,
        "visual_style_id": visual_style,
        "font_style_id": font_style,
        "eyebrow": component.get("eyebrow", ""),
        "anchor_item_id": shot.item_id,
        "start_offset_frames": start_offset_frames,
        "evidence": evidence,
        "x": spot["x"] if x is None else x,
        "y": spot["y"] if y is None else y,
        **({"target": target} if target else {}),
    }
    item = TextClip(
        element_id,
        shot.start_frame + start_offset_frames,
        duration_frames,
        text + ("\n" + detail if detail else ""),
        "agent.component." + digest(profile)[:24],
    )
    styles = load_text_styles(tools.repository.projects, tools.project_id)
    styles[item.style_id] = profile
    atomic_json(text_style_path(tools.repository.projects, tools.project_id), styles)
    tools._expected_revision_id = None
    return tools._save(replace(tools._timeline(), tracks=tools._component_lanes(item)), summary, element_id=element_id)
