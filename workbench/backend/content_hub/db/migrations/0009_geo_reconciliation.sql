-- GEO 历史导入逐项对账查询索引；不改变跨系统事实表结构。
CREATE INDEX ix_contract_comparisons_geo_history
ON contract_comparisons(module_key, contract_key, compared_at DESC);

CREATE INDEX ix_audit_log_geo_import
ON audit_log(action, subject_type, subject_id, occurred_at DESC);
