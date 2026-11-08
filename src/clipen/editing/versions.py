"""Versions a person can return to: one per complete edit, not one per AI tool call.

The AI saves a revision after every small step (one caption, one label), and
those stay available for repair tools. People see a version when an AI request
ends (finished, or stopped part way with changes) and after each restore.
Projects from before review notes may also hold manual edits, still labelled below.
"""
import json

from clipen.provider_registry import get_provider

_STOPS = {'provider_failed', 'run_cancelled', 'run_interrupted', 'budget_exhausted'}
_STOP_REASONS = {
    'provider_usage_limit': '額度不足時停下', 'provider_timed_out': '超過時間上限時停下',
    'provider_authentication_required': '登入過期時停下', 'provider_stopped_early': 'AI 提早結束',
    'delivery_blocked': '交付檢查未通過', 'provider_network_error': '連線中斷時停下',
}
# Labels for manual edits: rewording a graphic now, and the edits earlier Clipen versions allowed.
_MANUAL = {'edit_text': '文字與時間', 'configure_text_style': '文字外觀', 'remove_item': '刪除元素', 'trim_clip': '裁切片段',
           'reorder_clips': '重排片段', 'split_clip': '分割片段', 'replace_clip_source': '替換來源', 'insert_clip': '插入片段',
           'set_source_audio': '原音', 'set_color_grade': '調色', 'set_mosaic': '馬賽克', 'set_transition': '轉場',
           'set_fade': '淡入淡出', 'remove_clip_effect': '移除效果', 'place_audio': '音樂與音效', 'remove_audio': '移除聲音',
           'add_program_component': '新增節目元件', 'update_program_component': '修改節目元件',
           'remove_program_component': '移除節目元件', 'restyle_program_components': '更換元件風格與字體',
           'add_custom_component': '新增自由設計元件', 'update_custom_component': '修改自由設計元件',
           'set_component_text': '元件文字', 'edit_component': '修改元件'}


def _name(provider_id):
    try:
        return get_provider(provider_id).display_name
    except ValueError:
        return 'AI'


def versions(database, run_id, provider):
    """Versions of one film, newest first. `provider` is the run's current AI."""
    rows = database.execute(
        "SELECT sequence,event_type,payload_json,created_at FROM edit_agent_events WHERE run_id=? AND event_type IN "
        "('run_created','refinement_requested','revision_created','run_finished','provider_failed','run_cancelled',"
        "'run_interrupted','budget_exhausted','tool_completed','provider_switched') ORDER BY sequence", (run_id,)).fetchall()
    switches = [json.loads(r['payload_json']) for r in rows if r['event_type'] == 'provider_switched']
    current_ai = switches[0]['from'] if switches else provider
    latest = start = None
    request = None
    result = []

    def add(kind, label, created_at, detail=None):
        if latest is None or (result and result[-1]['revision_id'] == latest):
            return
        result.append({'revision_id': latest, 'kind': kind, 'label': label, 'detail': detail, 'created_at': created_at})

    for row in rows:
        kind, payload = row['event_type'], json.loads(row['payload_json'])
        if kind == 'revision_created':
            latest = payload.get('revision_id', latest)
            if start is None:
                start = latest
        elif kind == 'run_created':
            request, start = None, latest
        elif kind == 'refinement_requested':
            request, start = payload.get('instruction'), payload.get('base_revision_id', latest)
        elif kind == 'provider_switched':
            current_ai = payload.get('to', current_ai)
        elif kind == 'run_finished':
            add('ai_complete', f'{_name(current_ai)} {"打磨" if request else "製作"}完成', row['created_at'], request)
            start = latest
        elif kind in _STOPS:
            # A stop that changed nothing is not a version.
            if latest != start:
                reason = _STOP_REASONS.get(payload.get('error_code'), '製作中斷')
                add('ai_partial', f'{_name(current_ai)} 未完成（{reason}）', row['created_at'], request)
            start = latest
        elif kind == 'tool_completed' and payload.get('authority') == 'user':
            tool = payload.get('tool')
            add('restore' if tool == 'restore_revision' else 'manual',
                '還原版本' if tool == 'restore_revision' else f'手動修改：{_MANUAL.get(tool, "編輯")}', row['created_at'])
            start = latest
    for number, entry in enumerate(result, 1):
        entry['number'] = number
    return list(reversed(result))
