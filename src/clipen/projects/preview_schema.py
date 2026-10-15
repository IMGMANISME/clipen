"""SQLite schema for durable rough-cut preview renders."""

PREVIEW_SCHEMA = """
CREATE TABLE preview_renders (
    render_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(project_id),
    timeline_id TEXT NOT NULL REFERENCES edit_timelines(timeline_id),
    request_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (
        state IN (
            'queued', 'running', 'cancelling', 'succeeded',
            'failed', 'cancelled', 'interrupted'
        )
    ),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
    output_relative_path TEXT,
    output_size_bytes INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE current_preview_render (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    render_id TEXT NOT NULL REFERENCES preview_renders(render_id)
);
CREATE INDEX preview_renders_project_created
    ON preview_renders(project_id, created_at DESC);
"""
