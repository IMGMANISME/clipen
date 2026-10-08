"""SQLite schema for recoverable AI planning and editable Plan revisions."""

PLANNING_SCHEMA = """
CREATE TABLE planning_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(project_id),
    request_id TEXT NOT NULL UNIQUE,
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    editing_skill_id TEXT NOT NULL,
    system_skill_release TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    context_json TEXT NOT NULL,
    estimated_input_tokens INTEGER NOT NULL CHECK (estimated_input_tokens >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')
    ),
    phase TEXT NOT NULL,
    plan_id TEXT,
    plan_revision INTEGER CHECK (plan_revision IS NULL OR plan_revision >= 1),
    usage_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE plan_revisions (
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    project_id TEXT NOT NULL REFERENCES project(project_id),
    status TEXT NOT NULL CHECK (
        status IN ('draft', 'ready_for_review', 'confirmed', 'executing',
                  'reviewed', 'accepted', 'superseded')
    ),
    content_hash TEXT NOT NULL,
    material_snapshot_id TEXT NOT NULL,
    analysis_snapshot_id TEXT NOT NULL,
    system_skill_release TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_run_id TEXT REFERENCES planning_runs(run_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, revision),
    UNIQUE(plan_id, content_hash)
);
CREATE TABLE current_plan (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    FOREIGN KEY(plan_id, revision) REFERENCES plan_revisions(plan_id, revision)
);
CREATE TABLE plan_command_receipts (
    request_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX planning_runs_project_created
    ON planning_runs(project_id, created_at DESC);
CREATE INDEX plan_revisions_project_created
    ON plan_revisions(project_id, created_at DESC);
"""
