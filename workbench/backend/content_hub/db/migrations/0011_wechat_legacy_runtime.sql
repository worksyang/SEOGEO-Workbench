-- 微信兼容读取投影：保存旧算法输出的分片快照，不替代 v3.3 核心事实。
CREATE TABLE wechat_legacy_projections (
    projection_id TEXT PRIMARY KEY,
    projection_kind TEXT NOT NULL CHECK(projection_kind IN ('top_level','full','keyword','account','bootstrap','keyword_manage','article_detail','runtime')),
    subject_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    source_hash TEXT NOT NULL,
    source_manifest_id TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(projection_kind, subject_id, source_hash)
);

CREATE INDEX ix_wechat_legacy_projection_lookup
ON wechat_legacy_projections(projection_kind, subject_id, updated_at DESC);

CREATE TABLE wechat_article_paths (
    article_id TEXT NOT NULL,
    old_article_id TEXT NOT NULL,
    relative_path TEXT,
    asset_path TEXT,
    source_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(article_id, old_article_id, source_ref)
);

CREATE TABLE wechat_discovery_probes (
    probe_id TEXT PRIMARY KEY,
    probe_text TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    source_ref TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE wechat_discovery_candidates (
    candidate_id TEXT PRIMARY KEY,
    candidate_text TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    source_ref TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE wechat_discovery_evidence (
    evidence_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    probe_id TEXT,
    snapshot_id TEXT,
    source_article_id TEXT,
    evidence_date TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    source_ref TEXT,
    updated_at TEXT NOT NULL
);
