"""The account the AI gives of each change: what it changed and why, how it handled each review note,
and what it left undone. finish_edit refuses to close a change without one."""
from clipen.projects import ProjectError

OUTCOMES = {'done': '已處理', 'partly': '部分處理', 'not_done': '沒有處理', 'kept': '照你說的保留'}
# Work may be left only for one of these: the person has to decide or supply something,
# or no Clipen tool can do it. Running out of effort is not a reason.
REASONS = {'needs_you': '需要你決定或提供', 'cannot': 'Clipen 目前做不到'}
UNFINISHED = 'change_report_unfinished'


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
    if not isinstance(not_done, list) or len(not_done) > 10 or not all(
            isinstance(item, dict) and set(item) <= {'what', 'reason', 'detail'} and item.get('reason') in REASONS
            and _text(item.get('what'), 200) and _text(item.get('detail'), 300) for item in not_done):
        raise _invalid('not_done 每項要有 what（沒做到什麼）、reason 與 detail，最多 10 項。reason 只能是 needs_you'
                       '（detail 寫使用者要決定或提供什麼）或 cannot（detail 寫是哪個工具限制做不到）。'
                       '你的工具做得到的事不能列在這裡，要先做完。')
    request = intent.get('latest_revision_request') or {}
    sent = {item['reference_id'] for item in (request.get('references') or {}).get('items', []) if isinstance(item, dict) and 'reference_id' in item}
    notes = arguments.get('notes', [])
    if not isinstance(notes, list) or not all(
            isinstance(n, dict) and set(n) <= {'reference_id', 'outcome', 'explanation', 'reason'} and n.get('outcome') in OUTCOMES
            and _text(n.get('explanation'), 300) and isinstance(n.get('reference_id'), str) for n in notes):
        raise _invalid('notes 每項要有 reference_id、outcome（done／partly／not_done／kept）與 explanation（300 字內）。')
    if any(n['outcome'] in ('partly', 'not_done') and n.get('reason') not in REASONS for n in notes):
        raise _invalid('outcome 是 partly 或 not_done 的注記要附 reason：needs_you（需要使用者決定或提供）或 cannot（工具做不到）。'
                       '你的工具做得到的就先做完，再回報 done。')
    answered = [n['reference_id'] for n in notes]
    if len(answered) != len(set(answered)) or set(answered) != sent:
        missing = len(sent - set(answered))
        raise _invalid(f'使用者這次送了 {len(sent)} 筆注記，notes 要逐筆回應每一筆（reference_id 不重複、不多不少）'
                       + (f'；還有 {missing} 筆沒回應。' if missing else '。'))
    return {'changes': [{'what': c['what'].strip(), 'why': c['why'].strip()} for c in changes],
            'notes': [{'reference_id': n['reference_id'], 'outcome': n['outcome'], 'explanation': n['explanation'].strip(),
                       **({'reason': n['reason']} if n['outcome'] in ('partly', 'not_done') else {})} for n in notes],
            'not_done': [{'what': i['what'].strip(), 'reason': i['reason'], 'detail': i['detail'].strip()} for i in not_done]}


def unfinished(report):
    """What the report leaves undone, as short lines the AI is asked to reconsider."""
    return ([f"注記 {n['reference_id']}：{n['explanation']}" for n in report['notes'] if n['outcome'] in ('partly', 'not_done')]
            + [f"{i['what']}（{i['detail']}）" for i in report['not_done']])


def challenge(report):
    """Asked once per request before work is left undone: do what the tools can do first."""
    left = unfinished(report)
    return ProjectError(UNFINISHED, '修改說明裡還有沒做完的地方：' + '；'.join(left)[:900]
                        + '。用你的工具做得到的，現在就做完並改回報 done；只有真的需要使用者決定或提供、或工具確實做不到的才保留，'
                          '確認後再呼叫一次 finish_edit。', status_code=409)


def carried(report):
    """What the last change left for the person, handed to the next request."""
    return [{'what': i['what'], 'reason': i['reason'], 'detail': i['detail']} for i in report.get('not_done', [])
            if isinstance(i, dict) and i.get('reason') in REASONS]
