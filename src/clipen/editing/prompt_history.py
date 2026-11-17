"""Durable user prompts, including older production and refinement requests."""

import hashlib
import json

from clipen.projects import ProjectError
from clipen.projects.models import validate_request_id
from clipen.projects.storage import utc_now

from .agent_models import bounded_text, encoded


def save(repository, project_id, request_id, kind, text):
    repository.projects.get_project(project_id)
    request_id = validate_request_id(request_id)
    if kind not in {"production", "refinement"}:
        raise ProjectError("prompt_invalid", "需求類型無效。", status_code=422)
    text = bounded_text(text, "Prompt", 8000)
    identifier = "prompt_" + hashlib.sha256(request_id.encode()).hexdigest()[:32]
    value = {"prompt_id": identifier, "kind": kind, "text": text, "source": "saved"}
    with repository.projects.connect(project_id) as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute(
            "SELECT payload_json,created_at FROM operation_events WHERE event_id=?", (identifier,)
        ).fetchone()
        if prior:
            if json.loads(prior["payload_json"]) != value:
                raise ProjectError("request_reused", "儲存識別已用於其他需求。", status_code=409)
            return {**value, "created_at": prior["created_at"]}
        now = utc_now()
        db.execute(
            "INSERT INTO operation_events(event_id,project_id,operation_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (identifier, project_id, request_id, "production_prompt_saved", encoded(value), now),
        )
        db.commit()
    return {**value, "created_at": now}


def history(repository, project_id=None, offset=0, limit=30):
    if project_id:
        projects = [repository.projects.get_project(project_id)]
    else:
        projects = repository.projects.list_projects()
    entries = []
    for project in projects:
        with repository.projects.connect(project.project_id) as db:
            for row in db.execute("SELECT run_id,intent_json,created_at FROM production_sessions"):
                intent = json.loads(row["intent_json"])
                entries.append(
                    {
                        "prompt_id": "initial-" + row["run_id"],
                        "kind": "production",
                        "text": intent["brief"],
                        "created_at": row["created_at"],
                        "source": "submitted",
                        "run_id": row["run_id"],
                        "target_duration_seconds": intent.get("target_duration_seconds"),
                    }
                )
            for row in db.execute(
                "SELECT run_id,sequence,payload_json,created_at FROM edit_agent_events WHERE event_type='refinement_requested'"
            ):
                value = json.loads(row["payload_json"])
                entries.append(
                    {
                        "prompt_id": "revision-" + row["run_id"] + "-" + str(row["sequence"]),
                        "kind": "refinement",
                        "text": value["instruction"],
                        "references": value.get("references", {"version": 0, "items": []}),
                        "created_at": row["created_at"],
                        "source": "submitted",
                        "run_id": row["run_id"],
                    }
                )
            for row in db.execute(
                "SELECT payload_json,created_at FROM operation_events WHERE event_type='production_prompt_saved'"
            ):
                entries.append({**json.loads(row["payload_json"]), "created_at": row["created_at"]})
        # Enrich only entries from this project, never expose filesystem paths.
        for entry in entries:
            if "project_id" not in entry:
                entry.update(project_id=project.project_id, project_name=project.name)
    entries.sort(key=lambda e: (e["created_at"], e["prompt_id"]), reverse=True)
    return {"prompts": entries[offset : offset + limit], "total": len(entries), "offset": offset, "limit": limit}
