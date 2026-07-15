-- 将历史 geopromax 注册项规范化为唯一的 geo 注册项。
-- 只写入固定的注册元数据；不读取或复制 legacy details_json。
DROP TABLE IF EXISTS temp.migration_0004_geo_state;

CREATE TEMP TABLE migration_0004_geo_state AS
SELECT
    EXISTS (
        SELECT 1
        FROM system_connections
        WHERE system_key = 'geopromax'
    ) AS legacy_found;

INSERT INTO system_connections(
    system_key,
    display_name,
    base_url,
    status,
    capabilities_json,
    details_json
)
SELECT
    'geo',
    'GEOProMax',
    NULL,
    'unknown',
    '["read","json_import","manual_paid_refresh","history_import"]',
    '{"source_kind":"canonical_registry","migrated_from":"geopromax"}'
WHERE EXISTS (
    SELECT 1
    FROM system_connections
    WHERE system_key = 'geopromax'
)
AND NOT EXISTS (
    SELECT 1
    FROM system_connections
    WHERE system_key = 'geo'
);

DELETE FROM system_connections
WHERE system_key = 'geopromax';

INSERT INTO audit_log(
    audit_id,
    occurred_at,
    actor_type,
    actor_id,
    action,
    subject_type,
    subject_id,
    outcome,
    details_json
)
SELECT
    'audit_migration_0004_geo_connection',
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    'migration',
    '0004_canonicalize_geo_connection',
    'system_connection.canonicalize',
    'system_connection',
    'geo',
    'succeeded',
    json_object(
        'migration', '0004_canonicalize_geo_connection',
        'legacy_key', 'geopromax',
        'canonical_key', 'geo',
        'legacy_found', state.legacy_found,
        'legacy_deleted', CASE
            WHEN NOT EXISTS (
                SELECT 1
                FROM system_connections
                WHERE system_key = 'geopromax'
            ) THEN 1
            ELSE 0
        END,
        'canonical_geo_present', CASE
            WHEN EXISTS (
                SELECT 1
                FROM system_connections
                WHERE system_key = 'geo'
            ) THEN 1
            ELSE 0
        END
    )
FROM migration_0004_geo_state AS state
WHERE NOT EXISTS (
    SELECT 1
    FROM audit_log
    WHERE audit_id = 'audit_migration_0004_geo_connection'
);

DROP TABLE migration_0004_geo_state;
