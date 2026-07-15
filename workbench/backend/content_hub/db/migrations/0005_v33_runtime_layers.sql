-- v3.3 模块运行层与迁移控制平面。
-- 核心事实表保持独立；本迁移只新增模块状态、版本、任务与回退记录。

CREATE TABLE source_manifests (
    manifest_id TEXT PRIMARY KEY,
    system_key TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    root_fingerprint TEXT NOT NULL,
    manifest_hash TEXT NOT NULL UNIQUE,
    entry_count INTEGER NOT NULL DEFAULT 0 CHECK(entry_count >= 0),
    captured_at TEXT NOT NULL,
    immutable INTEGER NOT NULL DEFAULT 1 CHECK(immutable IN (0, 1)),
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json))
);

CREATE TABLE source_manifest_entries (
    manifest_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    content_hash TEXT,
    size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes >= 0),
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    PRIMARY KEY(manifest_id, relative_path),
    FOREIGN KEY(manifest_id) REFERENCES source_manifests(manifest_id) ON DELETE CASCADE
);

CREATE TABLE migration_switches (
    switch_id TEXT PRIMARY KEY,
    module_key TEXT NOT NULL,
    contract_key TEXT NOT NULL,
    data_mode TEXT NOT NULL CHECK(data_mode IN ('legacy', 'compare', 'hub')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    rollback_mode TEXT NOT NULL DEFAULT 'legacy' CHECK(rollback_mode IN ('legacy', 'compare', 'hub')),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    reason TEXT,
    UNIQUE(module_key, contract_key)
);

CREATE TABLE contract_comparisons (
    comparison_id TEXT PRIMARY KEY,
    module_key TEXT NOT NULL,
    contract_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    legacy_hash TEXT,
    hub_hash TEXT,
    status TEXT NOT NULL CHECK(status IN ('matched', 'different', 'legacy_error', 'hub_error')),
    diff_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(diff_json)),
    compared_at TEXT NOT NULL
);

CREATE TABLE dual_write_receipts (
    receipt_id TEXT PRIMARY KEY,
    module_key TEXT NOT NULL,
    command_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    legacy_status TEXT NOT NULL,
    hub_status TEXT NOT NULL,
    reconcile_status TEXT NOT NULL CHECK(reconcile_status IN ('pending', 'matched', 'different', 'blocked')),
    details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json)),
    created_at TEXT NOT NULL,
    UNIQUE(module_key, idempotency_key)
);

CREATE TABLE command_runs (
    command_id TEXT PRIMARY KEY,
    module_key TEXT NOT NULL,
    command_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'blocked')),
    confirmation_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(confirmation_json)),
    input_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(input_json)),
    output_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(output_json)),
    error_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(error_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(module_key, idempotency_key)
);

-- 搜索监控运行层：system_key/platform 隔离微信与小红书。
CREATE TABLE search_keyword_groups (
    group_id TEXT PRIMARY KEY,
    system_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    group_name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(system_key, platform, group_name)
);

CREATE TABLE search_keyword_settings (
    setting_id TEXT PRIMARY KEY,
    system_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    keyword_id TEXT NOT NULL,
    group_id TEXT,
    pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0, 1)),
    refresh_strategy TEXT NOT NULL DEFAULT 'manual' CHECK(refresh_strategy IN ('manual', 'scheduled', 'disabled')),
    refresh_interval_minutes INTEGER CHECK(refresh_interval_minutes IS NULL OR refresh_interval_minutes >= 1),
    commercial_value REAL,
    note TEXT NOT NULL DEFAULT '',
    archived_at TEXT,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    UNIQUE(system_key, platform, keyword_id),
    FOREIGN KEY(keyword_id) REFERENCES keywords(keyword_id),
    FOREIGN KEY(group_id) REFERENCES search_keyword_groups(group_id) ON DELETE SET NULL
);

