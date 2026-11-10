"""Read-only skill packages. Source footage and project facts never enter them.

A production skill is composed from three layers and frozen per session:

- the Clipen production core (``builtin/SKILL.md`` and ``builtin/references``),
  always read first and never overridden;
- the built-in design library (components, visual styles, fonts, previews)
  that the renderer can actually draw;
- a chain of instruction skills: a built-in genre or template skill, or a
  user's custom skill optionally extending one of them.

Every provider reads the same composition through the Clipen MCP tools.
"""
from copy import deepcopy
import base64
import hashlib
from importlib.resources import files
import json
from pathlib import Path

from clipen.projects import ProjectError


CORE_ID = "clipen-core"
BUILTIN_SKILL = "travel-program"
GENRE_SKILLS = ("travel-program",)
_CORE_PATHS = ("SKILL.md", "manifest.json", "presets.json", "components.json", "visual_styles.json",
               "font_styles.json", "font_sources.json", "templates.json",
               "references/font-chenyu-OFL.txt", "references/font-iansui-OFL.txt",
               "references/story.md", "references/editing.md", "references/captions.md",
               "references/sound.md", "references/graphics.md", "references/channel-design.md",
               "references/review.md")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _root():
    return files("clipen.skills").joinpath("builtin")


def _core():
    """The production core and the design library every skill shares."""
    root = _root()
    resources = {name: root.joinpath(name).read_text(encoding="utf-8") for name in _CORE_PATHS}
    components = json.loads(resources["components.json"])
    styles = json.loads(resources["visual_styles.json"])
    fonts = json.loads(resources["font_styles.json"])
    templates = json.loads(resources["templates.json"])
    samples = {t["sample_resource"]: {"mime_type": "video/mp4", "sha256": hashlib.sha256(
        root.joinpath(t["sample_resource"]).read_bytes()).hexdigest()} for t in templates}
    assets = {}
    names = [s["preview_resource"] for s in styles]
    for name in names:
        data = root.joinpath(name).read_bytes()
        assets[name] = {"mime_type": "image/png", "sha256": hashlib.sha256(data).hexdigest(),
                        "data": base64.b64encode(data).decode("ascii")}
    return {"manifest": json.loads(resources["manifest.json"]), "resources": resources, "assets": assets,
            "samples": samples, "components": components, "visual_styles": styles, "font_styles": fonts,
            "presets": json.loads(resources["presets.json"]), "templates": templates,
            "content_hash": _digest({"resources": resources, "assets": assets, "samples": samples})}


def _builtin_skills(core):
    """Built-in instruction skills: genres plus one standalone skill per template."""
    root = _root()
    skills = {}
    for skill_id in GENRE_SKILLS:
        folder = f"skills/{skill_id}/"
        resources = {folder + name: root.joinpath(folder + name).read_text(encoding="utf-8")
                     for name in ("SKILL.md", "manifest.json")}
        manifest = json.loads(resources[folder + "manifest.json"])
        skills[skill_id] = {**{k: manifest[k] for k in ("skill_id", "version", "name", "description")},
                            "kind": "genre", "resource": folder + "SKILL.md", "resources": resources,
                            "binding": {}, "content_hash": _digest(resources)}
    for template in core["templates"]:
        resource = template["skill_resource"]
        manifest_resource = resource.rsplit("/", 1)[0] + "/manifest.json"
        resources = {resource: root.joinpath(resource).read_text(encoding="utf-8"),
                     manifest_resource: root.joinpath(manifest_resource).read_text(encoding="utf-8")}
        binding = json.loads(resources[manifest_resource])
        style = next((s for s in core["visual_styles"] if s["visual_style_id"] == binding["visual_style_id"]), None)
        font = next((f for f in core["font_styles"] if f["font_style_id"] == binding["font_style_id"]), None)
        if (binding["template_id"] != template["template_id"] or binding["visual_style_id"] != template["visual_style_id"]
                or binding["font_style_id"] != template["font_style_id"] or style is None or font is None):
            raise ProjectError("template_skill_invalid", "模板的 Skill 與畫面設定不一致。", status_code=409)
        content_hash = _digest({"instructions": resources[resource], "manifest": binding, "style": style, "font": font})
        template["skill"] = {**binding, "resource": resource, "content_hash": content_hash}
        skills[binding["skill_id"]] = {
            "skill_id": binding["skill_id"], "version": binding["version"], "name": template["name"],
            "description": template["description"], "kind": "template", "resource": resource,
            "resources": resources, "content_hash": content_hash, "template": template,
            "binding": {"visual_style_id": binding["visual_style_id"], "font_style_id": binding["font_style_id"]}}
    return skills


