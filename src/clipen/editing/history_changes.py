"""What changed between two versions of a film, said the way a person would say it.

Compares stored timeline payloads, so it works for every version ever saved,
whoever made the change.
"""

def _components(payload):
    return {item['item_id']: item for track in payload['tracks']
            if track['track_id'] == 'program-components' or track['track_id'].startswith('program-components-')
            for item in track['items']}


def _lanes(payload):
    """Which component lane each graphic is on; a higher lane is drawn on top."""
    tracks = [t for t in payload['tracks'] if t['track_id'] == 'program-components' or t['track_id'].startswith('program-components-')]
    return {item['item_id']: n for n, track in enumerate(tracks) for item in track['items']}


def _subtitles(payload):
    return {item['item_id']: item for track in payload['tracks'] if track['track_id'] == 'agent-subtitles' for item in track['items']}


def _media(payload, kind):
    return [item for track in payload['tracks'] if track['kind'] == kind for item in track['items'] if item.get('kind') == 'media']


def _name(item):
    first = (item.get('text') or '').split('\n', 1)[0].strip()
    return f'「{first[:18]}{"…" if len(first) > 18 else ""}」' if first else '一個元件'


def _clock(frames, payload):
    rate = payload['frame_rate']
    seconds = round(frames * rate['den'] / rate['num'])
    return f'{seconds // 60}:{seconds % 60:02d}'


def _graphic_change(before, after, styles, recut=False):
    """One sentence for one graphic that is in both versions, or None when it did not change."""
    if before.get('text') != after.get('text'):
        old, new = (item.get('text', '').split('\n', 1)[0] for item in (before, after))
        return f'把「{old}」改成「{new}」' if old != new else f'改了{_name(after)}的補充文字'
    if before.get('style_id') != after.get('style_id'):
        old, new = styles.get(before.get('style_id'), {}), styles.get(after.get('style_id'), {})
        if old.get('html') != new.get('html') and new.get('html'):
            return f'重新設計{_name(after)}'
        if old.get('edits') != new.get('edits') and new.get('html'):
            before_edits, after_edits = old.get('edits') or {}, new.get('edits') or {}
            words = (before_edits.get('texts') or {}) != (after_edits.get('texts') or {})
            colours = (before_edits.get('colors') or {}) != (after_edits.get('colors') or {})
            return f'改了{_name(after)}的' + ('文字和顏色' if words and colours else '文字' if words else '顏色')
        if (old.get('x'), old.get('y')) != (new.get('x'), new.get('y')) and old and new:
            return f'移動{_name(after)}'
        if old.get('scale', 1) != new.get('scale', 1):
            return f'{"放大" if new.get("scale", 1) > old.get("scale", 1) else "縮小"}{_name(after)}'
        if old.get('colors') != new.get('colors'):
            return f'換了{_name(after)}的顏色'
        if old.get('visual_style_id') != new.get('visual_style_id'):
            return f'{_name(after)}換了樣式'
        if old.get('font_style_id') != new.get('font_style_id'):
            return f'{_name(after)}換了字體'
        if (old.get('start_offset_frames'), old.get('anchor_item_id')) == (new.get('start_offset_frames'), new.get('anchor_item_id')) and not recut:
            return f'調整{_name(after)}'
    # After a new cut every graphic shifts with its shot; that is part of the cut, not news of its own.
    if not recut and (before['start_frame'], before['duration_frames']) != (after['start_frame'], after['duration_frames']):
        return f'調整{_name(after)}出現的時間'
    return None


def describe(before, after, styles=None):
    """Short sentences, most noticeable first. Empty when the pictures are the same."""
    styles = styles or {}
    changes = []
    old_cut, new_cut = _media(before, 'video'), _media(after, 'video')
    recut = [(i['start_frame'], i['duration_frames'], i.get('source')) for i in old_cut] != [(i['start_frame'], i['duration_frames'], i.get('source')) for i in new_cut]
    if [(i['start_frame'], i['duration_frames'], i.get('source')) for i in old_cut] != [(i['start_frame'], i['duration_frames'], i.get('source')) for i in new_cut]:
        if before['duration_frames'] != after['duration_frames']:
            changes.append(f'重新剪輯，片長 {_clock(before["duration_frames"], before)} → {_clock(after["duration_frames"], after)}')
        else:
            changes.append('調整剪輯')
    elif [[e for e in i.get('effects', []) if not e.get('effect_id', '').startswith('audio.')] for i in old_cut] != \
            [[e for e in i.get('effects', []) if not e.get('effect_id', '').startswith('audio.')] for i in new_cut]:
        changes.append('調整畫面效果或轉場')
    def music(cut):
        return sorted({layer.get('asset_id') for i in cut for e in i.get('effects', []) if e.get('effect_id') == 'audio.layers'
                       for layer in e.get('parameters', {}).get('layers', [])} - {None})
    def sound(cut):
        return [[e for e in i.get('effects', []) if e.get('effect_id', '').startswith('audio.') and e.get('effect_id') != 'audio.layers'] for i in cut]
    # A new cut moves the music with it; only a different song, or new levels on the same cut, is news.
    if music(old_cut) != music(new_cut):
        changes.append('換了配樂')
    elif not changes and (sound(old_cut) != sound(new_cut) or [e for i in old_cut for e in i.get('effects', []) if e.get('effect_id') == 'audio.layers']
                          != [e for i in new_cut for e in i.get('effects', []) if e.get('effect_id') == 'audio.layers']):
        changes.append('調整配樂或聲音')
    old, new = _components(before), _components(after)
    for key in new.keys() - old.keys():
        changes.append(f'新增{_name(new[key])}')
    for key in old.keys() - new.keys():
        changes.append(f'刪除{_name(old[key])}')
    for key in sorted(old.keys() & new.keys(), key=lambda k: new[k]['start_frame']):
        sentence = _graphic_change(old[key], new[key], styles, recut)
        if sentence is None and not recut and _lanes(before)[key] != _lanes(after)[key]:
            sentence = f'把{_name(new[key])}移到另一軌'
        if sentence:
            changes.append(sentence)
    old_subtitles, new_subtitles = _subtitles(before), _subtitles(after)
    if old_subtitles != new_subtitles:
        changed = len(old_subtitles.keys() ^ new_subtitles.keys()) + sum(
            old_subtitles[k] != new_subtitles[k] for k in old_subtitles.keys() & new_subtitles.keys())
        changes.append(f'修改 {changed} 句字幕' if changed else '調整字幕')
    return changes


def summary(changes, limit=2):
    if not changes:
        return None
    shown = '、'.join(changes[:limit])
    return shown + (f'，還有 {len(changes) - limit} 處' if len(changes) > limit else '')
