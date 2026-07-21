-- Hub 原生微信 Agent 观察包：运行状态、长期判断账本与决策幂等。
CREATE TABLE wechat_agent_projection_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('building', 'validated', 'published', 'rejected')),
    source_as_of TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    projection_version TEXT NOT NULL,
    brief_id TEXT,
    artifact_count INTEGER NOT NULL DEFAULT 0 CHECK(artifact_count >= 0),
    evidence_count INTEGER NOT NULL DEFAULT 0 CHECK(evidence_count >= 0),
    validation_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(validation_json)),
    error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json)),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    published_at TEXT,
    UNIQUE(source_fingerprint, projection_version)
);

CREATE INDEX ix_wechat_agent_projection_runs_status
ON wechat_agent_projection_runs(status, started_at DESC);

CREATE TABLE wechat_agent_claims (
    claim_id TEXT PRIMARY KEY,
    claim_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    first_reported_at TEXT NOT NULL,
    last_reported_at TEXT NOT NULL,
    last_direction TEXT NOT NULL DEFAULT '',
    last_fingerprint TEXT NOT NULL DEFAULT '',
    last_priority REAL,
    last_state TEXT NOT NULL DEFAULT '',
    report_path TEXT NOT NULL DEFAULT '',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_ids_json)),
    source_run_id TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(source_run_id) REFERENCES wechat_agent_projection_runs(run_id)
);

CREATE INDEX ix_wechat_agent_claims_reported
ON wechat_agent_claims(last_reported_at DESC);

CREATE TABLE wechat_agent_decisions (
    decision_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    brief_id TEXT NOT NULL,
    decision_json TEXT NOT NULL CHECK(json_valid(decision_json)),
    reported_claim_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(reported_claim_ids_json)),
    report_path TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES wechat_agent_projection_runs(run_id)
);

CREATE INDEX ix_wechat_agent_decisions_run
ON wechat_agent_decisions(run_id, applied_at DESC);