def compose(chain, *, core=None, binding=None, assets=None, source="builtin"):
    """Freeze the core, design library and an instruction chain into one skill.

    The last chain entry names the skill; the default SKILL.md read returns the
    core followed by every chain entry in order.
    """
    core = deepcopy(core or _core())
    selected = chain[-1]
    resources = core["resources"]
    for entry in chain:
        resources.update(entry["resources"])
    merged_assets = {**core["assets"], **(assets or {})}
    binding = {**{k: v for e in chain for k, v in e.get("binding", {}).items()}, **(binding or {})}
    skill = {
        **{k: v for k, v in core["manifest"].items() if k not in {"skill_id", "version", "name", "description"}},
        "skill_id": selected["skill_id"], "version": selected["version"], "name": selected["name"],
        "description": selected["description"], "source": source, "kind": selected.get("kind", "custom"),
        "core": {"skill_id": CORE_ID, "version": core["manifest"]["version"], "content_hash": core["content_hash"]},
        "skill_chain": [{k: e[k] for k in ("skill_id", "version", "resource", "content_hash")}
                        | {"source": e.get("source", "builtin")} for e in chain],
        "resources": resources, "assets": merged_assets, "samples": core["samples"],
        "visual_styles": core["visual_styles"], "font_styles": core["font_styles"], "presets": core["presets"],
        "components": core["components"],
        # Only the travel genre offers the template gallery in the UI.
        "templates": core["templates"] if selected["skill_id"] == BUILTIN_SKILL else [],
        "selected_visual_style_id": binding.get("visual_style_id", "variety"),
        "selected_font_style_id": binding.get("font_style_id", "default"),
    }
    if "template" in selected:
        skill["template"] = deepcopy(selected["template"])
    if "components" in binding:
        allowed = set(binding["components"])
        skill["components"] = [c for c in skill["components"] if c["component_id"] in allowed]
    if "default_duration_seconds" in binding:
        skill["default_duration_seconds"] = binding["default_duration_seconds"]
    skill["content_hash"] = _digest({"core": core["content_hash"], "chain": skill["skill_chain"],
                                     "binding": binding, "assets": sorted(a["sha256"] for a in (assets or {}).values())})
    return skill


def builtin_skills():
    core = _core()
    return core, _builtin_skills(core)


def design_library(core=None):
    """Built-in design a custom skill may bind by id; renderers stay in Clipen."""
    core = core or _core()
    return {"visual_styles": tuple(s["visual_style_id"] for s in core["visual_styles"]),
            "font_styles": tuple(s["font_style_id"] for s in core["font_styles"]),
            "components": tuple(c["component_id"] for c in core["components"])}


def reserved_skill_ids(skills=None):
    skills = skills if skills is not None else builtin_skills()[1]
    return frozenset({CORE_ID, *skills})


_library = None


def configure_library(root):
    """Point the catalog at a skill library; the Controller and tests call this."""
    global _library
    from .library import SkillLibrary
    core, skills = builtin_skills()
    _library = SkillLibrary(Path(root), {**design_library(core), "extends": tuple(skills)}, reserved_skill_ids(skills))
    return _library


def skill_library():
    if _library is None:
        from clipen.bootstrap import clipen_data_root
        return configure_library(clipen_data_root() / "skills")
    return _library


def compose_custom_skill(package, core=None, skills=None):
    """A custom skill, optionally extending a built-in skill, over the core."""
    if core is None or skills is None:
        core, skills = builtin_skills()
    prefix = f"custom/{package.skill_id}/"
    entry = {"skill_id": package.skill_id, "version": package.version, "name": package.name,
             "description": package.description, "kind": "custom", "source": "custom",
             "resource": prefix + "SKILL.md", "content_hash": package.content_hash,
             "resources": {prefix + path: text for path, text in package.resources.items()}}
    extends = package.binding.get("extends")
    chain = ([skills[extends]] if extends else []) + [entry]
    binding = {k: v for k, v in package.binding.items() if k != "extends"}
    skill = compose(chain, core=core, binding=binding, source="custom",
                    assets={prefix + path: asset for path, asset in package.assets.items()})
    skill["custom_skill"] = package.summary()
    return skill


def get_skill(skill_id=BUILTIN_SKILL):
    core, skills = builtin_skills()
    if skill_id in skills:
        return compose([skills[skill_id]], core=core)
    return compose_custom_skill(skill_library().load(skill_id), core, skills)


def _public(skill):
    from .visual_styles import public_styles, public_font_styles
    return {**deepcopy({key: skill[key] for key in ("skill_id", "version", "name", "description", "content_hash")}),
            "source": skill.get("source", "builtin"), "kind": skill.get("kind", "genre"),
            "templates": deepcopy(skill["templates"]), "visual_styles": public_styles(skill), "font_styles": public_font_styles(skill),
            "selected_visual_style_id": skill.get("selected_visual_style_id"), "selected_font_style_id": skill.get("selected_font_style_id"),
            "chain": [e["skill_id"] for e in skill.get("skill_chain", [])],
            **({"default_duration_seconds": skill["default_duration_seconds"]} if "default_duration_seconds" in skill else {}),
            **({"custom": {k: skill["custom_skill"][k] for k in ("binding", "ignored")}} if "custom_skill" in skill else {})}


def list_skills():
    core, skills = builtin_skills()
    listed = [_public(compose([entry], core=core)) for entry in skills.values()]
    library = skill_library()
    for item in library.list():
        if item["state"] == "ready":
            listed.append(_public(compose_custom_skill(library.load(item["skill_id"]), core, skills)))
    return listed
