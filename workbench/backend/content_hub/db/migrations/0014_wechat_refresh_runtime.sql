-- 微信关键词刷新与调度运行层。
-- 0011/0013 保持只读兼容；本迁移只补齐可恢复的事件、取消和调度字段。

ALTER TABLE search_refresh_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0
    CHECK(cancel_requested IN (0, 1));
ALTER TABLE search_refresh_jobs ADD COLUMN cancel_requested_at TEXT;
ALTER TABLE search_refresh_jobs ADD COLUMN cancelled_at TEXT;
ALTER TABLE search_refresh_jobs ADD COLUMN trigger_source TEXT NOT NULL DEFAULT 'web_refresh_all';

ALTER TABLE search_refresh_items ADD COLUMN current_phase TEXT NOT NULL DEFAULT 'queued';

CREATE TABLE search_refresh_events (
    event_id TEXT PRIMARY KEY,
    refresh_job_id TEXT NOT NULL,
    refresh_item_id TEXT,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json)),
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(refresh_job_id) REFERENCES search_refresh_jobs(refresh_job_id) ON DELETE CASCADE,
    FOREIGN KEY(refresh_item_id) REFERENCES search_refresh_items(refresh_item_id) ON DELETE CASCADE
);

CREATE INDEX ix_search_refresh_events_job_time
ON search_refresh_events(refresh_job_id, occurred_at, event_id);

CREATE INDEX ix_search_refresh_items_job_status
ON search_refresh_items(refresh_job_id, status, ordinal);

CREATE INDEX ix_search_refresh_jobs_active
ON search_refresh_jobs(system_key, platform, status, cancel_requested, created_at);
