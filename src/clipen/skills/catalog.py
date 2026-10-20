"""Read-only skill packages. Source footage and project facts never enter them."""
from copy import deepcopy
import base64
import hashlib
from importlib.resources import files
import json

from clipen.projects import ProjectError


def get_skill(skill_id="travel-program"):
    if skill_id != "travel-program":
        raise ProjectError("skill_not_found", "找不到這套影片製作 Skill。", status_code=404)
    root = files("clipen.skills").joinpath(skill_id)
    paths = ("SKILL.md", "manifest.json", "presets.json", "components.json",
             "references/story.md", "references/editing.md", "references/captions.md",
             "references/sound.md", "references/graphics.md", "references/review.md")
    resources = {name: root.joinpath(name).read_text(encoding="utf-8") for name in paths}
    manifest = json.loads(resources["manifest.json"])
    components=json.loads(resources['components.json'])
    assets={}
    for component in components:
        name='assets/'+component['component_id']+'.png'
        data=root.joinpath(name).read_bytes()
        assets[name]={'mime_type':'image/png','sha256':hashlib.sha256(data).hexdigest(),
                      'data':base64.b64encode(data).decode('ascii')}
    digest = hashlib.sha256(json.dumps({'resources':resources,'assets':assets}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {**manifest, "content_hash": digest, "resources": resources,
            "presets": json.loads(resources["presets.json"]),
            "components": [{**component,'preview_resource':'assets/'+component['component_id']+'.png'} for component in components],
            "assets":assets}


def list_skills():
    skill = get_skill()
    return [deepcopy({key: skill[key] for key in
                     ("skill_id", "version", "name", "description", "content_hash")})]
