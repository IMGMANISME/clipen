"""Bounded, factual activity for the production UI; no inferred completion rates.

Stages report how far the AI has observably got, never a percentage of the
work: an agent revisits steps and has no fixed number of them.
"""
import json


_TOOL_GROUPS = (
    ('理解製作需求與影片風格', ('get_edit_status', 'list_editing_tools', 'list_production_skills', 'read_production_skill', 'list_program_components')),
    ('尋找與檢視來源素材', ('list_source_media', 'search_media', 'search_source_dialogue', 'register_source_range', 'sample_source', 'inspect_candidates', 'inspect_clip')),
    ('準備本機語音模型', ('prepare_local_speech',)),
    ('辨識與對齊來源口語', ('transcribe_source_range', 'align_source_speech', 'revise_source_speech', 'list_transcripts')),
    ('安排故事與剪輯', ('record_story_plan', 'read_story_plan', 'plan_episode', 'replace_sequence', 'replace_clip_source', 'trim_clip', 'reorder_clips', 'split_clip', 'insert_clip', 'set_canvas', 'remove_item')),
    ('製作字幕與畫面設計', ('replace_subtitles', 'edit_text', 'configure_text_style', 'add_clip_title', 'add_program_component', 'update_program_component', 'remove_program_component', 'add_custom_component', 'update_custom_component', 'set_component_look', 'set_color_grade', 'set_mosaic', 'set_transition', 'set_fade', 'add_effect', 'remove_clip_effect')),
    ('尋找與安排配樂、音效', ('search_music', 'list_audio_assets', 'extract_audio', 'inspect_audio', 'place_audio', 'remove_audio', 'set_source_audio')),
    ('輸出樣片預覽', ('render_preview',)),
    ('檢查畫面、字幕與聲音', ('inspect_preview', 'inspect_cut', 'review_cut', 'validate_sequence', 'inspect_mix', 'inspect_pacing')),
    ('等待或處理本機工作結果', ('read_evidence_job', 'retry_evidence_job', 'cancel_evidence_job')),
    ('整理交付檔案', ('export_edit', 'finish_edit')),
)
_TOOL_LABELS = {tool: label for label, tools in _TOOL_GROUPS for tool in tools}
_EVENT_LABELS = {
    'run_created': '製作工作已建立', 'run_started': 'AI 已開始製作',
    'refinement_requested': '收到你的修改要求', 'run_resumed': '接續上次的草稿',
    'provider_switched': '換另一個 AI 接手',
    'revision_created': '草稿已儲存', 'production_preview': '已提交預覽合成',
    # Reviewing a preview and its mix are the same step to the viewer as the review tools.
    'production_review': '檢查畫面、字幕與聲音', 'mix_review': '檢查畫面、字幕與聲音',
    'run_finished': '樣片製作已完成，等待你觀看',
    'provider_failed': 'AI 這次沒有完成製作',
    'run_cancelled': '製作已停止', 'run_interrupted': '製作已中斷',
    'budget_exhausted': '本次工作已達操作上限',
}
# A failed attempt is described by why it stopped, not as a generic error.
_FAILURE_LABELS = {
    'delivery_blocked': 'AI 做完草稿，但還有項目沒通過檢查',
    'provider_stopped_early': 'AI 先停下來了，請看 AI 的說明',
    'provider_usage_limit': 'AI 額度不足，暫停製作',
    'provider_timed_out': '超過製作時間上限，暫停製作',
}
# The furthest stage reached moves only forward. Checking the cut before any
# preview is part of editing; reviewing only counts once a film was rendered.
STAGES = ('understand', 'sources', 'edit', 'graphics', 'music', 'render', 'review')
_STAGE_OF_LABEL = {
    '理解製作需求與影片風格': 'understand', '尋找與檢視來源素材': 'sources', '準備本機語音模型': 'sources',
    '辨識與對齊來源口語': 'sources', '安排故事與剪輯': 'edit', '製作字幕與畫面設計': 'graphics',
    '尋找與安排配樂、音效': 'music', '輸出樣片預覽': 'render', '已提交預覽合成': 'render',
    '檢查畫面、字幕與聲音': 'review', '整理交付檔案': 'review',
}


