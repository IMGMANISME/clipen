"""Bounded, factual activity for the production UI; no inferred completion rates."""
import json


_TOOL_GROUPS = (
    ('理解製作需求與影片風格', ('get_edit_status', 'list_editing_tools', 'list_production_skills', 'read_production_skill', 'list_program_components', 'get_program_reference')),
    ('尋找與檢視來源素材', ('list_source_media', 'search_media', 'search_source_dialogue', 'register_source_range', 'sample_source', 'inspect_candidates', 'inspect_clip')),
    ('準備本機語音模型', ('prepare_local_speech',)),
    ('辨識與對齊來源口語', ('transcribe_source_range', 'align_source_speech', 'revise_source_speech', 'list_transcripts')),
    ('安排故事與剪輯', ('record_story_plan', 'read_story_plan', 'plan_episode', 'replace_sequence', 'replace_clip_source', 'trim_clip', 'reorder_clips', 'split_clip', 'insert_clip', 'set_canvas', 'remove_item')),
    ('製作字幕與畫面設計', ('replace_subtitles', 'edit_text', 'configure_text_style', 'add_clip_title', 'add_program_component', 'update_program_component', 'remove_program_component', 'set_color_grade', 'set_mosaic', 'set_transition', 'set_fade', 'add_effect', 'remove_clip_effect')),
    ('尋找與安排配樂、音效', ('search_music', 'list_audio_assets', 'extract_audio', 'inspect_audio', 'place_audio', 'remove_audio', 'set_source_audio')),
    ('輸出樣片預覽', ('render_preview',)),
    ('檢查畫面、字幕與聲音', ('inspect_preview', 'inspect_cut', 'review_cut', 'validate_sequence', 'inspect_mix', 'inspect_pacing')),
    ('等待或處理本機工作結果', ('read_evidence_job', 'retry_evidence_job', 'cancel_evidence_job')),
    ('整理交付檔案', ('export_edit', 'finish_edit')),
)
_TOOL_LABELS = {tool: label for label, tools in _TOOL_GROUPS for tool in tools}
_EVENT_LABELS = {
    'run_created': '製作工作已建立', 'run_started': 'AI 已開始製作',
    'revision_created': '草稿已儲存', 'production_preview': '已提交預覽合成',
    'production_review': '已檢查預覽畫面', 'mix_review': '已取得混音量測',
    'run_finished': '樣片製作已完成，等待你觀看',
    'provider_failed': 'AI 執行程序回報錯誤',
    'run_cancelled': '製作已停止', 'run_interrupted': '製作已中斷',
    'budget_exhausted': '本次工作已達操作上限',
}


def production_activity(database, run_id):
    kinds = ('tool_called', *_EVENT_LABELS)
    placeholders = ','.join('?' for _ in kinds)
    rows = database.execute(
        f'SELECT sequence,event_type,payload_json,created_at FROM edit_agent_events '
        f'WHERE run_id=? AND event_type IN ({placeholders}) ORDER BY sequence DESC LIMIT 12',
        (run_id, *kinds),
    ).fetchall()
    activity = []
    for row in reversed(rows):
        payload = json.loads(row['payload_json'])
        if row['event_type'] == 'tool_called':
            tool = payload.get('tool', '')
            # Polling a local job is not a new editorial milestone.
            label = _TOOL_LABELS.get(tool, 'AI 正在操作剪輯工具')
        else:
            tool = None
            label = _EVENT_LABELS[row['event_type']]
        activity.append({'sequence': row['sequence'], 'kind': row['event_type'],
                         'label': label, 'tool': tool, 'created_at': row['created_at']})
    return {'activity': activity, 'updated_at': activity[-1]['created_at'] if activity else None}
