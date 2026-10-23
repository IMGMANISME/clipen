"""Read-only skill packages. Source footage and project facts never enter them."""
from copy import deepcopy
import base64
import hashlib
from importlib.resources import files
import json
from pathlib import Path

from clipen.projects import ProjectError


BUILTIN_SKILL = "travel-program"


def _builtin_skill(skill_id=BUILTIN_SKILL):
    if skill_id != BUILTIN_SKILL:
        raise ProjectError("skill_not_found", "找不到這套影片製作 Skill。", status_code=404)
    root = files("clipen.skills").joinpath(skill_id)
    paths = ("SKILL.md", "manifest.json", "presets.json", "components.json", "visual_styles.json", "font_styles.json", "font_sources.json", "templates.json",
             "references/font-chenyu-OFL.txt", "references/font-iansui-OFL.txt",
             "references/story.md", "references/editing.md", "references/captions.md",
             "references/sound.md", "references/graphics.md", "references/channel-design.md", "references/review.md")
    resources = {name: root.joinpath(name).read_text(encoding="utf-8") for name in paths}
    manifest = json.loads(resources["manifest.json"])
    components=json.loads(resources['components.json'])
    styles=json.loads(resources['visual_styles.json'])
    fonts=json.loads(resources['font_styles.json'])
    templates=json.loads(resources['templates.json'])
    template_skills={}
    for template in templates:
        resource=template['skill_resource']
        manifest_resource=resource.rsplit('/',1)[0]+'/manifest.json'
        resources[resource]=root.joinpath(resource).read_text(encoding='utf-8')
        resources[manifest_resource]=root.joinpath(manifest_resource).read_text(encoding='utf-8')
        binding=json.loads(resources[manifest_resource])
        style=next((s for s in styles if s['visual_style_id']==binding['visual_style_id']),None)
        font=next((f for f in fonts if f['font_style_id']==binding['font_style_id']),None)
        if (binding['template_id']!=template['template_id'] or
            binding['visual_style_id']!=template['visual_style_id'] or
            binding['font_style_id']!=template['font_style_id'] or style is None or font is None):
            raise ProjectError('template_skill_invalid','模板的 Skill 與畫面設定不一致。',status_code=409)
        content_hash=hashlib.sha256(json.dumps({'instructions':resources[resource],
            'manifest':binding,'style':style,'font':font},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        template_skills[template['template_id']]={**binding,'resource':resource,'content_hash':content_hash}
        template['skill']=deepcopy(template_skills[template['template_id']])
    samples={t['sample_resource']:{'mime_type':'video/mp4','sha256':hashlib.sha256(root.joinpath(t['sample_resource']).read_bytes()).hexdigest()} for t in templates}
    assets={}
    asset_names=['assets/'+c['component_id']+'.png' for c in components]+[s['preview_resource'] for s in styles]+[s['preview_resource'] for s in fonts if 'preview_resource' in s]
    for name in asset_names:
        data=root.joinpath(name).read_bytes()
        assets[name]={'mime_type':'image/png','sha256':hashlib.sha256(data).hexdigest(),
                      'data':base64.b64encode(data).decode('ascii')}
    digest = hashlib.sha256(json.dumps({'resources':resources,'assets':assets,'samples':samples}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {**manifest, "content_hash": digest, "resources": resources, "visual_styles":styles,
            "selected_visual_style_id":"variety", "font_styles":json.loads(resources["font_styles.json"]), "selected_font_style_id":"default",
            "templates":templates, "template_skills":template_skills, "samples":samples, "presets": json.loads(resources["presets.json"]),
            "components": [{**component,'preview_resource':'assets/'+component['component_id']+'.png'} for component in components],
            "assets":assets}


def design_library(skill=None):
    """Built-in design a custom skill may bind by id; renderers stay in Clipen."""
    skill = skill or _builtin_skill()
    return {"visual_styles": tuple(s["visual_style_id"] for s in skill["visual_styles"]),
            "font_styles": tuple(s["font_style_id"] for s in skill["font_styles"]),
            "components": tuple(c["component_id"] for c in skill["components"])}


def reserved_skill_ids(skill=None):
    skill = skill or _builtin_skill()
    return frozenset({skill["skill_id"], *(t["skill_id"] for t in skill["template_skills"].values())})


_library = None


def configure_library(root):
    """Point the catalog at a skill library; the Controller and tests call this."""
    global _library
    from .library import SkillLibrary
    base = _builtin_skill()
    _library = SkillLibrary(Path(root), design_library(base), reserved_skill_ids(base))
    return _library


def skill_library():
    if _library is None:
        from clipen.bootstrap import clipen_data_root
        return configure_library(clipen_data_root() / "skills")
    return _library


def compose_custom_skill(package, base=None):
    """Layer a custom package's instructions over the built-in design library.

    The package is delivered exactly like a selected template Skill: the default
    SKILL.md read appends it, and its resources live under custom/<skill_id>/.
    Its clipen.json picks the default style/font and narrows the components the
    tools will accept.
    """
    base = deepcopy(base or _builtin_skill())
    prefix = f"custom/{package.skill_id}/"
    skill = base
    for path, text in package.resources.items():
        skill["resources"][prefix + path] = text
    for path, asset in package.assets.items():
        skill["assets"][prefix + path] = deepcopy(asset)
    binding = package.binding
    skill.update({
        "skill_id": package.skill_id, "version": package.version, "name": package.name,
        "description": package.description, "source": "custom",
        "base_skill": {key: base[key] for key in ("version", "content_hash")} | {"skill_id": BUILTIN_SKILL},
        "templates": [], "template_skills": {}, "samples": {},
        "selected_template_skill": {"skill_id": package.skill_id, "version": package.version,
                                    "resource": prefix + "SKILL.md", "content_hash": package.content_hash},
        "custom_skill": package.summary(),
    })
    if "visual_style_id" in binding:
        skill["selected_visual_style_id"] = binding["visual_style_id"]
    if "font_style_id" in binding:
        skill["selected_font_style_id"] = binding["font_style_id"]
    if "components" in binding:
        allowed = set(binding["components"])
        skill["components"] = [c for c in skill["components"] if c["component_id"] in allowed]
    if "default_duration_seconds" in binding:
        skill["default_duration_seconds"] = binding["default_duration_seconds"]
    skill["content_hash"] = hashlib.sha256(json.dumps(
        {"base": base["content_hash"], "package": package.content_hash, "binding": binding},
        sort_keys=True).encode()).hexdigest()
    return skill


def get_skill(skill_id=BUILTIN_SKILL):
    if skill_id == BUILTIN_SKILL:
        return _builtin_skill(skill_id)
    return compose_custom_skill(skill_library().load(skill_id))


def _public(skill):
    from .visual_styles import public_styles, public_font_styles
    return {**deepcopy({key:skill[key] for key in ("skill_id","version","name","description","content_hash")}),
            'source': skill.get('source', 'builtin'),
            'templates':deepcopy(skill['templates']), 'visual_styles':public_styles(skill), 'font_styles':public_font_styles(skill),
            'selected_visual_style_id': skill.get('selected_visual_style_id'), 'selected_font_style_id': skill.get('selected_font_style_id'),
            **({'default_duration_seconds': skill['default_duration_seconds']} if 'default_duration_seconds' in skill else {}),
            **({'custom': {k: skill['custom_skill'][k] for k in ('binding', 'ignored')}} if 'custom_skill' in skill else {})}


def list_skills():
    base = _builtin_skill()
    skills = [_public(base)]
    library = skill_library()
    for entry in library.list():
        if entry["state"] == "ready":
            skills.append(_public(compose_custom_skill(library.load(entry["skill_id"]), base)))
    return skills
