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


def list_skills():
    skill = get_skill()
    from .visual_styles import public_styles, public_font_styles
    return [{**deepcopy({key:skill[key] for key in ("skill_id","version","name","description","content_hash")}),
             'templates':deepcopy(skill['templates']), 'visual_styles':public_styles(skill), 'font_styles':public_font_styles(skill)}]
