"""SQLite schema for persisted timeline validation workflow state."""

VALIDATION_WORKFLOW_SCHEMA = """
CREATE TABLE timeline_validation_reports (
    report_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(project_id),
    timeline_id TEXT NOT NULL,
    capability_profile_id TEXT NOT NULL,
    capability_content_hash TEXT NOT NULL,
    report_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pass', 'warning', 'blocking')),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE current_timeline_validation (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    report_id TEXT NOT NULL REFERENCES timeline_validation_reports(report_id)
);
CREATE TABLE timeline_warning_acceptances (
    request_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES timeline_validation_reports(report_id),
    report_digest TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    accepted_at TEXT NOT NULL
);
CREATE INDEX timeline_validation_reports_project_created
    ON timeline_validation_reports(project_id, created_at DESC);
"""