def _stages(labels, finished):
    furthest, rendered = -1, False
    for label in labels:
        stage = _STAGE_OF_LABEL.get(label)
        if stage is None:
            continue
        if stage == 'render':
            rendered = True
        if stage == 'review' and not rendered:
            stage = 'edit'
        furthest = max(furthest, STAGES.index(stage))
    if finished:
        furthest = len(STAGES)
    return {'stages': STAGES, 'reached': furthest}


# Each request (a new film, a revision, a resume) starts a fresh activity list.
_STARTS = ('run_created', 'refinement_requested', 'run_resumed')
_SHOWN = 10


def _duration(seconds):
    minutes = max(1, round(seconds / 60))
    return f'{minutes} 分鐘' if minutes < 60 else f'{minutes // 60} 小時 {minutes % 60} 分鐘'


def _provider_name(provider_id):
    from clipen.provider_registry import get_provider
    try:
        return get_provider(provider_id).display_name
    except (KeyError, ValueError, TypeError):
        return 'AI'


def _latest_start(database, run_id, kinds):
    return database.execute(
        f"SELECT COALESCE(MAX(sequence),0) FROM edit_agent_events WHERE run_id=? AND event_type IN ({','.join('?' for _ in kinds)})",
        (run_id, *kinds)).fetchone()[0]


def production_activity(database, run_id):
    boundary = _latest_start(database, run_id, _STARTS)
    # Resuming continues the same request, so its stages carry on from before.
    request_boundary = _latest_start(database, run_id, ('run_created', 'refinement_requested'))
    kinds = ('tool_called', 'provider_finished', 'machine_slept', *_EVENT_LABELS)
    placeholders = ','.join('?' for _ in kinds)
    rows = database.execute(
        f'SELECT sequence,event_type,payload_json,created_at FROM edit_agent_events '
        f'WHERE run_id=? AND sequence>=? AND event_type IN ({placeholders}) ORDER BY sequence',
        (run_id, request_boundary, *kinds),
    ).fetchall()
    activity, message, request, labels, finished, slept = [], None, None, [], False, 0
    started_at = next((row['created_at'] for row in rows if row['sequence'] >= boundary), None)
    for row in rows:
        payload = json.loads(row['payload_json'])
        kind = row['event_type']
        finished = finished or kind == 'run_finished'
        if kind in {'provider_finished', 'provider_failed'} and row['sequence'] >= boundary:
            message = payload.get('message') or message
        if kind == 'provider_finished':
            continue
        if kind == 'refinement_requested':
            request = payload.get('instruction')
        tool = payload.get('tool', '') if kind == 'tool_called' else None
        if kind == 'tool_called':
            label = _TOOL_LABELS.get(tool, 'AI 正在操作剪輯工具')
        elif kind == 'provider_failed':
            label = _FAILURE_LABELS.get(payload.get('error_code'), _EVENT_LABELS[kind])
        elif kind == 'machine_slept':
            if row['sequence'] >= boundary:
                slept += payload.get('seconds', 0)
            label = f"Mac 睡眠約 {_duration(payload.get('seconds', 0))}，製作暫停"
        elif kind == 'provider_switched':
            label = f"改由 {_provider_name(payload.get('to'))} 接手修改"
        else:
            label = _EVENT_LABELS[kind]
        labels.append(label)
        if row['sequence'] < boundary:
            continue
        # Many tool calls in a row are one step to the viewer; keep its latest time.
        if activity and activity[-1]['label'] == label:
            activity[-1].update(sequence=row['sequence'], created_at=row['created_at'], count=activity[-1]['count'] + 1)
            continue
        activity.append({'sequence': row['sequence'], 'kind': kind, 'label': label, 'tool': tool,
                         'created_at': row['created_at'], 'count': 1})
    activity = activity[-_SHOWN:]
    return {'activity': activity, 'updated_at': activity[-1]['created_at'] if activity else None,
            'ai_message': message, 'request': request, 'started_at': started_at, 'slept_seconds': slept,
            **_stages(labels, finished)}
