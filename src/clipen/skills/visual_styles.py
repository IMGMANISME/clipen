"""Resolve appearance from the session's frozen package, never from live defaults."""
from copy import deepcopy
from clipen.projects import ProjectError


def visual_styles(skill):
    return skill.get('visual_styles', [{'visual_style_id':'variety','name':'活潑綜藝',
                                       'description':'本工作原有的元件樣式。','overrides':{}}])


def style_presets(skill, visual_style_id=None):
    identifier=visual_style_id if visual_style_id is not None else skill.get('selected_visual_style_id','variety')
    style=next((s for s in visual_styles(skill) if s['visual_style_id']==identifier),None)
    if style is None:
        raise ProjectError('visual_style_unavailable','這個工作沒有這套畫面風格，請選擇工作內提供的樣式。',status_code=422)
    presets=deepcopy(skill['presets'])
    for section,values in style.get('overrides',{}).items():
        presets.setdefault(section,{}).update(deepcopy(values))
    return identifier,presets


def public_styles(skill):
    return [{k:v for k,v in style.items() if k!='overrides'} for style in visual_styles(skill)]


def font_styles(skill):
    return skill.get('font_styles',[{'font_style_id':'default','name':'跟隨畫面風格','description':'本工作原有字體。','roles':{}}])


def font_presets(skill, presets, font_style_id=None):
    identifier = font_style_id if font_style_id is not None else skill.get('selected_font_style_id','default')
    style = next((s for s in font_styles(skill) if s['font_style_id']==identifier),None)
    if style is None:
        raise ProjectError('font_style_unavailable','這個工作沒有這套字體搭配，請選擇工作內提供的組合。',status_code=422)
    result = deepcopy(presets)
    if style['roles']:
        result['font_roles'] = deepcopy(style['roles'])
    else:
        result.pop('font_roles',None)
    return identifier, result


def public_font_styles(skill):
    return [{k:v for k,v in style.items() if k!='roles'} for style in font_styles(skill)]
