"""Versioned SQLite schema for the AgentFlow MVP."""

SCHEMA_VERSION = 2

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    canonical_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    run_mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, version),
    UNIQUE (content_hash)
);

CREATE TABLE IF NOT EXISTS authorizations (
    authorization_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    plan_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    authorized_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY (plan_id, plan_version) REFERENCES plans(plan_id, version)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    authorization_id TEXT NOT NULL UNIQUE,
    run_state TEXT NOT NULL,
    control_state TEXT NOT NULL,
    checkpoint_json TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (plan_id, plan_version) REFERENCES plans(plan_id, version),
    FOREIGN KEY (authorization_id) REFERENCES authorizations(authorization_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    contract_json TEXT NOT NULL,
    state TEXT NOT NULL,
    worktree_path TEXT,
    file_baseline_json TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, task_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    UNIQUE (run_id, task_id, attempt_number),
    FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id)
);

CREATE TABLE IF NOT EXISTS model_calls (
    call_id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    attempt_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    model_family TEXT,
    is_local INTEGER NOT NULL CHECK (is_local IN (0, 1)),
    role TEXT NOT NULL,
    state TEXT NOT NULL,
    data_sensitivity TEXT NOT NULL,
    read_only INTEGER NOT NULL CHECK (read_only IN (0, 1)),
    request_scope_json TEXT NOT NULL,
    provider_request_id TEXT,
    output_text TEXT,
    raw_metadata_json TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    first_token_latency_ms INTEGER,
    duration_ms INTEGER,
    remote_cost REAL,
    cost_unavailable INTEGER NOT NULL DEFAULT 0 CHECK (cost_unavailable IN (0, 1)),
    test_double INTEGER NOT NULL DEFAULT 0 CHECK (test_double IN (0, 1)),
    segment_index INTEGER NOT NULL DEFAULT 0 CHECK (segment_index >= 0),
    continuation_of_call_id TEXT,
    continuation_session_id TEXT,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (attempt_id) REFERENCES attempts(attempt_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_calls_provider_request
ON model_calls(provider, provider_request_id, segment_index)
WHERE provider_request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS test_results (
    test_result_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    source TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    duration_ms INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id),
    FOREIGN KEY (attempt_id) REFERENCES attempts(attempt_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
    findings_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id),
    FOREIGN KEY (attempt_id) REFERENCES attempts(attempt_id)
);

CREATE TABLE IF NOT EXISTS cost_entries (
    cost_entry_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    amount REAL NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (call_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (call_id) REFERENCES model_calls(call_id)
);

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_aggregate
ON events(aggregate_type, aggregate_id, sequence);

CREATE TABLE IF NOT EXISTS supervisor_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    reason TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL DEFAULT 'medium',
    plan_hash TEXT,
    event_sequence INTEGER NOT NULL DEFAULT 0,
    terminal INTEGER NOT NULL DEFAULT 0 CHECK (terminal IN (0, 1)),
    content_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'acknowledged')),
    decision_json TEXT,
    acknowledged_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_supervisor_checkpoints_run
ON supervisor_checkpoints(run_id, sequence);

CREATE TABLE IF NOT EXISTS input_artifact_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    snapshot_at TEXT NOT NULL,
    FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id),
    FOREIGN KEY (attempt_id) REFERENCES attempts(attempt_id)
);

CREATE INDEX IF NOT EXISTS idx_input_artifact_snapshots_attempt
ON input_artifact_snapshots(attempt_id);

CREATE TABLE IF NOT EXISTS staging_syncs (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('created', 'synced')),
    synced_files_json TEXT,
    baseline_json TEXT,
    manifest_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (attempt_id) REFERENCES attempts(attempt_id),
    FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id)
);

CREATE INDEX IF NOT EXISTS idx_staging_syncs_run
ON staging_syncs(run_id, task_id);
"""
