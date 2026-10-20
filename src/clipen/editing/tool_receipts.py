"""Durable mutation receipts; uncertain interrupted calls are never silently replayed."""
import hashlib
import json

from clipen.projects import ProjectError
from clipen.projects.storage import utc_now
from .agent_models import ToolResult, bounded_text, encoded

TIMELINE_MUTATIONS = frozenset({
    'replace_sequence','add_title','set_clip_effect','undo_revision','finish_edit',
    'set_canvas','replace_clip_source','add_clip_title','replace_subtitles','edit_text',
    'configure_text_style','remove_item','trim_clip','reorder_clips','split_clip','insert_clip',
    'set_source_audio','set_color_grade','set_mosaic','set_transition','set_fade',
    'remove_clip_effect','place_audio','remove_audio','restore_revision',
    'add_program_component','update_program_component','remove_program_component',
})


def begin_receipt(tools, name, arguments, expected_revision_id, mutation_id):
    mutation_id=bounded_text(mutation_id,'操作識別',160)
    argument_hash=hashlib.sha256(encoded({'arguments':arguments,'expected_revision_id':expected_revision_id}).encode()).hexdigest()
    with tools.repository.projects.connect(tools.project_id) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM production_tool_receipts WHERE run_id=? AND mutation_id=?',(tools.run_id,mutation_id)).fetchone()
        if row:
            if row['tool_name']!=name or row['arguments_hash']!=argument_hash:
                raise ProjectError('request_reused','這個操作識別已用於其他參數。',status_code=409)
            if row['state']=='started':
                raise ProjectError('tool_outcome_unknown','先前操作在回報前中斷；請讀取目前版本與紀錄確認結果，不要直接重送。',status_code=409)
            payload=json.loads(row['result_json'])
            if row['state']=='failed':
                raise ProjectError(payload['error'],payload['message'],status_code=409)
            db.rollback()
            return ToolResult({**payload,'replayed':True})
        current=tools.repository._read_run(db,tools.run_id)
        if expected_revision_id is not None and expected_revision_id!=current.current_revision_id:
            raise ProjectError('revision_conflict','作品版本已更新，請重新讀取後修改。',status_code=409)
        if current.state!='running':
            raise ProjectError('edit_agent_not_running','AI 製作目前未在執行。',status_code=409)
        db.execute('INSERT INTO production_tool_receipts VALUES(?,?,?,?,?,?,?)',
                   (tools.run_id,mutation_id,name,argument_hash,'started',None,utc_now()))
        db.commit()
    return None


def finish_receipt(tools, mutation_id, *, result=None, error=None):
    payload=dict(result.payload) if error is None else {'error':getattr(error,'code','tool_failed'),
        'message':getattr(error,'message','這次操作未完成，請查看作品版本與紀錄。')}
    with tools.repository.projects.connect(tools.project_id) as db:
        db.execute("UPDATE production_tool_receipts SET state=?,result_json=? WHERE run_id=? AND mutation_id=? AND state='started'",
                   ('completed' if error is None else 'failed',encoded(payload),tools.run_id,mutation_id))
