-- WritingMoney 运行层补强：运行表自身持有 legacy bridge 关联，
-- 队列状态与写入证据不再依赖 production_jobs.payload_json。

ALTER TABLE wm_projects ADD COLUMN legacy_job_id TEXT;
ALTER TABLE wm_batches ADD COLUMN legacy_job_id TEXT;

CREATE UNIQUE INDEX ux_wm_projects_legacy_job
ON wm_projects(legacy_job_id)
WHERE legacy_job_id IS NOT NULL;

CREATE UNIQUE INDEX ux_wm_batches_legacy_job
ON wm_batches(legacy_job_id)
WHERE legacy_job_id IS NOT NULL;

CREATE TABLE wm_batch_queue_items (
    wm_batch_queue_item_id TEXT PRIMARY KEY,
    wm_batch_id TEXT NOT NULL,
    wm_batch_keyword_id TEXT,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting', 'running', 'done', 'rework', 'failed', 'cancelled')),
    output_ref TEXT NOT NULL DEFAULT '',
    wm_draft_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(wm_batch_id, ordinal),
    FOREIGN KEY(wm_batch_id) REFERENCES wm_batches(wm_batch_id) ON DELETE CASCADE,
    FOREIGN KEY(wm_batch_keyword_id) REFERENCES wm_batch_keywords(wm_batch_keyword_id) ON DELETE SET NULL,
    FOREIGN KEY(wm_draft_id) REFERENCES wm_drafts(wm_draft_id) ON DELETE SET NULL
);

CREATE INDEX ix_wm_batch_queue_items_status
ON wm_batch_queue_items(wm_batch_id, status, ordinal);
