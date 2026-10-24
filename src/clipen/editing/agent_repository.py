"""Crash-safe persistence for bounded AI editing sessions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from clipen.domain import EditTimeline
from clipen.projects import ProjectError, ProjectRepository
from clipen.projects.models import validate_project_id, validate_request_id
from clipen.projects.storage import command_digest, utc_now

from .agent_models import (
    AgentRevision,
    AgentRun,
    REVISION_ID_PATTERN,
    RUN_ID_PATTERN,
    bounded_text,
    encoded,
    make_id,
)


def _closing_message(value: str | None) -> str | None:
    """Keep the provider's last words to the user short enough to show on the status panel."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    return text if len(text) <= 2000 else text[:1999].rstrip() + "…"


class EditAgentRepository:
    """Persist every agent mutation as an immutable revision and append-only event."""

    def __init__(self, projects: ProjectRepository) -> None:
        self.projects = projects

    def create_run(
        self,
        project_id: str,
        request_id: object,
        *,
        provider: str = "codex",
        max_tool_calls: int = 80,
        max_preview_renders: int = 4,
        creative_direction: dict | None = None,
        production: dict | None = None,
    ) -> tuple[AgentRun, bool]:
        validate_project_id(project_id)
        request = validate_request_id(request_id)
        from .program_direction import validate_direction, PROGRAM_DIRECTION, FULL_PRODUCTION_REQUIREMENTS
        skill = None
        if production is not None:
            if not isinstance(production, dict) or (not {"skill_id", "intent"} <= set(production) or not set(production) <= {"skill_id", "intent", "visual_style_id", "font_style_id", "target_duration_seconds", "music_asset_id", "template_id"}):
                raise ProjectError("production_input_invalid", "請選擇剪輯樣板並填寫影片需求。", status_code=422)
            from clipen.skills import get_skill
            skill = get_skill(production["skill_id"])
            from clipen.skills.visual_styles import style_presets, font_presets
            template=None
            template_supplied='template_id' in production
            font_supplied='font_style_id' in production or template_supplied
            if 'template_id' not in production:
                inferred=next((t for t in skill.get('templates',[]) if t['visual_style_id']==production.get('visual_style_id')),None)
                if inferred is not None:production={**production,'template_id':inferred['template_id']}
            if 'template_id' in production:
                template=next((t for t in skill.get('templates',[]) if t['template_id']==production['template_id']),None)
                if template is None:raise ProjectError('template_unavailable','找不到這套影片模板。',status_code=422)
                if production.get('visual_style_id',template['visual_style_id'])!=template['visual_style_id']:
                    raise ProjectError('template_conflict','畫面風格與選擇的模板不一致，請重新選擇。',status_code=422)
                production={**production,'visual_style_id':template['visual_style_id'],'font_style_id':production.get('font_style_id',template['font_style_id'])}
                # A template is its own skill over the production core.
                skill=get_skill(template['skill']['skill_id'])
                skill['selected_template_id']=template['template_id']
            elif skill.get('template'):
                # The template skill was chosen directly; keep its design brief in the intent.
                template=skill['template']
                skill['selected_template_id']=template['template_id']
            target=production.get('target_duration_seconds',skill.get('default_duration_seconds',60))
            if isinstance(target,bool) or not isinstance(target,int) or not 30<=target<=1200:
                raise ProjectError('production_input_invalid','預計片長必須為 30 到 1200 秒。',status_code=422)
            from .workbench import music_choice
            selected_music=music_choice(self,project_id,production.get("music_asset_id"))
            supplied_style="visual_style_id" in production
            selected,_=style_presets(skill,production.get('visual_style_id'))
            skill['selected_visual_style_id']=selected
            selected_font,_=font_presets(skill,{},production.get('font_style_id'))
            skill['selected_font_style_id']=selected_font
            production = {**({'template_id':template['template_id']} if template_supplied else {}),"skill_id": skill["skill_id"], "intent": bounded_text(production["intent"], "影片需求", 8000), **({"visual_style_id":selected} if supplied_style else {}), **({"font_style_id":selected_font} if font_supplied else {}), **({"target_duration_seconds":target} if "target_duration_seconds" in production else {}), **({"music_asset_id":production["music_asset_id"]} if "music_asset_id" in production else {})}
            creative_direction = {**PROGRAM_DIRECTION, "production_requirements": FULL_PRODUCTION_REQUIREMENTS}
        creative_direction = validate_direction(creative_direction)
        if isinstance(max_tool_calls, bool) or not 1 <= max_tool_calls <= 500:
            raise ValueError("max_tool_calls is invalid")
        if isinstance(max_preview_renders, bool) or not 0 <= max_preview_renders <= 20:
            raise ValueError("max_preview_renders is invalid")
        request_hash = command_digest(
            {
                "provider": provider,
                "max_tool_calls": max_tool_calls,
                "max_preview_renders": max_preview_renders,
                **({"creative_direction":creative_direction} if creative_direction is not None else {}),
                **({"production": production} if production is not None else {}),
            }
        )
        from clipen.provider_registry import implemented_providers
        if provider not in implemented_providers():
            raise ValueError("provider is invalid")
        run_id = make_id("editrun_", [project_id, request])
        now = utc_now()
        with self.projects.connect(project_id) as database:
            try:
                database.execute("BEGIN IMMEDIATE")
                receipt = database.execute(
                    "SELECT command_type,request_hash,response_json "
                    "FROM edit_agent_command_receipts WHERE request_id=?",
                    (request,),
                ).fetchone()
                if receipt is not None:
                    if receipt["command_type"] != "start" or receipt["request_hash"] != request_hash:
                        raise ProjectError(
                            "request_reused",
                            "這次操作的識別資訊已用於不同內容。",
                            status_code=409,
                        )
                    replay_id = json.loads(receipt["response_json"])["run_id"]
                    result = self._read_run(database, replay_id)
                    database.rollback()
                    return result, False
                base = database.execute(
                    "SELECT timelines.* FROM current_timeline current "
                    "JOIN edit_timelines timelines ON timelines.timeline_id=current.timeline_id "
                    "WHERE current.slot=1"
                ).fetchone()
                if base is None and production is None:
                    raise ProjectError(
                        "timeline_required",
                        "請先建立可執行的剪輯版本。",
                        status_code=409,
                    )
                busy = database.execute(
                    "SELECT run_id FROM edit_agent_runs "
                    "WHERE state IN ('queued','running') LIMIT 1"
                ).fetchone()
                if busy is not None:
                    raise ProjectError(
                        "edit_agent_busy",
                        "已有一個 AI 剪輯工作正在進行。",
                        status_code=409,
                    )
                sources = None
                if production is not None:
                    from clipen.domain import Rational, Track, TrackKind
                    from .production_media import source_rows
                    sources = source_rows(self.projects, project_id)
                    inventory = database.execute("SELECT run_id FROM current_inventory WHERE slot=1").fetchone()
                    if inventory is None or not any(s["kind"] == "video" for s in sources):
                        raise ProjectError("inventory_required", "請先檢查素材，至少需要一段可用影片。", status_code=409)
                    timeline = EditTimeline(
                        make_id("production_", run_id), project_id, None, None, None,
                        Rational(30, 1), 1280, 720, 0, (Track("video-main", TrackKind.VIDEO, ()),),
                        production_session_id=run_id,
                    )
                    payload = timeline.to_payload()
                else:
                    payload = json.loads(base["payload_json"])
                    timeline = EditTimeline.from_payload(payload)
                digest = hashlib.sha256(encoded(payload).encode("utf-8")).hexdigest()
                revision_id = make_id("editrev_", [run_id, 0, digest])
                database.execute(
                    "INSERT INTO edit_agent_runs "
                    "(run_id,project_id,request_id,base_timeline_id,state,phase,"
                    "max_tool_calls,max_preview_renders,current_revision_id,provider,created_at) "
                    "VALUES(?,?,?,?,'queued','ready',?,?,?,?,?)",
                    (
                        run_id,
                        project_id,
                        request,
                        None if production is not None else timeline.timeline_id,
                        max_tool_calls,
                        max_preview_renders,
                        revision_id,
                        provider,
                        now,
                    ),
                )
                database.execute(
                    "INSERT INTO edit_agent_revisions "
                    "(revision_id,run_id,ordinal,parent_revision_id,payload_digest,"
                    "payload_json,summary,created_at) VALUES(?,?,0,NULL,?,?,?,?)",
                    (revision_id, run_id, digest, encoded(payload), "空白製作草稿" if production else "原始剪輯版本", now),
                )
                if production is not None:
                    database.execute("INSERT INTO production_sessions VALUES(?,?,?,?,?,?)", (
                        run_id, encoded({**({'template':template} if template else {}),"authority": "user", "brief": production["intent"], "allow_local_model_downloads": True, "visual_style_id":selected, "font_style_id":selected_font, "target_duration_seconds":target, "music_choice":selected_music}),
                        encoded(skill), encoded(sources), inventory["run_id"], now,
                    ))
                from .program_reference import latest_reference
                reference=latest_reference(database) if creative_direction else None
                self._append_event(database, run_id, "run_created", {"revision_id": revision_id,
                    **({"creative_direction":creative_direction} if creative_direction is not None else {}),
                    **({'program_reference':reference} if reference else {})}, now)
                database.execute(
                    "INSERT INTO edit_agent_command_receipts VALUES(?,?,?,?,?)",
                    (request, "start", request_hash, encoded({"run_id": run_id}), now),
                )
                result = self._read_run(database, run_id)
                database.commit()
                return result, True
            except ProjectError:
                database.rollback()
                raise
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, sqlite3.Error) as error:
                database.rollback()
                raise ProjectError(
                    "edit_agent_storage_failed",
                    "Clipen 無法建立 AI 剪輯工作。",
                    status_code=500,
                ) from error

    def production_session(self, project_id: str, run_id: str) -> dict | None:
        self._validate_run_id(run_id)
        with self.projects.connect(project_id) as db:
            row = db.execute("SELECT * FROM production_sessions WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return {"run_id": run_id, "intent": json.loads(row["intent_json"]),
                "skill": json.loads(row["skill_json"]), "sources": json.loads(row["sources_json"]),
                "inventory_run_id": row["inventory_run_id"]}

    def creative_direction(self, project_id: str, run_id: str) -> dict:
        self._validate_run_id(run_id)
        with self.projects.connect(project_id) as db:
            row=db.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='run_created' ORDER BY sequence LIMIT 1",(run_id,)).fetchone()
        return {} if row is None else json.loads(row['payload_json']).get('creative_direction',{})

    def request_refinement(self, project_id, run_id, request_id, expected_revision_id, instruction, music_asset_id=None, expected_references_version=None, provider=None):
        from .refinement_references import public_state, references
        from .workbench import music_choice
        normalized=self._validate_run_id(run_id)
        request=validate_request_id(request_id)
        instruction=bounded_text(instruction,'修改需求',8000)
        choice=music_choice(self,project_id,music_asset_id)
        if provider is not None:
            from clipen.provider_registry import get_provider
            get_provider(provider)
        request_hash=command_digest({'run_id':normalized,'expected_revision_id':expected_revision_id,'instruction':instruction,'music_asset_id':music_asset_id,'expected_references_version':expected_references_version,
                                     **({'provider':provider} if provider is not None else {})})
        with self.projects.connect(project_id) as db:
            db.execute('BEGIN IMMEDIATE')
            receipt=db.execute('SELECT * FROM edit_agent_command_receipts WHERE request_id=?',(request,)).fetchone()
            if receipt:
                if receipt['command_type']!='refine' or receipt['request_hash']!=request_hash:
                    raise ProjectError('request_reused','操作識別已用於其他修改。',status_code=409)
                return self._read_run(db,normalized),False
            run=self._read_run(db,normalized)
            latest=db.execute('SELECT run_id FROM edit_agent_runs ORDER BY created_at DESC LIMIT 1').fetchone()
            if latest['run_id']!=normalized or db.execute("SELECT 1 FROM edit_agent_runs WHERE state IN ('queued','running')").fetchone():
                raise ProjectError('edit_agent_busy','請先停止目前的 AI 製作。',status_code=409)
            if run.current_revision_id!=expected_revision_id:
                raise ProjectError('revision_conflict','影片已更新，請重新讀取後提出修改。',status_code=409)
            session=db.execute('SELECT intent_json FROM production_sessions WHERE run_id=?',(normalized,)).fetchone()
            if session is None:raise ProjectError('production_required','這個版本不支援持續修訂。',status_code=422)
            intent=json.loads(session['intent_json'])
            if expected_references_version is not None and (isinstance(expected_references_version,bool) or not isinstance(expected_references_version,int) or references(intent)['version'] != expected_references_version):
                raise ProjectError('reference_conflict','參考註記已更新，請重新讀取並確認後再送出。',status_code=409)
            reference_snapshot=public_state(intent,self._read_revision(db,run.current_revision_id).timeline)
            intent['latest_revision_request']={'authority':'user','instruction':instruction,'base_revision_id':expected_revision_id,'created_at':utc_now(),'references':reference_snapshot}
            if choice is not None:intent['music_choice']=choice
            db.execute('UPDATE production_sessions SET intent_json=? WHERE run_id=?',(encoded(intent),normalized))
            self._append_event(db,normalized,'refinement_requested',{'instruction':instruction,'references':reference_snapshot,'base_revision_id':expected_revision_id,'previous_budget':run.to_public()['budget'],'previous_attempt_id':run.provider_attempt_id,'music_choice':intent.get('music_choice')},utc_now())
            # A revision is a new request from the user, so the AI Clipen runs with
            # now may take over the draft; it spends its own quota from a fresh budget.
            # Resuming an interrupted attempt never switches (see resume_stopped).
            if provider is not None and provider!=run.provider:
                self._append_event(db,normalized,'provider_switched',{'from':run.provider,'to':provider},utc_now())
                db.execute('UPDATE edit_agent_runs SET provider=? WHERE run_id=?',(provider,normalized))
            db.execute("UPDATE edit_agent_runs SET state='queued',phase='ready',tool_calls=0,preview_renders=0,max_tool_calls=500,max_preview_renders=6,error_code=NULL,provider_attempt_id=NULL,usage_json=NULL,completed_at=NULL WHERE run_id=?",(normalized,))
            db.execute('INSERT INTO edit_agent_command_receipts VALUES(?,?,?,?,?)',(request,'refine',request_hash,encoded({'run_id':normalized}),utc_now()))
            result=self._read_run(db,normalized);db.commit();return result,True

    # Stops that leave a valid draft and say nothing about the draft itself: the
    # wall clock ran out, the provider account hit its usage limit, or the
    # provider ended its turn early. Resuming continues the same revision and
    # budget once the user asks again. A draft blocked by delivery checks needs
    # a revision request instead, since the same instructions hit the same checks.
    RESUMABLE_ERRORS = frozenset({'provider_timed_out', 'provider_usage_limit', 'provider_stopped_early'})

    def resume_stopped(self, project_id: str, run_id: object) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._read_run(db, normalized)
            latest = db.execute('SELECT run_id FROM edit_agent_runs ORDER BY created_at DESC LIMIT 1').fetchone()
            if latest is None or latest['run_id'] != normalized:
                raise ProjectError('edit_agent_resume_invalid', '已有較新的製作工作，請使用最新工作。', status_code=409)
            if db.execute("SELECT 1 FROM edit_agent_runs WHERE state IN ('queued','running')").fetchone():
                raise ProjectError('edit_agent_busy', '目前已有 AI 製作工作。', status_code=409)
            if run.state != 'failed' or run.error_code not in self.RESUMABLE_ERRORS:
                raise ProjectError('edit_agent_resume_invalid', '只能接續因 AI 逾時、額度不足或提早結束而停止的工作。', status_code=409)
            if not db.execute('SELECT 1 FROM production_sessions WHERE run_id=?', (normalized,)).fetchone():
                raise ProjectError('edit_agent_resume_invalid', '這份工作不支援接續製作。', status_code=409)
            if run.tool_calls >= run.max_tool_calls or run.preview_renders >= run.max_preview_renders:
                raise ProjectError('edit_agent_budget_exhausted', '這份工作的操作或輸出次數已用完。', status_code=409)
            # The run row only keeps the latest attempt's usage; keep what the
            # stopped attempt already spent in the append-only event log.
            previous_usage = db.execute('SELECT usage_json FROM edit_agent_runs WHERE run_id=?', (normalized,)).fetchone()['usage_json']
            self._append_event(db, normalized, 'run_resumed', {'previous_attempt_id':run.provider_attempt_id,
                'previous_error_code':run.error_code,
                'previous_usage':None if previous_usage is None else json.loads(previous_usage)}, utc_now())
            db.execute("UPDATE edit_agent_runs SET state='queued',phase='queued',error_code=NULL,provider_attempt_id=NULL,usage_json=NULL,completed_at=NULL WHERE run_id=?", (normalized,))
            result = self._read_run(db, normalized)
            db.commit()
            return result

    resume_timed_out = resume_stopped

    def delivery_blockers(self, project_id: str, run_id: object) -> tuple[str, ...] | None:
        """Issue codes from the latest delivery check of the current revision; None when it was never checked."""
        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as db:
            run = self._read_run(db, normalized)
            rows = db.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='sequence_validation' ORDER BY sequence DESC LIMIT 30", (normalized,)).fetchall()
        for row in rows:
            value = json.loads(row['payload_json'])
            if value['revision_id'] == run.current_revision_id:
                if value['valid']:
                    return ()
                codes = [issue['code'] for issue in value.get('issues', []) + value.get('delivery_issues', [])]
                return tuple(dict.fromkeys(codes)) or ('sequence_invalid',)
        return None

    def activate(self, project_id: str, run_id: object) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            if run.state == "queued":
                now = utc_now()
                database.execute(
                    "UPDATE edit_agent_runs SET state='running',phase='editing',started_at=? "
                    "WHERE run_id=?",
                    (now, normalized),
                )
                self._append_event(database, normalized, "run_started", {}, now)
            elif run.state != "running":
                database.rollback()
                raise ProjectError(
                    "edit_agent_finished",
                    "這次 AI 剪輯工作已經結束。",
                    status_code=409,
                )
            result = self._read_run(database, normalized)
            database.commit()
            return result

    def consume_tool_call(self, project_id: str, run_id: object, tool_name: str) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        bounded_text(tool_name, "工具名稱", 80)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            if run.state != "running":
                database.rollback()
                raise ProjectError("edit_agent_not_running", "AI 剪輯工作並未執行。", status_code=409)
            if run.tool_calls >= run.max_tool_calls:
                now = utc_now()
                database.execute(
                    "UPDATE edit_agent_runs SET state='interrupted',phase='budget_exhausted',"
                    "error_code='tool_budget_exhausted',completed_at=? WHERE run_id=?",
                    (now, normalized),
                )
                self._append_event(database, normalized, "budget_exhausted", {}, now)
                database.commit()
                raise ProjectError(
                    "tool_budget_exhausted",
                    "這次 AI 剪輯已達工具呼叫上限。",
                    status_code=409,
                )
            database.execute(
                "UPDATE edit_agent_runs SET tool_calls=tool_calls+1 WHERE run_id=?",
                (normalized,),
            )
            now = utc_now()
            self._append_event(database, normalized, "tool_called", {"tool": tool_name}, now)
            result = self._read_run(database, normalized)
            database.commit()
            return result

    def current_revision(self, project_id: str, run_id: object) -> AgentRevision:
        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            run = self._read_run(database, normalized)
            return self._read_revision(database, run.current_revision_id)

    def save_revision(
        self,
        project_id: str,
        run_id: object,
        timeline: EditTimeline,
        summary: object,
        *,
        expected_revision_id: str | None = None,
        allow_user_edit: bool = False,
    ) -> tuple[AgentRevision, bool]:
        normalized = self._validate_run_id(run_id)
        normalized_summary = bounded_text(summary, "修改摘要", 500)
        payload = timeline.to_payload()
        digest = hashlib.sha256(encoded(payload).encode("utf-8")).hexdigest()
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            if expected_revision_id is not None and expected_revision_id != run.current_revision_id:
                raise ProjectError("revision_conflict", "作品版本已更新，請重新讀取目前版本後再修改。", status_code=409)
            if timeline.project_id != project_id or timeline.production_session_id != (
                normalized if run.base_timeline_id is None else None
            ):
                raise ProjectError("revision_owner_mismatch", "剪輯版本不屬於這次製作。", status_code=409)
            if run.state != "running" and not (allow_user_edit and run.state not in {"queued", "running"}):
                database.rollback()
                raise ProjectError("edit_agent_not_running", "AI 剪輯工作並未執行。", status_code=409)
            if allow_user_edit and database.execute("SELECT 1 FROM edit_agent_runs WHERE state IN ('queued','running')").fetchone():
                raise ProjectError('edit_agent_busy','AI 正在修改作品，請稍後再編輯。',status_code=409)
            existing = database.execute(
                "SELECT revision_id FROM edit_agent_revisions WHERE run_id=? AND payload_digest=?",
                (normalized, digest),
            ).fetchone()
            if existing is not None:
                database.execute(
                    "UPDATE edit_agent_runs SET current_revision_id=?,phase=? WHERE run_id=?",
                    (existing["revision_id"], "user_edit" if allow_user_edit else run.phase, normalized),
                )
                result = self._read_revision(database, existing["revision_id"])
                database.commit()
                return result, False
            ordinal = database.execute(
                "SELECT COALESCE(MAX(ordinal),-1)+1 value FROM edit_agent_revisions WHERE run_id=?",
                (normalized,),
            ).fetchone()["value"]
            revision_id = make_id("editrev_", [normalized, ordinal, digest])
            now = utc_now()
            database.execute(
                "INSERT INTO edit_agent_revisions VALUES(?,?,?,?,?,?,?,?)",
                (
                    revision_id,
                    normalized,
                    ordinal,
                    run.current_revision_id,
                    digest,
                    encoded(payload),
                    normalized_summary,
                    now,
                ),
            )
            database.execute(
                "UPDATE edit_agent_runs SET current_revision_id=?,phase=? WHERE run_id=?",
                (revision_id, "user_edit" if allow_user_edit else "editing", normalized),
            )
            self._append_event(
                database,
                normalized,
                "revision_created",
                {"revision_id": revision_id, "summary": normalized_summary},
                now,
            )
            result = self._read_revision(database, revision_id)
            database.commit()
            return result, True

    def undo(self, project_id: str, run_id: object, *, expected_revision_id: str | None = None) -> AgentRevision:
        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            if expected_revision_id is not None and expected_revision_id!=run.current_revision_id:
                raise ProjectError('revision_conflict','作品版本已更新，請重新讀取。',status_code=409)
            current = self._read_revision(database, run.current_revision_id)
            if current.parent_revision_id is None:
                database.rollback()
                raise ProjectError("undo_unavailable", "目前已是最初版本。", status_code=409)
            parent = self._read_revision(database, current.parent_revision_id)
            database.execute(
                "UPDATE edit_agent_runs SET current_revision_id=? WHERE run_id=?",
                (parent.revision_id, normalized),
            )
            self._append_event(
                database,
                normalized,
                "revision_selected",
                {"revision_id": parent.revision_id},
                utc_now(),
            )
            database.commit()
            return parent

    def finish(self, project_id: str, run_id: object, summary: object, *, expected_revision_id: str | None = None) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        normalized_summary = bounded_text(summary, "完成摘要", 1_000)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            if expected_revision_id is not None and expected_revision_id!=run.current_revision_id:
                raise ProjectError('revision_conflict','作品版本已更新，請重新檢視。',status_code=409)
            if run.state != "running":
                database.rollback()
                raise ProjectError("edit_agent_not_running", "AI 剪輯工作並未執行。", status_code=409)
            now = utc_now()
            database.execute(
                "UPDATE edit_agent_runs SET state='succeeded',phase='completed',"
                "final_revision_id=current_revision_id,completed_at=? WHERE run_id=?",
                (now, normalized),
            )
            self._append_event(database, normalized, "run_finished", {"summary": normalized_summary}, now)
            result = self._read_run(database, normalized)
            database.commit()
            return result

    def record_provider_outcome(
        self,
        project_id: str,
        run_id: object,
        attempt_id: str,
        *,
        usage: Mapping[str, int] | None,
        error_code: str | None = None,
        message: str | None = None,
    ) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        bounded_text(attempt_id, "Provider attempt", 128)
        if error_code is not None:
            bounded_text(error_code, "Provider 錯誤", 128)
        usage_json = None
        if usage is not None:
            expected = {"input_tokens", "cached_input_tokens", "output_tokens"}
            if set(usage) != expected or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in usage.values()
            ):
                raise ValueError("provider usage is invalid")
            if usage["cached_input_tokens"] > usage["input_tokens"]:
                raise ValueError("provider cached usage exceeds input usage")
            usage_json = encoded(dict(usage))
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            now = utc_now()
            if error_code is not None and run.state in {"queued", "running"}:
                database.execute(
                    "UPDATE edit_agent_runs SET state='failed',phase='failed',error_code=?,"
                    "provider_attempt_id=?,usage_json=?,completed_at=? WHERE run_id=?",
                    (error_code, attempt_id, usage_json, now, normalized),
                )
            else:
                database.execute(
                    "UPDATE edit_agent_runs SET provider_attempt_id=?,usage_json=? WHERE run_id=?",
                    (attempt_id, usage_json, normalized),
                )
            self._append_event(
                database,
                normalized,
                "provider_finished" if error_code is None else "provider_failed",
                {"attempt_id": attempt_id, "usage": usage, "error_code": error_code,
                 "message": _closing_message(message)},
                now,
            )
            result = self._read_run(database, normalized)
            database.commit()
            return result

    def get(self, project_id: str, run_id: object) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            return self._read_run(database, normalized)

    def latest(self, project_id: str) -> AgentRun | None:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as database:
            row = database.execute(
                "SELECT run_id FROM edit_agent_runs ORDER BY created_at DESC,rowid DESC LIMIT 1"
            ).fetchone()
            return None if row is None else self._read_run(database, row["run_id"])

    def latest_successful(self, project_id: str) -> AgentRun | None:
        """Return the newest published result, preserving it across failed retries."""

        validate_project_id(project_id)
        with self.projects.connect(project_id) as database:
            row = database.execute(
                "SELECT run_id FROM edit_agent_runs "
                "WHERE state='succeeded' AND final_revision_id IS NOT NULL "
                "ORDER BY completed_at DESC,rowid DESC LIMIT 1"
            ).fetchone()
            return None if row is None else self._read_run(database, row["run_id"])

    def final_revision(self, project_id: str, run_id: object) -> AgentRevision:
        """Return the immutable result only after the run finished successfully."""

        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            run = self._read_run(database, normalized)
            if run.state != "succeeded" or run.final_revision_id is None:
                raise ProjectError(
                    "edit_agent_result_not_ready",
                    "AI 剪輯版本尚未完成。",
                    status_code=409,
                )
            return self._read_revision(database, run.final_revision_id)

    def cancel(self, project_id: str, run_id: object) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            if run.state in {"succeeded", "failed", "cancelled", "interrupted"}:
                database.rollback()
                return run
            now = utc_now()
            database.execute(
                "UPDATE edit_agent_runs SET state='cancelled',phase='cancelled',"
                "error_code='user_cancelled',completed_at=? WHERE run_id=?",
                (now, normalized),
            )
            self._append_event(database, normalized, "run_cancelled", {}, now)
            result = self._read_run(database, normalized)
            database.commit()
            return result

    def interrupt_incomplete(self, project_id: str) -> int:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute(
                "SELECT run_id FROM edit_agent_runs WHERE state IN ('queued','running')"
            ).fetchall()
            now = utc_now()
            for row in rows:
                database.execute(
                    "UPDATE edit_agent_runs SET state='interrupted',phase='interrupted',"
                    "error_code='controller_stopped',completed_at=? WHERE run_id=?",
                    (now, row["run_id"]),
                )
                self._append_event(
                    database, row["run_id"], "run_interrupted", {}, now
                )
            database.commit()
            return len(rows)

    def interrupt_for_shutdown(self, project_id: str, run_id: object) -> AgentRun:
        """Record Controller shutdown after its provider process has been stopped."""

        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            may_interrupt = run.state in {"queued", "running"} or (
                run.state == "failed" and run.error_code == "provider_cancelled"
            )
            if not may_interrupt:
                database.rollback()
                return run
            now = utc_now()
            database.execute(
                "UPDATE edit_agent_runs SET state='interrupted',phase='interrupted',"
                "error_code='controller_stopped',completed_at=? WHERE run_id=?",
                (now, normalized),
            )
            self._append_event(database, normalized, "run_interrupted", {}, now)
            result = self._read_run(database, normalized)
            database.commit()
            return result

    @staticmethod
    def _validate_run_id(value: object) -> str:
        if not isinstance(value, str) or RUN_ID_PATTERN.fullmatch(value) is None:
            raise ProjectError("edit_agent_not_found", "找不到 AI 剪輯工作。", status_code=404)
        return value

    @staticmethod
    def _append_event(database, run_id: str, event_type: str, payload: object, now: str) -> None:
        # Background preview completion can race a foreground tool call. Keep
        # sequence allocation and insertion in one write statement, including
        # callers that intentionally use SQLite autocommit.
        database.execute(
            "INSERT INTO edit_agent_events(run_id,sequence,event_type,payload_json,created_at) "
            "SELECT ?,COALESCE(MAX(sequence),0)+1,?,?,? FROM edit_agent_events WHERE run_id=?",
            (run_id, event_type, encoded(payload), now, run_id),
        )

    @staticmethod
    def _read_run(database, run_id: str) -> AgentRun:
        row = database.execute("SELECT * FROM edit_agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ProjectError("edit_agent_not_found", "找不到 AI 剪輯工作。", status_code=404)
        try:
            usage = None if row["usage_json"] is None else json.loads(row["usage_json"])
        except (json.JSONDecodeError, TypeError) as error:
            raise ProjectError("storage_corrupt", "Clipen 無法讀取 AI 用量紀錄。", status_code=500) from error
        return AgentRun(
            row["run_id"], row["project_id"], row["base_timeline_id"], row["state"],
            row["phase"], row["max_tool_calls"], row["tool_calls"],
            row["max_preview_renders"], row["preview_renders"], row["current_revision_id"],
            row["final_revision_id"], row["provider"], row["provider_attempt_id"], usage,
            row["error_code"], row["created_at"],
            row["started_at"], row["completed_at"],
        )

    @staticmethod
    def _read_revision(database, revision_id: str) -> AgentRevision:
        if not isinstance(revision_id, str) or REVISION_ID_PATTERN.fullmatch(revision_id) is None:
            raise ProjectError("edit_revision_not_found", "找不到剪輯修改版本。", status_code=404)
        row = database.execute(
            "SELECT * FROM edit_agent_revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
        if row is None:
            raise ProjectError("edit_revision_not_found", "找不到剪輯修改版本。", status_code=404)
        try:
            payload = json.loads(row["payload_json"])
            if hashlib.sha256(encoded(payload).encode('utf-8')).hexdigest()!=row['payload_digest']:
                raise ValueError('revision content digest mismatch')
            timeline = EditTimeline.from_payload(payload)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProjectError("storage_corrupt", "Clipen 無法讀取剪輯修改版本。", status_code=500) from error
        return AgentRevision(
            row["revision_id"], row["run_id"], row["ordinal"], row["parent_revision_id"],
            timeline, row["summary"], row["created_at"],
        )
