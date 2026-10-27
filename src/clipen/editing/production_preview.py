"""Revision-bound render/review tools; no model-visible filesystem handles."""
import json
import hashlib
import os
import sys
import secrets
from pathlib import Path
import threading
import time

from clipen.projects import ProjectError
from clipen.projects.storage import utc_now
from clipen.rendering import FfmpegPreviewRenderer, PreviewRenderRepository
from clipen.media import MediaPreviewRepository
from clipen.finishing.audio import run_process
from clipen.finishing.renderer import verify_output
from .agent_models import ToolResult


def _event(tools,kind,payload):
    with tools.repository.projects.connect(tools.project_id) as db:
        tools.repository._append_event(db,tools.run_id,kind,payload,utc_now())


def render_recipe_id():
    """Cache executable rendering code as well as the immutable edit payload."""
    from clipen.rendering import renderer, titles, program_components, program_motion, program_styles, program_studio, program_identities, program_subtitles, production_effects
    from . import program_audio, production_media
    digest=hashlib.sha256()
    for module in (renderer,titles,program_components,program_motion,program_styles,program_studio,program_identities,program_subtitles,production_effects,program_audio,production_media):
        digest.update(Path(module.__file__).read_bytes())
    return digest.hexdigest()


def render_preview(tools):
    revision=tools.repository.current_revision(tools.project_id,tools.run_id)
    recipe_id=render_recipe_id()
    validation=tools._production_validate_sequence({}).payload
    issues=validation['render_issues'] if tools._session_value() else validation['issues']
    if issues:raise ProjectError('agent_sequence_invalid','請先使用 validate_sequence 修正時間軸。',status_code=422)
    store=PreviewRenderRepository(tools.repository.projects)
    # The exact revision is the cache key. Polling/reviewing never rerenders it.
    with tools.repository.projects.connect(tools.project_id) as db:
        prior=db.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='production_preview' ORDER BY sequence DESC",(tools.run_id,)).fetchall()
    for row in prior:
        entry=json.loads(row['payload_json'])
        if entry['revision_id']==revision.revision_id and entry.get('recipe_id')==recipe_id:
            saved=store.get(tools.project_id,entry['render_id'])
            if saved['state'] in {'queued','running','succeeded'}:
                return ToolResult({'render':saved,'revision_id':revision.revision_id,'reused':True})
    current=store.current(tools.project_id)
    if current and current['state'] in {'queued','running','cancelling'}:
        raise ProjectError('preview_busy','目前有預覽正在合成，請先等待完成。',status_code=409)
    with tools.repository.projects.connect(tools.project_id) as db:
        db.execute('BEGIN IMMEDIATE')
        run=tools.repository._read_run(db,tools.run_id)
        if not getattr(tools,'_user_edit',False) and run.preview_renders>=run.max_preview_renders:
            raise ProjectError('preview_budget_exhausted','已達本次預覽上限，請使用已完成的版本或回報尚未完成的檢視。',status_code=409)
        if not getattr(tools,'_user_edit',False):db.execute('UPDATE edit_agent_runs SET preview_renders=preview_renders+1 WHERE run_id=?',(tools.run_id,))
        db.commit()
    request_id=('user-preview-'+secrets.token_hex(16)) if getattr(tools,'_user_edit',False) else 'agent-preview-'+tools.run_id+'-'+revision.revision_id+'-'+str(run.preview_renders+1)
    render=store.create(tools.project_id,request_id,revision.revision_id)
    _event(tools,'production_preview',{'render_id':render['render_id'],'revision_id':revision.revision_id,'recipe_id':recipe_id})
    cancel=threading.Event()
    if not hasattr(tools,'_preview_jobs'):tools._preview_jobs={}
    renderer=getattr(tools,'preview_renderer',None) or FfmpegPreviewRenderer(MediaPreviewRepository(tools.repository.projects), hardware_acceleration=sys.platform == "darwin")
    def worker():
        staging=final=None
        def progress(value):
            state=tools.repository.get(tools.project_id,tools.run_id).state
            if (state not in {'queued','running'} and not getattr(tools,'_user_edit',False)) or tools._cancel_event().is_set():cancel.set()
            if store.get(tools.project_id,render['render_id'])['state']=='cancelling':cancel.set()
            store.update_progress(tools.project_id,render['render_id'],value)
        try:
            staging=store.output_path(tools.project_id,render['render_id'],staging=True)
            final=store.output_path(tools.project_id,render['render_id'])
            store.mark_running(tools.project_id,render['render_id'])
            renderer.render(tools.project_id,revision.timeline,staging,cancel,progress)
            if cancel.is_set():raise ProjectError('user_cancelled','已取消預覽。',status_code=409)
            verify_output(staging,revision.timeline,cancel)
            os.replace(staging,final);final.chmod(0o600)
            for suffix in ('.music.wav','.camera.wav','.mix.wav','.mix.json'):
                stem=staging.with_suffix(suffix)
                if stem.is_file():
                    os.replace(stem,final.with_suffix(suffix));final.with_suffix(suffix).chmod(0o600)
            store.complete(tools.project_id,render['render_id'],'previews/'+final.name,final.stat().st_size)
        except Exception as error:
            _event(tools,'production_preview_failed',{'render_id':render['render_id'],'revision_id':revision.revision_id,
                'error_code':getattr(error,'code','agent_render_failed'),'error_type':type(error).__name__,
                'message':getattr(error,'message','Local render failed')})
            if final is not None:
                final.unlink(missing_ok=True)
                for suffix in ('.music.wav','.camera.wav','.mix.wav','.mix.json'):final.with_suffix(suffix).unlink(missing_ok=True)
            store.finish_unsuccessful(tools.project_id,render['render_id'],'cancelled' if cancel.is_set() else 'failed',error_code=getattr(error,'code','agent_render_failed'))
        finally:
            if staging is not None:staging.unlink(missing_ok=True)
            if staging is not None:
                for suffix in ('.music.wav','.camera.wav','.mix.wav','.mix.json'):staging.with_suffix(suffix).unlink(missing_ok=True)
    thread=threading.Thread(target=worker,name='clipen-agent-preview',daemon=True)
    tools._preview_jobs[render['render_id']]=(cancel,thread);thread.start()
    return ToolResult({'render':render,'revision_id':revision.revision_id,'reused':False,'next':'inspect_preview with wait_seconds up to 30; do not call render_preview to poll'})