CREATE TABLE search_refresh_jobs (
    refresh_job_id TEXT PRIMARY KEY,
    system_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    command_id TEXT,
    trigger_type TEXT NOT NULL CHECK(trigger_type IN ('manual', 'scheduled', 'replay', 'import')),
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'partial_failed', 'failed', 'cancelled', 'blocked')),
    requested_count INTEGER NOT NULL DEFAULT 0 CHECK(requested_count >= 0),
    succeeded_count INTEGER NOT NULL DEFAULT 0 CHECK(succeeded_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK(failed_count >= 0),
    checkpoint_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(checkpoint_json)),
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(command_id) REFERENCES command_runs(command_id)
);

CREATE TABLE search_refresh_items (
    refresh_item_id TEXT PRIMARY KEY,
    refresh_job_id TEXT NOT NULL,
    keyword_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'blocked')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    snapshot_id TEXT,
    source_manifest_id TEXT,
    error_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(error_json)),
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(refresh_job_id, keyword_id),
    FOREIGN KEY(refresh_job_id) REFERENCES search_refresh_jobs(refresh_job_id) ON DELETE CASCADE,
    FOREIGN KEY(keyword_id) REFERENCES keywords(keyword_id),
    FOREIGN KEY(snapshot_id) REFERENCES search_snapshots(snapshot_id),
    FOREIGN KEY(source_manifest_id) REFERENCES source_manifests(manifest_id)
);

CREATE TABLE search_scheduler_state (
    system_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    next_run_at TEXT,
    last_run_at TEXT,
    active_refresh_job_id TEXT,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    PRIMARY KEY(system_key, platform),
    FOREIGN KEY(active_refresh_job_id) REFERENCES search_refresh_jobs(refresh_job_id)
);

-- 公众号监控运行层：任何秘密只保存 configuration_ref，不保存凭据。
CREATE TABLE mp_accounts_runtime (
    mp_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    source_account_ref TEXT NOT NULL UNIQUE,
    avatar_ref TEXT,
    description TEXT,
    configuration_ref TEXT,
    last_seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json))
);

CREATE TABLE mp_categories (
    category_id TEXT PRIMARY KEY,
    category_name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE mp_account_flags (
    mp_id TEXT PRIMARY KEY,
    category_id TEXT,
    monitor_enabled INTEGER NOT NULL DEFAULT 1 CHECK(monitor_enabled IN (0, 1)),
    run_enabled INTEGER NOT NULL DEFAULT 1 CHECK(run_enabled IN (0, 1)),
    updated_at TEXT NOT NULL,
    FOREIGN KEY(mp_id) REFERENCES mp_accounts_runtime(mp_id) ON DELETE CASCADE,
    FOREIGN KEY(category_id) REFERENCES mp_categories(category_id) ON DELETE SET NULL
);

CREATE TABLE mp_collection_jobs (
    collection_job_id TEXT PRIMARY KEY,
    command_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'partial_failed', 'failed', 'cancelled', 'blocked')),
    account_count INTEGER NOT NULL DEFAULT 0 CHECK(account_count >= 0),
    settings_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(settings_json)),
    checkpoint_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(checkpoint_json)),
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(command_id) REFERENCES command_runs(command_id)
);

CREATE TABLE mp_collection_events (
    collection_event_id TEXT PRIMARY KEY,
    collection_job_id TEXT NOT NULL,
    mp_id TEXT,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json)),
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(collection_job_id) REFERENCES mp_collection_jobs(collection_job_id) ON DELETE CASCADE,
    FOREIGN KEY(mp_id) REFERENCES mp_accounts_runtime(mp_id) ON DELETE SET NULL
);

CREATE TABLE mp_runtime_settings (
    setting_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(value_json)),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

-- Wiki 运行层：原目录只读，工作副本/版本/图片任务均由此层描述。
CREATE TABLE wiki_edit_sessions (
    session_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL,
    workspace_ref TEXT NOT NULL,
    base_version_id TEXT,
    actor_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'saved', 'abandoned', 'conflicted')),
    lock_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(content_id) REFERENCES contents(content_id)
);

