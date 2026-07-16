-- 微信关键词状态类写入：兼容旧 SQLite registry 的人工控制层。
ALTER TABLE search_keyword_groups ADD COLUMN archived_at TEXT;
ALTER TABLE search_keyword_settings ADD COLUMN pin_order INTEGER;
ALTER TABLE search_keyword_settings ADD COLUMN batch_default_selected INTEGER NOT NULL DEFAULT 1
    CHECK(batch_default_selected IN (0, 1));
ALTER TABLE search_keyword_settings ADD COLUMN refresh_policy_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE search_keyword_settings ADD COLUMN commercial_value_source TEXT NOT NULL DEFAULT 'auto';
ALTER TABLE search_keyword_settings ADD COLUMN commercial_value_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE search_keyword_settings ADD COLUMN auto_archive_locked INTEGER NOT NULL DEFAULT 0
    CHECK(auto_archive_locked IN (0, 1));
ALTER TABLE search_keyword_settings ADD COLUMN keyword_order INTEGER;

CREATE UNIQUE INDEX ux_wechat_keyword_pin_order
ON search_keyword_settings(system_key, platform, pin_order)
WHERE system_key='wechat-search' AND platform='wechat-search' AND pinned=1 AND pin_order IS NOT NULL;

CREATE INDEX ix_wechat_keyword_state
ON search_keyword_settings(system_key, platform, keyword_id, archived_at);
