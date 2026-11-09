"""The account the AI gives of each change: what it changed and why, how it handled each review note,
and what it left undone. finish_edit refuses to close a change without one."""
from clipen.projects import ProjectError

OUTCOMES = {'done': '已處理', 'partly': '部分處理', 'not_done': '沒有處理', 'kept': '照你說的保留'}


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def _invalid(message):
    return ProjectError('change_report_invalid', message, status_code=422)


def check_report(arguments, intent):
    """The report finish_edit was given, checked against the notes the person sent with this request."""
    changes = arguments.get('changes')
    if not isinstance(changes, list) or not 1 <= len(changes) <= 20 or not all(
            isinstance(c, dict) and set(c) <= {'what', 'why'} and _text(c.get('what'), 200) and _text(c.get('why'), 300) for c in changes):
        raise _invalid('finish_edit 要附上修改說明：changes 列出 1–20 項，每項寫 what（改了什麼，200 字內）與 why（為什麼，300 字內），用觀眾看得懂的話。')
    not_done = arguments.get('not_done', [])
    if not isinstance(not_done, list) or len(not_done) > 10 or not all(_text(item, 300) for item in not_done):
        raise _invalid('not_done 列出沒做到或還不確定的地方，最多 10 項，每項 300 字內。')
    request = intent.get('latest_revision_request') or {}
    sent = {item['reference_id'] for item in (request.get('references') or {}).get('items', []) if isinstance(item, dict) and 'reference_id' in item}
    notes = arguments.get('notes', [])
    if not isinstance(notes, list) or not all(
            isinstance(n, dict) and set(n) <= {'reference_id', 'outcome', 'explanation'} and n.get('outcome') in OUTCOMES
            and _text(n.get('explanation'), 300) and isinstance(n.get('reference_id'), str) for n in notes):
        raise _invalid('notes 每項要有 reference_id、outcome（done／partly／not_done／kept）與 explanation（300 字內）。')
    answered = [n['reference_id'] for n in notes]
    if len(answered) != len(set(answered)) or set(answered) != sent:
        missing = len(sent - set(answered))
        raise _invalid(f'使用者這次送了 {len(sent)} 筆注記，notes 要逐筆回應每一筆（reference_id 不重複、不多不少）'
                       + (f'；還有 {missing} 筆沒回應。' if missing else '。'))
    return {'changes': [{'what': c['what'].strip(), 'why': c['why'].strip()} for c in changes],
            'notes': [{'reference_id': n['reference_id'], 'outcome': n['outcome'], 'explanation': n['explanation'].strip()} for n in notes],
            'not_done': [item.strip() for item in not_done]}