CREATE TABLE wiki_file_versions (
    version_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    workspace_ref TEXT NOT NULL,
    parent_version_id TEXT,
    file_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    version_status TEXT NOT NULL CHECK(version_status IN ('baseline', 'draft', 'published', 'superseded', 'reverted')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(content_id) REFERENCES contents(content_id),
    FOREIGN KEY(parent_version_id) REFERENCES wiki_file_versions(version_id)
);

CREATE INDEX ix_wiki_versions_content_time ON wiki_file_versions(content_id, created_at DESC);

CREATE TABLE wiki_image_index (
    image_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL,
    version_id TEXT,
    image_ref TEXT NOT NULL,
    image_core TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK(occurrence_count >= 1),
    ocr_status TEXT NOT NULL DEFAULT 'unknown' CHECK(ocr_status IN ('unknown', 'available', 'queued', 'failed')),
    indexed_at TEXT NOT NULL,
    UNIQUE(content_id, version_id, image_core),
    FOREIGN KEY(content_id) REFERENCES contents(content_id),
    FOREIGN KEY(version_id) REFERENCES wiki_file_versions(version_id)
);

CREATE TABLE wiki_image_jobs (
    image_job_id TEXT PRIMARY KEY,
    command_id TEXT,
    job_type TEXT NOT NULL CHECK(job_type IN ('index', 'ocr', 'delete_preview', 'delete_apply', 'clean', 'generate')),
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'blocked')),
    input_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(input_json)),
    output_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(output_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(command_id) REFERENCES command_runs(command_id)
);

CREATE TABLE wiki_ocr_records (
    ocr_record_id TEXT PRIMARY KEY,
    image_id TEXT,
    image_core TEXT NOT NULL,
    source_ref TEXT,
    ocr_text TEXT NOT NULL DEFAULT '',
    provider_kind TEXT NOT NULL DEFAULT 'legacy_readonly',
    content_hash TEXT,
    captured_at TEXT NOT NULL,
    FOREIGN KEY(image_id) REFERENCES wiki_image_index(image_id) ON DELETE SET NULL
);

-- WritingMoney 运行层：项目、素材、模板、方案、写作包、批次、草稿一律规范化。
CREATE TABLE wm_projects (
    wm_project_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL CHECK(stage IN ('decision', 'materials', 'template', 'plan', 'package', 'draft', 'completed', 'archived')),
    status TEXT NOT NULL CHECK(status IN ('draft', 'active', 'blocked', 'completed', 'archived')),
    workspace_ref TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE wm_project_events (
    wm_project_event_id TEXT PRIMARY KEY,
    wm_project_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json)),
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(wm_project_id) REFERENCES wm_projects(wm_project_id) ON DELETE CASCADE
);

CREATE TABLE wm_materials (
    wm_material_id TEXT PRIMARY KEY,
    material_kind TEXT NOT NULL CHECK(material_kind IN ('wiki', 'url', 'manual', 'signal')),
    title TEXT,
    source_content_id TEXT,
    source_ref TEXT,
    url TEXT,
    parse_status TEXT NOT NULL CHECK(parse_status IN ('received', 'queued', 'parsed', 'failed', 'blocked')),
    body_ref TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(source_content_id) REFERENCES contents(content_id)
);

CREATE TABLE wm_project_materials (
    wm_project_id TEXT NOT NULL,
    wm_material_id TEXT NOT NULL,
    usage_state TEXT NOT NULL CHECK(usage_state IN ('required', 'reference', 'excluded')),
    selected_by TEXT,
    selected_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(wm_project_id, wm_material_id),
    FOREIGN KEY(wm_project_id) REFERENCES wm_projects(wm_project_id) ON DELETE CASCADE,
    FOREIGN KEY(wm_material_id) REFERENCES wm_materials(wm_material_id) ON DELETE CASCADE
);

