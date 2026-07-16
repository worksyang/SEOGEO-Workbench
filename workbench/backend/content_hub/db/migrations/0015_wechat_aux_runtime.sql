-- 微信 AUX 迁移运行层：只保存兼容投影、受控缓存和可重启的 provider 结果。
CREATE TABLE wechat_aux_artifacts (
    artifact_id TEXT PRIMARY KEY,
    artifact_kind TEXT NOT NULL CHECK(artifact_kind IN (
        'manifest', 'daily_brief', 'metric_dictionary', 'evidence',
        'penalty_signals', 'account_aliases'
    )),
    subject_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    source_hash TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    model_version TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(artifact_kind, subject_id, source_hash)
);

CREATE INDEX ix_wechat_aux_artifacts_lookup
ON wechat_aux_artifacts(artifact_kind, subject_id, updated_at DESC);

CREATE TABLE wechat_aux_cover_cache (
    source_url TEXT PRIMARY KEY,
    asset_hash TEXT NOT NULL,
    asset_path TEXT NOT NULL,
    content_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    width INTEGER,
    height INTEGER,
    fetched_at TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    CHECK(width IS NULL OR width > 0),
    CHECK(height IS NULL OR height > 0)
);

CREATE INDEX ix_wechat_aux_cover_cache_hash
ON wechat_aux_cover_cache(asset_hash);

CREATE TABLE wechat_aux_provider_results (
    result_id TEXT PRIMARY KEY,
    provider_kind TEXT NOT NULL,
    operation TEXT NOT NULL,
    lookup_key TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    model_version TEXT,
    source_ref TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    command_id TEXT,
    UNIQUE(provider_kind, operation, lookup_key),
    FOREIGN KEY(command_id) REFERENCES command_runs(command_id)
);

CREATE INDEX ix_wechat_aux_provider_results_lookup
ON wechat_aux_provider_results(provider_kind, operation, lookup_key);
