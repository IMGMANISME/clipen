"""Looks: named sets of typefaces and colours that graphics of one kind share.

The Agent designs each look (a chapter look, a place look, a reaction look...) and
every free design names the look it uses. Clipen gives the page that look's
fonts and colours as --look-font-<key> and --look-<key> variables, and a page may
draw text only in its look's typefaces, so graphics of one kind stay consistent
while their layout and motion remain free.

Each graphic keeps a copy of its look, so every version draws as it was saved.
The run's list of looks lives beside the film; the copies on the current film
win, so going back to an earlier version also brings back its looks.
"""
import json
import re

from clipen.projects import ProjectError
from clipen.rendering.custom_components import FONT_CHOICES

KEY = re.compile(r'^[A-Za-z0-9_\-一-鿿]{1,24}$')
HEX = re.compile(r'^#[0-9a-fA-F]{6}$')
MAX_LOOKS, MAX_FONTS, MAX_COLORS = 8, 3, 6


def _invalid(message):
    return ProjectError('component_look_invalid', message, status_code=422)


def normalize(value):
    """A look as given by the Agent, checked: {look_id, name, fonts: [{key, label, family}], colors: [{key, label, value}]}."""
    if not isinstance(value, dict) or not isinstance(value.get('look_id'), str) or not KEY.match(value['look_id']):
        raise _invalid('look_id 要是 1–24 個字的英數字、中文、- 或 _。')
    name = value.get('name') if isinstance(value.get('name'), str) and value['name'].strip() else value['look_id']
    fonts, colors = value.get('fonts'), value.get('colors', [])
    if not isinstance(fonts, list) or not 1 <= len(fonts) <= MAX_FONTS:
        raise _invalid(f'每個外觀要有 1–{MAX_FONTS} 套字型。')
    if not isinstance(colors, list) or len(colors) > MAX_COLORS:
        raise _invalid(f'每個外觀最多 {MAX_COLORS} 個顏色。')
    def entries(items, kind):
        seen, result = set(), []
        for entry in items:
            if not isinstance(entry, dict) or not isinstance(entry.get('key'), str) or not KEY.match(entry['key']) or entry['key'] in seen:
                raise _invalid('字型與顏色的 key 要是不重複的英數字、中文、- 或 _。')
            seen.add(entry['key'])
            label = entry.get('label') if isinstance(entry.get('label'), str) and entry['label'].strip() else entry['key']
            if kind == 'font':
                if entry.get('family') not in FONT_CHOICES:
                    raise _invalid('可用的字型：' + '、'.join(FONT_CHOICES) + '。')
                result.append({'key': entry['key'], 'label': label.strip()[:24], 'family': entry['family']})
            else:
                if not isinstance(entry.get('value'), str) or not HEX.match(entry['value']):
                    raise _invalid('顏色要寫成 #rrggbb。')
                result.append({'key': entry['key'], 'label': label.strip()[:24], 'value': entry['value'].lower()})
        return result
    return {'look_id': value['look_id'], 'name': name.strip()[:24], 'fonts': entries(fonts, 'font'), 'colors': entries(colors, 'color')}


def _path(tools):
    return tools.repository.projects.root / tools.project_id / f'component-looks-{tools.run_id}.json'


def stored(tools):
    path = _path(tools)
    if path.is_symlink():
        raise ProjectError('storage_unsafe', '外觀檔案不安全。', status_code=409)
    if not path.exists():
        return {}
    return {look['look_id']: look for look in json.loads(path.read_text(encoding='utf-8'))}


def store(tools, looks):
    from .production_media import atomic_json
    atomic_json(_path(tools), list(looks.values()))


def on_film(tools, timeline=None):
    """(graphic, profile) for each free design on the film."""
    from .production_media import load_text_styles
    styles = load_text_styles(tools.repository.projects, tools.project_id)
    timeline = timeline or tools._timeline()
    return [(item, styles[item.style_id]) for track in timeline.tracks
            if track.track_id == 'program-components' or track.track_id.startswith('program-components-')
            for item in track.items if styles.get(item.style_id, {}).get('component_id') == 'custom']


def current(tools):
    """The run's looks, as the current film has them."""
    looks = stored(tools)
    for _, profile in on_film(tools):
        look = profile.get('look')
        if look:
            looks[look['look_id']] = look
    return looks


def public(look, used=0):
    """A look as the Agent and the review page see it, with the variables a page writes."""
    return {**look, 'used_by': used,
            'fonts': [{**f, 'css': f'var(--look-font-{f["key"]})'} for f in look['fonts']],
            'colors': [{**c, 'css': f'var(--look-{c["key"]})'} for c in look['colors']]}


def listing(tools):
    counts = {}
    for _, profile in on_film(tools):
        if profile.get('look'):
            counts[profile['look']['look_id']] = counts.get(profile['look']['look_id'], 0) + 1
    return [public(look, counts.get(key, 0)) for key, look in current(tools).items()]
