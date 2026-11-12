"""Versions a person can return to: one per complete edit, not one per AI tool call.

The AI saves a revision after every small step (one caption, one label), and
those stay available for repair tools. People see a version when an AI request
ends (finished, or stopped part way with changes) and after each restore.
Projects from before review notes may also hold manual edits, still labelled below.
"""
import json
import sqlite3

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
           'remove_program_component': '移除節目元件',
           'add_custom_component': '新增自由設計元件', 'update_custom_component': '修改自由設計元件',
           'set_component_text': '元件文字', 'edit_component': '修改元件', 'edit_component_look': '元件外觀'}


def _name(provider_id):
    try:
        return get_provider(provider_id).display_name
    except ValueError:
        return 'AI'


def versions(database, run_id, provider, styles=None, current=None):
    """Versions of one film, newest first, each saying what changed. `provider` is the run's current AI;
    `current` the revision the film is at now, which is always the newest entry."""
    rows = database.execute(
        "SELECT sequence,event_type,payload_json,created_at FROM edit_agent_events WHERE run_id=? AND event_type IN "
        "('run_created','refinement_requested','revision_created','revision_selected','run_finished','provider_failed','run_cancelled',"
        "'run_interrupted','budget_exhausted','tool_completed','provider_switched','provider_finished') ORDER BY sequence", (run_id,)).fetchall()
    switches = [json.loads(r['payload_json']) for r in rows if r['event_type'] == 'provider_switched']
    current_ai = switches[0]['from'] if switches else provider
    latest = start = None
    request = None
    sent = {}
    result = []

    skipped = []
    # Tokens an AI attempt used, waiting for the version that attempt produced.
    pending, last_ai, span = None, None, 0

    def add(kind, label, created_at, detail=None):
        nonlocal pending, last_ai
        if latest is None or (result and result[-1]['revision_id'] == latest):
            if latest is not None:
                skipped.append((kind, label, created_at))
            return
        entry = {'revision_id': latest, 'kind': kind, 'label': label, 'detail': detail, 'created_at': created_at}
        if kind.startswith('ai_'):
            entry['usage'], pending, last_ai = pending, None, (entry, span)
        result.append(entry)

    for row in rows:
        kind, payload = row['event_type'], json.loads(row['payload_json'])
        usage = payload.get('usage') if kind in ('provider_finished', 'provider_failed') else None
        if isinstance(usage, dict):
            # A finished attempt reports after its version is listed; a stopped one before.
            if kind == 'provider_finished' and last_ai is not None and last_ai[1] == span:
                last_ai[0]['usage'] = add_usage(last_ai[0]['usage'], usage)
            else:
                pending = add_usage(pending, usage)
        if kind in ('run_created', 'refinement_requested'):
            # An attempt that changed nothing has no version to show it; it still counts in the film's total.
            span, pending = span + 1, None
        if kind in ('revision_created', 'revision_selected'):
            latest = payload.get('revision_id', latest)
            if start is None:
                start = latest
        elif kind == 'run_created':
            request, start, sent = None, latest, {}
        elif kind == 'refinement_requested':
            request, start = payload.get('instruction'), payload.get('base_revision_id', latest)
            sent = {item.get('reference_id'): item for item in (payload.get('references') or {}).get('items', []) if isinstance(item, dict)}
        elif kind == 'provider_switched':
            current_ai = payload.get('to', current_ai)
        elif kind == 'run_finished':
            add('ai_complete', f'{_name(current_ai)} {"打磨" if request else "製作"}完成', row['created_at'], request)
            if result and result[-1]['revision_id'] == latest:
                result[-1]['report'] = _report(payload, sent)
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
    # Older projects did not record a step that went back to an earlier picture; show where the film is now.
    if current and result and result[-1]['revision_id'] != current and skipped:
        kind, label, created_at = skipped[-1]
        result.append({'revision_id': current, 'kind': kind, 'label': label, 'detail': None, 'created_at': created_at})
    _describe(database, run_id, result, styles or {})
    for number, entry in enumerate(result, 1):
        entry['number'] = number
    return list(reversed(result))


def _report(payload, sent):
    """What the AI said it changed, with each answer placed next to the note it answers."""
    from .change_report import OUTCOMES
    report = payload.get('report') or {}
    notes = [{**note, 'outcome_label': OUTCOMES.get(note.get('outcome'), ''), 'kind': (sent.get(note.get('reference_id')) or {}).get('kind'),
              'note': (sent.get(note.get('reference_id')) or {}).get('note', '')} for note in report.get('notes', [])]
    from .change_report import REASONS
    # Reports written before reasons were required hold plain lines.
    left = [{'what': item, 'reason': None, 'reason_label': '', 'detail': ''} if isinstance(item, str)
            else {**item, 'reason_label': REASONS.get(item.get('reason'), '')} for item in report.get('not_done', [])]
    for note in notes:
        note['reason_label'] = REASONS.get(note.get('reason'), '')
    return {'summary': payload.get('summary') or '', 'changes': report.get('changes', []), 'notes': notes, 'not_done': left}


def add_usage(total, usage):
    keys = ('input_tokens', 'cached_input_tokens', 'output_tokens')
    if not isinstance(usage, dict) or not all(isinstance(usage.get(k), int) for k in keys):
        return total
    return {k: (total or {}).get(k, 0) + usage[k] for k in keys}


def film_usage(database, run_id):
    """Every token the AI used on this film, including attempts that changed nothing."""
    total = None
    for row in database.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type IN ('provider_finished','provider_failed')", (run_id,)):
        total = add_usage(total, json.loads(row['payload_json']).get('usage'))
    return total


def _describe(database, run_id, result, styles):
    """What each version changed from the one before it, and which moment a restore went back to."""
    from .history_changes import describe, summary
    ids = sorted({entry['revision_id'] for entry in result})
    payloads = {}
    try:
        for start in range(0, len(ids), 400):
            chunk = ids[start:start+400]
            for row in database.execute(f"SELECT revision_id,payload_json FROM edit_agent_revisions WHERE run_id=? AND revision_id IN ({','.join('?'*len(chunk))})", (run_id, *chunk)):
                payloads[row['revision_id']] = json.loads(row['payload_json'])
    except sqlite3.OperationalError:
        pass  # Without stored pictures the history still lists every step, only without what changed.
    for index, entry in enumerate(result):
        earlier = next((e for e in reversed(result[:index]) if e['revision_id'] == entry['revision_id']), None)
        if entry['kind'] == 'restore' and earlier is not None:
            entry['restored_from'] = earlier['created_at']
        before, after = (payloads.get(result[index-1]['revision_id']) if index else None), payloads.get(entry['revision_id'])
        entry['changes'] = summary(describe(before, after, styles)) if before and after else None
