"""SQLite schema for bounded, resumable AI editing sessions."""

EDIT_AGENT_SCHEMA = """
CREATE TABLE edit_agent_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(project_id),
    request_id TEXT NOT NULL UNIQUE,
    base_timeline_id TEXT REFERENCES edit_timelines(timeline_id),
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')
    ),
    phase TEXT NOT NULL,
    max_tool_calls INTEGER NOT NULL CHECK (max_tool_calls BETWEEN 1 AND 500),
    tool_calls INTEGER NOT NULL DEFAULT 0 CHECK (tool_calls >= 0),
    max_preview_renders INTEGER NOT NULL CHECK (max_preview_renders BETWEEN 0 AND 20),
    preview_renders INTEGER NOT NULL DEFAULT 0 CHECK (preview_renders >= 0),
    current_revision_id TEXT,
    final_revision_id TEXT,
    provider TEXT NOT NULL,
    provider_attempt_id TEXT,
    usage_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE edit_agent_revisions (
    revision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES edit_agent_runs(run_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    parent_revision_id TEXT REFERENCES edit_agent_revisions(revision_id),
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, ordinal),
    UNIQUE(run_id, payload_digest)
);
CREATE TABLE edit_agent_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES edit_agent_runs(run_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);
CREATE TABLE edit_agent_command_receipts (
    request_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX edit_agent_runs_project_created
    ON edit_agent_runs(project_id, created_at DESC);
CREATE INDEX edit_agent_revisions_run_ordinal
    ON edit_agent_revisions(run_id, ordinal DESC);
CREATE INDEX edit_agent_events_run_sequence
    ON edit_agent_events(run_id, sequence);
"""

PRODUCTION_SESSION_SCHEMA = """
CREATE TABLE production_sessions (
    run_id TEXT PRIMARY KEY REFERENCES edit_agent_runs(run_id),
    intent_json TEXT NOT NULL,
    skill_json TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    inventory_run_id TEXT NOT NULL REFERENCES inventory_runs(run_id),
    created_at TEXT NOT NULL
);
CREATE TABLE production_evidence_jobs (
    job_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES edit_agent_runs(run_id),
    cache_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('transcribe','align','setup')),
    state TEXT NOT NULL CHECK(state IN ('running','succeeded','failed','cancelled','interrupted')),
    request_json TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id,cache_key)
);
CREATE TABLE production_tool_receipts (
    run_id TEXT NOT NULL REFERENCES edit_agent_runs(run_id),
    mutation_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('started','completed','failed')),
    result_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id,mutation_id)
);
"""
