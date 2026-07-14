CREATE TABLE ingestion_batches (
    batch_id TEXT PRIMARY KEY,
    adapter_key TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK(status IN ('queued', 'running', 'succeeded', 'partial_failed', 'failed', 'cancelled')),
    started_at TEXT,
    finished_at TEXT,
    records_seen INTEGER NOT NULL DEFAULT 0 CHECK(records_seen >= 0),
    records_written INTEGER NOT NULL DEFAULT 0 CHECK(records_written >= 0),
    records_failed INTEGER NOT NULL DEFAULT 0 CHECK(records_failed >= 0),
    source_ref TEXT,
    error_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(error_json)),
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX ix_ingestion_batches_adapter_time
ON ingestion_batches(adapter_key, created_at);

CREATE TABLE ingestion_checkpoints (
    adapter_key TEXT NOT NULL,
    checkpoint_key TEXT NOT NULL,
    cursor_value TEXT,
    source_hash TEXT,
    last_success_at TEXT,
    batch_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    PRIMARY KEY(adapter_key, checkpoint_key),
    FOREIGN KEY(batch_id) REFERENCES ingestion_batches(batch_id)
);

CREATE TABLE audit_log (
    audit_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    subject_type TEXT,
    subject_id TEXT,
    request_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('succeeded', 'failed', 'blocked')),
    details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json))
);

CREATE INDEX ix_audit_time_action ON audit_log(occurred_at, action);

CREATE TABLE keywords (
    keyword_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    keyword TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'paused', 'archived')),
    topic TEXT,
    keyword_bucket TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    UNIQUE(platform, keyword)
);

CREATE TABLE platforms (
    platform_key TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    aliases_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(aliases_json)),
    icon_url TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json))
);

CREATE TABLE creators (
    creator_id TEXT PRIMARY KEY,
    canonical_name TEXT,
    platform TEXT,
    external_id TEXT,
    profile_url TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    UNIQUE(platform, external_id)
);

CREATE TABLE contents (
    content_id TEXT PRIMARY KEY,
    content_type TEXT NOT NULL,
    title TEXT,
    canonical_url TEXT,
    creator_id TEXT,
    author_name TEXT,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    md_path TEXT,
    file_hash TEXT,
    content_hash TEXT,
    domain TEXT,
    entities_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(entities_json)),
    intents_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(intents_json)),
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(creator_id) REFERENCES creators(creator_id)
);

CREATE UNIQUE INDEX ux_contents_canonical_url
ON contents(canonical_url)
WHERE canonical_url IS NOT NULL;

CREATE INDEX ix_contents_type_time ON contents(content_type, published_at);
CREATE INDEX ix_contents_creator_time ON contents(creator_id, published_at);

CREATE TABLE content_identifiers (
    namespace TEXT NOT NULL,
    external_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    PRIMARY KEY(namespace, external_id),
    FOREIGN KEY(content_id) REFERENCES contents(content_id)
);

CREATE INDEX ix_identifiers_content ON content_identifiers(content_id);

CREATE TABLE search_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    keyword TEXT NOT NULL,
    keyword_id TEXT,
    captured_at TEXT NOT NULL,
    trigger_type TEXT,
    result_count INTEGER CHECK(result_count IS NULL OR result_count >= 0),
    features_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(features_json)),
    source_ref TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(keyword_id) REFERENCES keywords(keyword_id),
    UNIQUE(platform, keyword, captured_at)
);

CREATE INDEX ix_search_keyword_time
ON search_snapshots(platform, keyword, captured_at);

CREATE TABLE content_discoveries (
    discovery_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL,
    discovery_system TEXT NOT NULL,
    discovery_channel TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    snapshot_id TEXT,
    source_ref TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(content_id) REFERENCES contents(content_id),
    FOREIGN KEY(snapshot_id) REFERENCES search_snapshots(snapshot_id)
);

CREATE UNIQUE INDEX ux_discoveries_identity
ON content_discoveries(
    content_id,
    discovery_system,
    discovery_channel,
    COALESCE(snapshot_id, 'no-snapshot')
);

