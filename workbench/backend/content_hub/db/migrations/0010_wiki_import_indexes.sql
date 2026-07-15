-- Wiki 全量只读索引链的幂等查找索引。
-- 不改变正文事实；source_ref、hash 与版本表保持可追溯且可恢复。
CREATE INDEX IF NOT EXISTS ix_wiki_versions_source_hash
ON wiki_file_versions(source_ref, file_hash, content_hash);

CREATE INDEX IF NOT EXISTS ix_wiki_discoveries_source_ref
ON content_discoveries(discovery_system, source_ref);
