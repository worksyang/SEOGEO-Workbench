-- 微信刷新前端契约：保存取消原因，避免取消/失败被压成无理由状态。
ALTER TABLE search_refresh_jobs ADD COLUMN cancel_reason TEXT;