def inspect_preview(tools,args):
    store=PreviewRenderRepository(tools.repository.projects)
    with tools.repository.projects.connect(tools.project_id) as db:
        entries=[json.loads(r['payload_json']) for r in db.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='production_preview'",(tools.run_id,))]
    entry=next((e for e in entries if e['render_id']==args['render_id']),None)
    if entry is None:raise ProjectError('preview_not_found','預覽不屬於這次剪輯。',status_code=404)
    deadline=time.monotonic()+args.get('wait_seconds',0)
    while True:
        render=store.get(tools.project_id,args['render_id'])
        if render['state'] not in {'queued','running','cancelling'} or time.monotonic()>=deadline:break
        tools._cancel_event().wait(min(.2,max(0,deadline-time.monotonic())))
    current=tools.repository.current_revision(tools.project_id,tools.run_id)
    payload={'render':render,'revision_id':entry['revision_id'],'recipe_id':entry.get('recipe_id'),
             'stale':entry['revision_id']!=current.revision_id or entry.get('recipe_id')!=render_recipe_id(),'coverage':'selected instants only; audio not listened to'}
    if render['state']!='succeeded':return ToolResult(payload)
    path=store.output_path(tools.project_id,args['render_id'],prepare=False)
    if path.is_symlink() or not path.is_file() or path.stat().st_size!=render['size_bytes']:
        raise ProjectError('preview_missing','預覽檔案已遺失或變更。',status_code=409)
    with tools.repository.projects.connect(tools.project_id) as db:
        row=db.execute('SELECT payload_json FROM edit_agent_revisions WHERE revision_id=? AND run_id=?',(entry['revision_id'],tools.run_id)).fetchone()
    timeline=json.loads(row['payload_json']);seconds=timeline['duration_frames']*timeline['frame_rate']['den']/timeline['frame_rate']['num']
    if any(t>=seconds for t in args['offsets_seconds']):raise ProjectError('agent_input_invalid','檢視時間超出預覽範圍。',status_code=422)
    images=[]
    for n,offset in enumerate(args['offsets_seconds']):
        image=tools._workdir('reviews')/(args['render_id']+'-'+str(n)+'-'+str(time.time_ns())+'.png')
        run_process(['ffmpeg','-v','error','-nostdin','-ss',str(offset),'-i',str(path),'-frames:v','1','-vf','scale=640:-2','-threads','1',str(image)],tools._cancel_event(),timeout=30)
        if not image.is_file():raise ProjectError('preview_frame_missing','指定時間沒有可解碼畫面。',status_code=409)
        images.append(image)
    if images:_event(tools,'production_review',{'render_id':args['render_id'],'revision_id':entry['revision_id'],'recipe_id':entry.get('recipe_id'),'offsets_seconds':args['offsets_seconds']})
    return ToolResult({**payload,'sample_offsets_seconds':args['offsets_seconds']},tuple(images))


def finish_review_status(tools):
    from .editorial_review import coverage_targets,text_targets
    revision=tools.repository.current_revision(tools.project_id,tools.run_id)
    with tools.repository.projects.connect(tools.project_id) as db:
        entries=[json.loads(r['payload_json']) for r in db.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='production_review'",(tools.run_id,))]
        reports=[json.loads(r['payload_json']) for r in db.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='editorial_review' ORDER BY sequence",(tools.run_id,))]
    store=PreviewRenderRepository(tools.repository.projects);offsets=[]
    recipe_id=render_recipe_id()
    for entry in entries:
        if entry['revision_id']!=revision.revision_id or entry.get('recipe_id')!=recipe_id:continue
        saved=store.get(tools.project_id,entry['render_id'])
        path=store.output_path(tools.project_id,entry['render_id'],prepare=False)
        if saved['state']=='succeeded' and not path.is_symlink() and path.is_file() and path.stat().st_size==saved['size_bytes']:offsets.extend(entry['offsets_seconds'])
    fps=revision.timeline.frame_rate.numerator/revision.timeline.frame_rate.denominator
    def seen(target):return any(abs(o-target)<.45/fps for o in offsets)
    # Shots and texts accepted in an earlier review and unchanged since need no new stills.
    from .editorial_review import carried_reviews
    carried_shots,carried_texts=carried_reviews(tools,revision,recipe_id)
    missing_shots={identifier:[o for o in samples if not seen(o)] for identifier,samples in coverage_targets(revision.timeline).items() if identifier not in carried_shots}
    missing_shots={k:v for k,v in missing_shots.items() if v}
    missing_texts={k:v for k,v in text_targets(revision.timeline).items() if k not in carried_texts and not seen(v)}
    complete=not missing_shots and not missing_texts
    report=next((r for r in reversed(reports) if r['revision_id']==revision.revision_id and r.get('recipe_id')==recipe_id),None)
    unresolved=[] if report is None else [s['item_id'] for s in report['shots'] if s['decision']=='revise']+[c['item_id'] for c in report['text_checks'] if not c['matches']]
    return {'reviewed_revision':complete and report is not None and not unresolved,'render_coverage_complete':complete,
        'missing_shots':missing_shots,'missing_texts':missing_texts,'editorial_assessment_recorded':report is not None,'unresolved':unresolved,
        'carried_shots':sorted(carried_shots),'carried_texts':sorted(carried_texts),
        'carried_note':'Unchanged shots and texts accepted in an earlier review of this run; review_cut may omit them.',
        'coverage':'start/middle/end rendered stills and text samples; continuous motion and audio not reviewed',
        'assessment_authority':'model_authored','human_review':'pending'}


def cancel_preview(tools,args):
    with tools.repository.projects.connect(tools.project_id) as db:
        entries=[json.loads(r['payload_json']) for r in db.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='production_preview'",(tools.run_id,))]
    if not any(e['render_id']==args['render_id'] for e in entries):raise ProjectError('preview_not_found','預覽不屬於這次剪輯。',status_code=404)
    store=PreviewRenderRepository(tools.repository.projects)
    saved=store.get(tools.project_id,args['render_id'])
    if saved['state'] not in {'queued','running','cancelling'}:return ToolResult({'render':saved})
    store.request_cancel(tools.project_id,args['render_id'])
    job=getattr(tools,'_preview_jobs',{}).get(args['render_id'])
    if job:job[0].set()
    return ToolResult({'render':store.get(tools.project_id,args['render_id'])})
