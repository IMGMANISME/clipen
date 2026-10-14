"""SQLite schema for immutable compiled edit timelines."""

TIMELINE_SCHEMA = """
CREATE TABLE edit_timelines (
    timeline_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(project_id),
    plan_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL CHECK (plan_revision >= 1),
    plan_content_hash TEXT NOT NULL,
    output_preset TEXT NOT NULL CHECK (
        output_preset IN ('landscape_1080p', 'portrait_1080p')
    ),
    frame_rate INTEGER NOT NULL CHECK (frame_rate IN (24, 25, 30, 60)),
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, plan_revision, output_preset, frame_rate)
);
CREATE TABLE current_timeline (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    timeline_id TEXT NOT NULL REFERENCES edit_timelines(timeline_id)
);
CREATE TABLE timeline_command_receipts (
    request_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    timeline_id TEXT NOT NULL REFERENCES edit_timelines(timeline_id),
    created_at TEXT NOT NULL
);
CREATE INDEX edit_timelines_project_created
    ON edit_timelines(project_id, created_at DESC);
"""
