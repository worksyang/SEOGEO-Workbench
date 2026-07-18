-- 小红书读取接管：旧页面仍保留原 DOM，但事实读取统一从 Hub 兼容投影提供。
-- 刷新、设置和其他外部动作继续由现有冻结/影子任务契约控制。
INSERT INTO migration_switches(
    switch_id, module_key, contract_key, data_mode, enabled, rollback_mode,
    updated_at, updated_by, reason
) VALUES
    ('sw_xhs_001', 'xhs-search', 'bootstrap', 'hub', 1, 'legacy', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'migration_0019', '读取接管：Hub 兼容投影'),
    ('sw_xhs_002', 'xhs-search', 'bootstrap-summary', 'hub', 1, 'legacy', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'migration_0019', '读取接管：Hub 轻量摘要'),
    ('sw_xhs_003', 'xhs-search', 'keyword-detail', 'hub', 1, 'legacy', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'migration_0019', '读取接管：关键词事实'),
    ('sw_xhs_004', 'xhs-search', 'account-detail', 'hub', 1, 'legacy', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'migration_0019', '读取接管：博主事实'),
    ('sw_xhs_005', 'xhs-search', 'article-list', 'hub', 1, 'legacy', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'migration_0019', '读取接管：笔记列表'),
    ('sw_xhs_006', 'xhs-search', 'article-detail', 'hub', 1, 'legacy', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'migration_0019', '读取接管：笔记事实'),
    ('sw_xhs_007', 'xhs-search', 'refresh-status', 'hub', 1, 'legacy', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'migration_0019', '读取接管：Hub 任务状态');
