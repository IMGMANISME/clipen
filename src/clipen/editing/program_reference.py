"""Human-selected style reference, frozen per run; the agent cannot approve itself."""
import json
from clipen.domain import TrackKind
from clipen.projects import ProjectError
from clipen.projects.storage import utc_now
from .production_media import load_text_styles
from .program_story import read_episode


def latest_reference(database):
    row=database.execute("SELECT payload_json FROM edit_agent_events WHERE event_type='user_program_reference' ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
    return None if row is None else json.loads(row['payload_json'])


def reference_for(repository,project_id,run_id):
    repository.get(project_id,run_id)
    with repository.projects.connect(project_id) as db:
        row=db.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='run_created' ORDER BY sequence LIMIT 1",(run_id,)).fetchone()
    return None if row is None else json.loads(row['payload_json']).get('program_reference')


def accept_reference(repository,project_id,run_id,revision_id):
    run=repository.get(project_id,run_id)
    if run.state!='succeeded' or run.final_revision_id!=revision_id:
        raise ProjectError('program_reference_invalid','只能將已完成、指定版本的節目樣片設為基準。',status_code=409)
    if repository.creative_direction(project_id,run_id).get('format')!='travel_program':
        raise ProjectError('program_reference_invalid','這個版本沒有節目格式與字幕來源。',status_code=422)
    timeline=repository.final_revision(project_id,run_id).timeline
    episode=read_episode(repository.projects,project_id,run_id,timeline)
    if episode is None:raise ProjectError('program_reference_invalid','這個版本缺少可對應的節目規劃。',status_code=409)
    styles=load_text_styles(repository.projects,project_id)
    examples=[{'text':i.text,'style':{k:v for k,v in styles.get(i.style_id,{}).items() if k in {'kind','delivery','font_size_ratio','emphasis','motion'}}}
              for t in timeline.tracks if t.kind is TrackKind.TITLE for i in t.items][:12]
    value={'authority':'user_selected_style_reference','run_id':run_id,'revision_id':revision_id,
        'scope':'Programme style and storytelling direction only; not verified dialogue, audio or independent quality approval.',
        'episode':episode,'subtitle_examples':examples,
        'source_version_ids':sorted({i.source.span.version_id for t in timeline.tracks if t.kind is TrackKind.VIDEO for i in t.items}),
        'principles':['以人物口語與互動推進一個事件','期待、體驗與回應要由來源支撐','不使用獨立字卡','字幕短句分頁，重點與動態服務當下內容','保留人聲，配樂依段落進退；不照抄參考片的台詞或剪點']}
    with repository.projects.connect(project_id) as db:
        db.execute('BEGIN IMMEDIATE')
        if latest_reference(db)!=value:repository._append_event(db,run_id,'user_program_reference',value,utc_now())
        db.commit()
    return value
