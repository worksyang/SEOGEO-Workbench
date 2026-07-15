-- v3.3 跨系统回填与信号重算使用的只读检索索引。
-- 不改变核心表字段，不创建评论、生产任务或任何推测性事实。
CREATE INDEX IF NOT EXISTS ix_platforms_active_name
ON platforms(active, canonical_name);

CREATE INDEX IF NOT EXISTS ix_signals_model_subject_date
ON signals(model_version, subject_type, subject_id, signal_date);

CREATE INDEX IF NOT EXISTS ix_geo_relations_source_content
ON geo_source_relations(source_content_id, relation_type, position);

CREATE INDEX IF NOT EXISTS ix_audit_action_subject_time
ON audit_log(action, subject_type, subject_id, occurred_at);