CREATE INDEX ix_discovery_system_time
ON content_discoveries(discovery_system, discovered_at);

CREATE TABLE search_hits (
    hit_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK(rank > 0),
    content_id TEXT,
    title_raw TEXT,
    url_raw TEXT,
    creator_name_raw TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(snapshot_id) REFERENCES search_snapshots(snapshot_id) ON DELETE CASCADE,
    FOREIGN KEY(content_id) REFERENCES contents(content_id),
    UNIQUE(snapshot_id, rank)
);

CREATE INDEX ix_hits_content ON search_hits(content_id, snapshot_id);

CREATE TABLE metric_definitions (
    metric_key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'number'
        CHECK(value_type IN ('number', 'text', 'boolean')),
    unit TEXT,
    accumulation_mode TEXT NOT NULL DEFAULT 'gauge'
        CHECK(accumulation_mode IN ('gauge', 'counter', 'delta', 'state')),
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))
);

CREATE TABLE metric_observations (
    observation_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    numeric_value REAL,
    text_value TEXT,
    snapshot_id TEXT,
    source_ref TEXT,
    confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(metric_key) REFERENCES metric_definitions(metric_key),
    FOREIGN KEY(snapshot_id) REFERENCES search_snapshots(snapshot_id),
    CHECK(
        (numeric_value IS NOT NULL AND text_value IS NULL)
        OR (numeric_value IS NULL AND text_value IS NOT NULL)
    )
);

CREATE UNIQUE INDEX ux_metric_observations_identity
ON metric_observations(
    subject_type,
    subject_id,
    metric_key,
    observed_at,
    COALESCE(snapshot_id, 'no-snapshot')
);

CREATE INDEX ix_obs_subject_metric_time
ON metric_observations(subject_type, subject_id, metric_key, observed_at);

CREATE INDEX ix_obs_metric_time ON metric_observations(metric_key, observed_at);

CREATE TABLE comments (
    comment_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    external_id TEXT,
    parent_comment_id TEXT,
    author_name TEXT,
    text_raw TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    current_visibility TEXT NOT NULL DEFAULT 'visible'
        CHECK(current_visibility IN ('visible', 'missing', 'deleted', 'unknown')),
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(content_id) REFERENCES contents(content_id),
    FOREIGN KEY(parent_comment_id) REFERENCES comments(comment_id),
    UNIQUE(platform, external_id)
);

CREATE TABLE comment_events (
    event_id TEXT PRIMARY KEY,
    comment_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    previous_state TEXT,
    current_state TEXT,
    source_ref TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(comment_id) REFERENCES comments(comment_id) ON DELETE CASCADE
);

CREATE INDEX ix_comment_events_time ON comment_events(event_type, observed_at);

CREATE TABLE geo_answers (
    answer_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL,
    app TEXT NOT NULL,
    mode TEXT,
    question_raw TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    answer_hash TEXT,
    tools_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tools_json)),
    recommended_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(recommended_json)),
    source_ref TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(content_id) REFERENCES contents(content_id)
);

CREATE UNIQUE INDEX ux_geo_answer_fingerprint
ON geo_answers(app, question_raw, captured_at, answer_hash)
WHERE answer_hash IS NOT NULL;

CREATE INDEX ix_geo_question_time ON geo_answers(app, question_raw, captured_at);

CREATE TABLE geo_source_relations (
    relation_id TEXT PRIMARY KEY,
    answer_id TEXT NOT NULL,
    source_content_id TEXT,
    relation_type TEXT NOT NULL,
    position INTEGER CHECK(position IS NULL OR position >= 0),
    anchor_text TEXT,
    url_raw TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(answer_id) REFERENCES geo_answers(answer_id) ON DELETE CASCADE,
    FOREIGN KEY(source_content_id) REFERENCES contents(content_id)
);