CREATE TABLE wm_templates (
    wm_template_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_ref TEXT,
    markdown_ref TEXT NOT NULL,
    content_hash TEXT,
    version_label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE wm_project_templates (
    wm_project_id TEXT NOT NULL,
    wm_template_id TEXT NOT NULL,
    selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0, 1)),
    recommendation_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(wm_project_id, wm_template_id),
    FOREIGN KEY(wm_project_id) REFERENCES wm_projects(wm_project_id) ON DELETE CASCADE,
    FOREIGN KEY(wm_template_id) REFERENCES wm_templates(wm_template_id) ON DELETE CASCADE
);

CREATE TABLE wm_plans (
    wm_plan_id TEXT PRIMARY KEY,
    wm_project_id TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft', 'selected', 'superseded')),
    outline_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(outline_json)),
    judgment_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(judgment_json)),
    input_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(wm_project_id, version_no),
    FOREIGN KEY(wm_project_id) REFERENCES wm_projects(wm_project_id) ON DELETE CASCADE
);

CREATE TABLE wm_packages (
    wm_package_id TEXT PRIMARY KEY,
    wm_project_id TEXT NOT NULL,
    wm_plan_id TEXT,
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft', 'frozen', 'superseded')),
    package_ref TEXT NOT NULL,
    input_manifest_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(input_manifest_json)),
    input_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(wm_project_id, version_no),
    FOREIGN KEY(wm_project_id) REFERENCES wm_projects(wm_project_id) ON DELETE CASCADE,
    FOREIGN KEY(wm_plan_id) REFERENCES wm_plans(wm_plan_id)
);

CREATE TABLE wm_batches (
    wm_batch_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL CHECK(status IN ('draft', 'ready', 'running', 'partial_failed', 'completed', 'cancelled', 'blocked', 'archived')),
    requirements_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(requirements_json)),
    workspace_ref TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE wm_batch_keywords (
    wm_batch_keyword_id TEXT PRIMARY KEY,
    wm_batch_id TEXT NOT NULL,
    keyword_id TEXT,
    keyword_text TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    target_article_count INTEGER NOT NULL DEFAULT 1 CHECK(target_article_count BETWEEN 1 AND 100),
    readiness_status TEXT NOT NULL DEFAULT 'pending' CHECK(readiness_status IN ('pending', 'ready', 'blocked')),
    ordinal INTEGER NOT NULL DEFAULT 0 CHECK(ordinal >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
    UNIQUE(wm_batch_id, keyword_text),
    FOREIGN KEY(wm_batch_id) REFERENCES wm_batches(wm_batch_id) ON DELETE CASCADE,
    FOREIGN KEY(keyword_id) REFERENCES keywords(keyword_id)
);

CREATE TABLE wm_batch_mother_links (
    wm_batch_keyword_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    relation_type TEXT NOT NULL CHECK(relation_type IN ('recommended', 'selected', 'excluded')),
    confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(wm_batch_keyword_id, content_id),
    FOREIGN KEY(wm_batch_keyword_id) REFERENCES wm_batch_keywords(wm_batch_keyword_id) ON DELETE CASCADE,
    FOREIGN KEY(content_id) REFERENCES contents(content_id)
);

CREATE TABLE wm_drafts (
    wm_draft_id TEXT PRIMARY KEY,
    wm_project_id TEXT,
    wm_batch_id TEXT,
    wm_batch_keyword_id TEXT,
    parent_draft_id TEXT,
    content_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'draft', 'review', 'rework', 'ready_for_publish', 'published', 'failed', 'cancelled')),
    title TEXT,
    artifact_ref TEXT,
    input_hash TEXT,
    output_hash TEXT,
    provider_kind TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(wm_project_id IS NOT NULL OR wm_batch_id IS NOT NULL),
    FOREIGN KEY(wm_project_id) REFERENCES wm_projects(wm_project_id) ON DELETE SET NULL,
    FOREIGN KEY(wm_batch_id) REFERENCES wm_batches(wm_batch_id) ON DELETE SET NULL,
    FOREIGN KEY(wm_batch_keyword_id) REFERENCES wm_batch_keywords(wm_batch_keyword_id) ON DELETE SET NULL,
    FOREIGN KEY(parent_draft_id) REFERENCES wm_drafts(wm_draft_id),
    FOREIGN KEY(content_id) REFERENCES contents(content_id)
);

