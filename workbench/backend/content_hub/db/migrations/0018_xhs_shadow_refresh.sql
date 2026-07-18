-- 小红书搜索级影子刷新：只保存关键词搜索原始响应与可审计引用。
CREATE TABLE xhs_shadow_responses (
    response_id TEXT PRIMARY KEY,
    refresh_job_id TEXT NOT NULL,
    refresh_item_id TEXT,
    keyword_id TEXT NOT NULL,
    provider_kind TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    response_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    FOREIGN KEY(refresh_job_id) REFERENCES search_refresh_jobs(refresh_job_id) ON DELETE CASCADE,
    FOREIGN KEY(refresh_item_id) REFERENCES search_refresh_items(refresh_item_id) ON DELETE SET NULL,
    UNIQUE(refresh_job_id, keyword_id)
);
CREATE INDEX ix_xhs_shadow_responses_keyword_time
ON xhs_shadow_responses(keyword_id, captured_at DESC);