CREATE UNIQUE INDEX ux_geo_source_relations_identity
ON geo_source_relations(
    answer_id,
    relation_type,
    COALESCE(position, -1),
    COALESCE(url_raw, ''),
    COALESCE(anchor_text, '')
);

CREATE INDEX ix_geo_source_content
ON geo_source_relations(source_content_id, answer_id);

CREATE TABLE signals (
    signal_id TEXT PRIMARY KEY,
    signal_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    severity REAL,
    value REAL,
    baseline_value REAL,
    model_version TEXT,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK(status IN ('new', 'acknowledged', 'consumed', 'dismissed')),
    details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json)),
    consumed_by_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(consumed_by_json))
);

CREATE UNIQUE INDEX ux_signals_daily
ON signals(
    signal_type,
    subject_type,
    subject_id,
    signal_date,
    COALESCE(model_version, 'no-model')
);

CREATE INDEX ix_signals_daily ON signals(signal_date, signal_type, status);

CREATE TABLE production_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'blocked')),
    input_signal_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(input_signal_ids_json)),
    source_content_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(source_content_ids_json)),
    output_content_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    scheduled_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts >= 1),
    last_error TEXT,
    locked_by TEXT,
    locked_at TEXT,
    next_attempt_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(output_content_id) REFERENCES contents(content_id),
    CHECK(status = 'running' OR locked_by IS NULL OR locked_at IS NOT NULL)
);

CREATE INDEX ix_jobs_status_time
ON production_jobs(status, scheduled_at, next_attempt_at);

CREATE TABLE identity_merge_candidates (
    candidate_id TEXT PRIMARY KEY,
    left_content_id TEXT NOT NULL,
    right_content_id TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'approved', 'rejected', 'expired')),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT,
    FOREIGN KEY(left_content_id) REFERENCES contents(content_id),
    FOREIGN KEY(right_content_id) REFERENCES contents(content_id),
    CHECK(left_content_id < right_content_id),
    UNIQUE(left_content_id, right_content_id)
);

CREATE TABLE identity_merge_map (
    source_content_id TEXT PRIMARY KEY,
    target_content_id TEXT NOT NULL,
    merged_at TEXT NOT NULL,
    merged_by TEXT NOT NULL,
    reason_json TEXT NOT NULL CHECK(json_valid(reason_json)),
    reverted_at TEXT,
    reverted_by TEXT,
    FOREIGN KEY(source_content_id) REFERENCES contents(content_id),
    FOREIGN KEY(target_content_id) REFERENCES contents(content_id),
    CHECK(source_content_id <> target_content_id)
);

CREATE TABLE signal_consumption (
    consumption_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    consumer_type TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(signal_id) REFERENCES signals(signal_id),
    UNIQUE(signal_id, consumer_type, consumer_id)
);

CREATE TABLE job_events (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT,
    progress REAL CHECK(progress IS NULL OR (progress >= 0 AND progress <= 1)),
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(job_id) REFERENCES production_jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX ix_job_events_job_time ON job_events(job_id, occurred_at);

CREATE TABLE system_connections (
    system_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    base_url TEXT,
    status TEXT NOT NULL DEFAULT 'unknown'
        CHECK(status IN ('healthy', 'degraded', 'offline', 'blocked', 'unknown')),
    last_checked_at TEXT,
    capabilities_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(capabilities_json)),
    details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json))
);

CREATE TABLE correction_jobs (
    correction_id TEXT PRIMARY KEY,
    correction_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'approved', 'running', 'succeeded', 'rejected', 'failed')),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_by TEXT,
    error TEXT
);

CREATE INDEX ix_corrections_status_time ON correction_jobs(status, created_at);

CREATE TABLE publish_attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    account_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL CHECK(mode IN ('dry_run', 'draft', 'publish')),
    status TEXT NOT NULL
        CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'blocked', 'cancelled')),
    attempted_at TEXT,
    remote_receipt TEXT,
    error TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(job_id) REFERENCES production_jobs(job_id)
);

CREATE INDEX ix_publish_attempts_job ON publish_attempts(job_id, status);