-- 发布运行层：秘密仍在本机配置，数据库只保存非敏感状态与调度信息。
CREATE TABLE publish_accounts_runtime (
    account_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    configuration_ref TEXT,
    login_status TEXT NOT NULL CHECK(login_status IN ('unknown', 'ready', 'expired', 'blocked', 'disabled')),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    publishable INTEGER NOT NULL DEFAULT 0 CHECK(publishable IN (0, 1)),
    cooldown_until TEXT,
    last_checked_at TEXT,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json))
);

CREATE TABLE publish_queues (
    publish_queue_id TEXT PRIMARY KEY,
    queue_name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('draft', 'dry_run', 'normal', 'immediate', 'scheduled', 'mixed')),
    status TEXT NOT NULL CHECK(status IN ('draft', 'ready', 'running', 'paused', 'completed', 'cancelled', 'blocked')),
    interval_seconds INTEGER NOT NULL DEFAULT 0 CHECK(interval_seconds >= 0),
    requires_confirmation INTEGER NOT NULL DEFAULT 1 CHECK(requires_confirmation IN (0, 1)),
    schedule_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(schedule_json)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE publish_queue_items (
    publish_queue_item_id TEXT PRIMARY KEY,
    publish_queue_id TEXT NOT NULL,
    wm_draft_id TEXT,
    content_id TEXT,
    account_id TEXT,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    status TEXT NOT NULL CHECK(status IN ('queued', 'waiting', 'running', 'drafted', 'published', 'failed', 'cancelled', 'blocked')),
    scheduled_at TEXT,
    idempotency_key TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    UNIQUE(publish_queue_id, ordinal),
    UNIQUE(publish_queue_id, idempotency_key),
    FOREIGN KEY(publish_queue_id) REFERENCES publish_queues(publish_queue_id) ON DELETE CASCADE,
    FOREIGN KEY(wm_draft_id) REFERENCES wm_drafts(wm_draft_id) ON DELETE SET NULL,
    FOREIGN KEY(content_id) REFERENCES contents(content_id) ON DELETE SET NULL,
    FOREIGN KEY(account_id) REFERENCES publish_accounts_runtime(account_id) ON DELETE SET NULL
);

CREATE TABLE publish_events (
    publish_event_id TEXT PRIMARY KEY,
    publish_queue_item_id TEXT,
    attempt_id TEXT,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json)),
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(publish_queue_item_id) REFERENCES publish_queue_items(publish_queue_item_id) ON DELETE CASCADE,
    FOREIGN KEY(attempt_id) REFERENCES publish_attempts(attempt_id) ON DELETE SET NULL
);

ALTER TABLE production_jobs ADD COLUMN wm_project_id TEXT REFERENCES wm_projects(wm_project_id);
ALTER TABLE production_jobs ADD COLUMN wm_batch_id TEXT REFERENCES wm_batches(wm_batch_id);

CREATE INDEX ix_command_runs_module_status ON command_runs(module_key, status, updated_at);
CREATE INDEX ix_search_refresh_jobs_status ON search_refresh_jobs(system_key, platform, status, created_at);
CREATE INDEX ix_mp_collection_jobs_status ON mp_collection_jobs(status, created_at);
CREATE INDEX ix_wm_projects_status ON wm_projects(status, updated_at);
CREATE INDEX ix_wm_batches_status ON wm_batches(status, updated_at);
CREATE INDEX ix_wm_drafts_status ON wm_drafts(status, updated_at);
CREATE INDEX ix_publish_queues_status ON publish_queues(status, updated_at);
