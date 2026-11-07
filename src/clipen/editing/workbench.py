"""The review workbench: read the current film, restore a version, reword graphics, render and export it."""
import threading
import json
from clipen.projects import ProjectError, DirectoryReference, DirectoryRole
from clipen.projects.storage import utc_now
from .agent_tools import EditAgentTools
from .production_media import AgentAudioStore
from .refinement_references import public_state, sent

# Changes go through review notes to the AI; the user picks which version is current and may reword graphics.
USER_TOOLS = frozenset({'restore_revision','set_component_text'})

def idle(repository, project_id, run_id):
    run=repository.get(project_id,run_id)
    latest=repository.latest(project_id)
    if latest is None or latest.run_id!=run_id or run.state in {'queued','running'}:
        raise ProjectError('edit_agent_busy','請先停止 AI 製作，再還原或輸出目前版本。',status_code=409)
    if repository.production_session(project_id,run_id) is None:
        raise ProjectError('production_required','這個版本不是可編輯的影片製作。',status_code=409)
    return run

def tools_for(repository, project_id, run_id):
    tools=EditAgentTools(repository,project_id,run_id)
    tools._user_edit=True
    return tools

def state(repository, project_id, run_id):
    revision=repository.current_revision(project_id,run_id)
    from .versions import versions
    with repository.projects.connect(project_id) as db:
        # One entry per complete edit; the AI's per-step revisions stay internal.
        history=versions(db,run_id,repository.get(project_id,run_id).provider)
        for version in history:
            row=db.execute('SELECT payload_json FROM edit_agent_revisions WHERE revision_id=? AND run_id=?',(version['revision_id'],run_id)).fetchone()
            if row:
                value=json.loads(row['payload_json']);rate=value['frame_rate']
                version['duration_seconds']=value['duration_frames']*rate['den']/rate['num']
            preview=db.execute("SELECT render_id FROM preview_renders WHERE timeline_id=? AND state='succeeded' ORDER BY created_at DESC LIMIT 1",(version['revision_id'],)).fetchone()
            version['preview_url']=f"/api/projects/{project_id}/preview/{preview['render_id']}/media" if preview else None
    intent=(repository.production_session(project_id,run_id) or {}).get('intent',{})
    return {'revision':revision.to_public(include_timeline=True),'history':history,'references':public_state(intent,revision.timeline),'sent_notes':sent(intent),'audio_assets':AgentAudioStore(repository.projects,project_id).public_assets()}

def edit(repository, project_id, run_id, name, arguments, expected, mutation_id):
    idle(repository,project_id,run_id)
    if name not in USER_TOOLS:
        raise ProjectError('tool_not_allowed','請用審片注記交給 AI 修改；這裡只能還原版本或改元件文字。',status_code=422)
    tools=tools_for(repository,project_id,run_id)
    try:
        result=tools.call(name,{**arguments,'expected_revision_id':expected,'mutation_id':mutation_id})
        return result.payload
    finally:tools.close_production()

def import_music(repository, project_id, path, display_name):
    from clipen.finishing.audio import inspect_library
    store=AgentAudioStore(repository.projects,project_id)
    directory=path.parent;info=directory.stat()
    reference=DirectoryReference(DirectoryRole.SOURCE,str(directory),directory.name,'使用者指定配樂',info.st_dev,info.st_ino,utc_now()).to_storage()
    assets,issues=inspect_library('music',reference,threading.Event(),lambda _:None)
    if len(assets)!=1:
        raise ProjectError('music_invalid','音樂檔無法解碼或沒有可用聲音。',status_code=422)
    asset=assets[0];asset['name']=display_name
    asset['provenance']={'source':'user_upload','title':display_name,'license':'user_supplied','rights_verified':False,'attribution':'使用者提供；系統未驗證授權。','changes':'將依剪輯需要截取、調整音量及淡入淡出。'}
    store.add(assets,reference)
    return next(a for a in store.public_assets() if a['asset_id']==asset['asset_id'])

def music_choice(repository, project_id, asset_id):
    if asset_id is None:return None
    asset=next((a for a in AgentAudioStore(repository.projects,project_id).public_assets() if a['asset_id']==asset_id and a['role']=='music'),None)
    if asset is None:raise ProjectError('music_missing','指定的配樂不在本作品，請重新選擇。',status_code=422)
    return {'mode':'user','asset_id':asset_id,'name':asset['name']}
