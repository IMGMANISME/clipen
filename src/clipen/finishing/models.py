"""Versioned creative decisions and deterministic compilation into an editable cut."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import hashlib
import json
import math

from jsonschema import Draft202012Validator

from clipen.domain import ClipSource, EditTimeline, SourceSpan, TrackKind
from clipen.projects import ProjectError
from clipen.music_vocabulary import MUSIC_TAGS


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def invalid(message='AI 製作決策不完整或超出支援範圍。'):
    return ProjectError('finishing_invalid', message, status_code=422)


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def array(item, maximum, minimum=0):
    return {'type': 'array', 'items': item, 'minItems': minimum, 'maxItems': maximum}


TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 80, 'pattern': '^[^\\u0000-\\u001f]+$'}
NOTE = {**TEXT, 'maxLength': 1000}
ID = {'type': 'string', 'minLength': 1, 'maxLength': 160}
FRAME = {'type': 'integer', 'minimum': 0, 'maximum': 4500}
SPAN = {'start_frame': FRAME, 'duration_frames': {'type': 'integer', 'minimum': 1, 'maximum': 4500}}
GAIN = {'type': 'number', 'minimum': -36, 'maximum': 0}
BRIEF_SCHEMA = obj({
    'intent': NOTE,
    'story_arc': NOTE,
    'music_tags': array({'enum': MUSIC_TAGS}, 4, 1),
    'music_reason': NOTE,
    'preserve': array(obj({'clip_id': ID, 'reason': TEXT}), 30),
})
SCHEMA = obj({
    'schema_version': {'const': 2},
    'style': {'const': 'travel.clean.v2'},
    'summary': NOTE,
    'edit': array(obj({'clip_id': ID, 'source_offset_frames': FRAME,
                       'duration_frames': {'type': 'integer', 'minimum': 1, 'maximum': 600},
                       'purpose': {'enum': ['hook', 'story', 'detail', 'reaction', 'transition', 'ending']},
                       'reason': TEXT, 'evidence_refs': array(ID, 8, 1)}), 30, 3),
    'captions': array(obj({'cue_id': ID, 'text': TEXT, 'emphasis': {'type': 'string', 'maxLength': 12}}), 96),
    'cards': array(obj({**SPAN, 'text': TEXT, 'purpose': {'enum': ['chapter', 'location']}}), 6),
    'music': obj({'asset_id': ID, 'offset_seconds': {'type': 'number', 'minimum': 0, 'maximum': 3600},
                  'gain_db': GAIN, 'reason': NOTE,
                  'regions': array(obj({**SPAN, 'gain_db': {'type':'number','minimum':-60,'maximum':0}, 'reason':TEXT}),12,1)}),
    'alternatives': array(obj({'asset_id': ID, 'reason_not_selected': TEXT}), 3),
    'sound_effects': array(obj({**SPAN, 'asset_id': ID, 'gain_db': GAIN, 'reason': TEXT}), 12),
    'source_audio': array(obj({'clip_id': ID, 'mode': {'enum': ['keep', 'mute', 'dialogue']}, 'reason': TEXT}), 30, 3),
    'transitions': array(obj({'clip_id': ID, 'kind': {'enum': ['dip_black', 'dip_white']},
                              'duration_frames': {'type': 'integer', 'minimum': 2, 'maximum': 24}, 'reason': TEXT}), 8),
    'motion': array(obj({'clip_id': ID, 'kind': {'enum': ['punch_in', 'slow_push', 'reframe', 'speed_ramp']},
                         'amount': {'type': 'number', 'minimum': 1.02, 'maximum': 1.25},
                         'x': {'type': 'number', 'minimum': 0.1, 'maximum': 0.9},
                         'y': {'type': 'number', 'minimum': 0.1, 'maximum': 0.9}}), 12),
    'grade': obj({'contrast': {'type': 'number', 'minimum': 0.95, 'maximum': 1.12},
                  'saturation': {'type': 'number', 'minimum': 0.9, 'maximum': 1.15},
                  'brightness': {'type': 'number', 'minimum': -0.04, 'maximum': 0.04}}),
    'beat_markers': array(FRAME, 30),
})


def validate_brief(payload, context):
    if not Draft202012Validator(BRIEF_SCHEMA).is_valid(payload):
        raise invalid('AI 沒有產生可執行的影片與配樂方向。')
    clips = {i['item_id'] for t in context['timeline']['tracks'] if t['kind'] == 'video' for i in t['items']}
    if any(p['clip_id'] not in clips for p in payload['preserve']):
        raise invalid('敘事方向引用了不存在的鏡頭。')
    return json.loads(encoded(payload))


def compile_context(context, plan):
    """Re-cut source spans and remap ASR once, with exact rational source time."""
    base = EditTimeline.from_payload(context['timeline'])
    fps = Fraction(base.frame_rate.numerator, base.frame_rate.denominator)
    originals = {i.item_id: i for t in base.tracks if t.kind is TrackKind.VIDEO for i in t.items}
    modes = {a['clip_id']: a['mode'] for a in plan['source_audio']}
    selected, cues, cursor = [], [], 0
    used = set()
    facts = {f['evidence_id'] for f in context.get('shot_facts', [])}
    for decision in plan['edit']:
        clip = originals.get(decision['clip_id'])
        if clip is None or clip.item_id in used:
            raise invalid('剪輯決策引用了不存在或重複的鏡頭。')
        used.add(clip.item_id)
        offset, duration = decision['source_offset_frames'], decision['duration_frames']
        if duration/fps < Fraction(3, 4) or duration/fps > 10 or offset + duration > clip.duration_frames:
            raise invalid('AI 選擇的鏡頭區間超出已確認方向的來源範圍。')
        if not decision['evidence_refs'] or any(ref not in facts for ref in decision['evidence_refs']):
            raise invalid('剪輯理由必須引用已提供的影片證據。')
        source = clip.source.span
        units_per_frame = Fraction(source.time_base.denominator * base.frame_rate.denominator,
                                   source.time_base.numerator * base.frame_rate.numerator)
        start = int(source.start_pts) + round(offset * units_per_frame)
        end = min(int(source.end_pts), start + round(duration * units_per_frame))
        span = SourceSpan(source.version_id, source.stream_index, str(start), str(end), source.time_base)
        selected.append(replace(clip, start_frame=cursor, duration_frames=duration, source=ClipSource(span), effects=()))
        if modes.get(clip.item_id) != 'mute':
            for cue in context['cues']:
                if cue['clip_id'] != clip.item_id:
                    continue
                left = cue['start_frame'] - clip.start_frame
                right = left + cue['duration_frames']
                if right <= offset or left >= offset + duration:
                    continue
                if not cue.get('complete', True) or left < offset or right > offset + duration:
                    raise invalid('這個剪點會切斷保留的口語。AI 需要保留完整句子或將這段作為靜音畫面。')
                cues.append({**cue, 'start_frame': cursor + left - offset})
        cursor += duration
    if not 30 <= cursor/fps <= 75:
        raise invalid('完成度樣片需維持 30–75 秒，目標為 45–60 秒。')
    protected = {p['clip_id'] for p in context.get('creative_brief', {}).get('preserve', [])}
    if not protected.issubset(used):
        raise invalid('AI 刪除了本次敘事指定保留的鏡頭。')
    track = next(t for t in base.tracks if t.kind is TrackKind.VIDEO)
    timeline = replace(base, timeline_id='finishcut_' + digest(plan['edit'])[:32], duration_frames=cursor,
                       tracks=(replace(track, items=tuple(selected)),), markers=())
    return {**context, 'timeline': timeline.to_payload(), 'cues': sorted(cues, key=lambda c: c['start_frame'])}


def validate_plan(payload, context):
    try:
        if len(encoded(payload).encode()) > 64_000 or not Draft202012Validator(SCHEMA).is_valid(payload):
            raise invalid()
    except (TypeError, ValueError):
        raise invalid()
    plan = json.loads(encoded(payload))
    compiled = compile_context(context, plan)
    timeline = compiled['timeline']
    fps = timeline['frame_rate']['num'] / timeline['frame_rate']['den']
    end = timeline['duration_frames']
    clips = {i['item_id']: i for t in timeline['tracks'] if t['kind'] == 'video' for i in t['items']}
    modes = {a['clip_id']: a['mode'] for a in plan['source_audio']}
    if len(plan['source_audio']) != len(clips) or set(modes) != set(clips):
        raise invalid('每個選用的鏡頭都需要明確的原音安排。')
    cues = {c['cue_id']: c for c in compiled['cues']}
    seen = set()
    for caption in plan['captions']:
        cue = cues.get(caption['cue_id'])
        if cue is None or caption['cue_id'] in seen or len(caption['text']) > 40 or cue['duration_frames']/fps < .45:
            raise invalid('字幕必須引用保留的完整口語，並有足夠的閱讀時間。')
        if caption['emphasis'] and caption['emphasis'] not in caption['text']:
            raise invalid('強調詞必須出現在字幕內。')
        seen.add(caption['cue_id'])
    if seen != set(cues):
        raise invalid('保留的口語尚未全部安排字幕。')
    if any(a['start_frame']+a['duration_frames'] > b['start_frame'] for a,b in zip(compiled['cues'],compiled['cues'][1:])):
        raise invalid('保留的口語互相重疊，AI 需要調整剪輯。')
    assets = {a['asset_id']: a for a in context['assets']}
    music = assets.get(plan['music']['asset_id'])
    if music is None or music['role'] != 'music' or plan['music']['offset_seconds']+end/fps > min(music['duration_seconds'], music.get('analyzed_seconds',90)):
        raise invalid('選用的配樂區間不足以涵蓋樣片。')
    if not music.get('provenance'):
        raise invalid('自動配樂缺少可回查的來源與授權條件。')
    cursor = 0
    for region in plan['music']['regions']:
        if region['start_frame'] != cursor:
            raise invalid('配樂的敘事區段必須連續，安靜段落也需要明確安排。')
        cursor += region['duration_frames']
    if cursor != end:
        raise invalid('配樂區段與重剪後的影片長度不一致。')
    alternatives = [a['asset_id'] for a in plan['alternatives']]
    if len(alternatives) != len(set(alternatives)) or any(a not in assets or a == music['asset_id'] for a in alternatives):
        raise invalid('配樂比較引用了不存在或重複的候選曲。')
    for collection in ('cards', 'sound_effects'):
        for item in plan[collection]:
            if item['start_frame']+item['duration_frames'] > end:
                raise invalid('製作元素超過樣片結尾。')
            if collection == 'cards' and (len(item['text']) > 24 or item['duration_frames']/fps < 1):
                raise invalid('字卡需至少顯示一秒且不超過 24 字。')
            if collection == 'sound_effects':
                source = assets.get(item['asset_id'])
                if source is None or source['role'] != 'sfx' or item['duration_frames']/fps > source['duration_seconds']:
                    raise invalid('音效來源不存在或長度不足。')
    # Concurrent chapter/location cards are valid. The compositor lays them out
    # vertically; temporal overlap is not a reason to discard paid AI decisions.
    spoken = {c['clip_id'] for c in compiled['cues']}
    for key in ('motion', 'transitions'):
        ids = [i['clip_id'] for i in plan[key]]
        if len(ids) != len(set(ids)) or any(i not in clips for i in ids):
            raise invalid('效果引用了不存在或重複的鏡頭。')
    for motion in plan['motion']:
        if motion['kind'] == 'speed_ramp' and motion['clip_id'] in spoken:
            raise invalid('保留口語的鏡頭不能變速。')
    if 'audio_streams' in context and any(modes[c] != 'mute' and context['audio_streams'].get(c) is None for c in clips):
        raise invalid('沒有原音的鏡頭必須明確設成靜音。')
    ordered = list(clips.values())
    for transition in plan['transitions']:
        clip = clips[transition['clip_id']]
        index = ordered.index(clip)
        if index == 0 or transition['duration_frames'] > min(clip['duration_frames'], ordered[index-1]['duration_frames']):
            raise invalid('轉場必須位於有效剪點。')
    offset = plan['music']['offset_seconds']
    beats = [(b-offset)*fps for b in music['beats_seconds']]
    cuts = [c['start_frame'] for c in clips.values() if c['start_frame']]
    if any(not 0 <= frame < end or not any(abs(frame-b) <= fps*.1 for b in beats)
           or not any(abs(frame-cut) <= fps*.15 for cut in cuts) for frame in plan['beat_markers']):
        raise invalid('標示對拍的剪點必須與實際分析的音樂節拍一致。')
    return plan
