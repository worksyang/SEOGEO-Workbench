-- v3.3 §5.2：为微信搜索、小红书、Wiki 的既有历史事实补齐不可再生证据清单。
-- 实际文件不由迁移复制或伪造；Python backfill 只登记已存在的来源、hash、时间、计数。
CREATE INDEX IF NOT EXISTS ix_source_manifest_entries_hash
ON source_manifest_entries(content_hash);
