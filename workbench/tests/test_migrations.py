from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

from content_hub.db.connection import connect
from content_hub.db.migrations import migrate
from content_hub.config import Settings


def test_migrations_are_idempotent(settings) -> None:
    assert migrate(settings) == []
    with connect(settings, readonly=True) as connection:
        rows = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert [(row["version"], row["name"]) for row in rows] == [
        (1, "initial"),
        (2, "views"),
        (3, "system_registry"),
        (4, "canonicalize_geo_connection"),
        (5, "v33_runtime_layers"),
        (6, "writing_runtime_receipts"),
        (7, "source_manifest_backfill"),
        (8, "core_fill_indexes"),
        (9, "geo_reconciliation"),
    ]


def _settings_for_migration_state(
    base: Settings, tmp_path: Path, *, initial_only: bool
) -> Settings:
    migration_dir = tmp_path / ("initial-migrations" if initial_only else "all-migrations")
    migration_dir.mkdir()
    source_dir = base.migration_dir
    names = ["0001_initial.sql", "0002_views.sql", "0003_system_registry.sql"]
    if not initial_only:
        names.extend([
            "0004_canonicalize_geo_connection.sql",
            "0005_v33_runtime_layers.sql",
            "0006_writing_runtime_receipts.sql",
            "0007_source_manifest_backfill.sql",
            "0008_core_fill_indexes.sql",
            "0009_geo_reconciliation.sql",
        ])
    for name in names:
        shutil.copy2(source_dir / name, migration_dir / name)
    return replace(
        base,
        database_path=tmp_path / "state.sqlite",
        migration_dir=migration_dir,
    )


def _apply_initial_schema(base: Settings, tmp_path: Path) -> Settings:
    settings = _settings_for_migration_state(base, tmp_path, initial_only=True)
    assert migrate(settings) == [1, 2, 3]
    return settings


def _run_canonicalization(
    base: Settings,
    tmp_path: Path,
    *,
    with_geo: bool,
) -> Settings:
    initial = _apply_initial_schema(base, tmp_path)
    with connect(initial) as connection:
        connection.execute("DELETE FROM system_connections")
        connection.execute(
            """
            INSERT INTO system_connections(
                system_key, display_name, base_url, status,
                capabilities_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "geopromax",
                "legacy GEOProMax",
                "http://127.0.0.1:8790",
                "unknown",
                '["read"]',
                '{"absolute_path":"/Users/secret/geopromax.sqlite","api_key":"do-not-copy"}',
            ),
        )
        if with_geo:
            connection.execute(
                """
                INSERT INTO system_connections(
                    system_key, display_name, base_url, status,
                    capabilities_json, details_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "geo",
                    "GEOProMax",
                    None,
                    "healthy",
                    '["read","json_import"]',
                    '{"source_kind":"current"}',
                ),
            )
        connection.commit()

    full = _settings_for_migration_state(base, tmp_path, initial_only=False)
    assert full.database_path == initial.database_path
    assert migrate(full) == [4, 5, 6, 7, 8, 9]
    return full


def _assert_canonicalized(settings: Settings) -> None:
    with connect(settings, readonly=True) as connection:
        rows = connection.execute(
            "SELECT system_key FROM system_connections ORDER BY system_key"
        ).fetchall()
        assert [row["system_key"] for row in rows] == [
            "geo",
        ]
        audits = connection.execute(
            """
            SELECT action, details_json
            FROM audit_log
            WHERE audit_id = 'audit_migration_0004_geo_connection'
            """
        ).fetchall()
        assert len(audits) == 1
        assert audits[0]["action"] == "system_connection.canonicalize"
        assert "/Users/secret" not in audits[0]["details_json"]
        assert "do-not-copy" not in audits[0]["details_json"]
        details = json.loads(audits[0]["details_json"])
        assert "geo_created" not in details
        assert details["legacy_found"] == 1
        assert details["legacy_deleted"] == 1
        assert details["canonical_geo_present"] == 1


def test_legacy_only_connection_is_canonicalized_without_legacy_details(
    settings, tmp_path
) -> None:
    migrated = _run_canonicalization(settings, tmp_path, with_geo=False)
    _assert_canonicalized(migrated)


def test_legacy_and_geo_connection_keep_geo_and_drop_legacy(tmp_path) -> None:
    base = Settings.load()
    migrated = _run_canonicalization(base, tmp_path, with_geo=True)
    _assert_canonicalized(migrated)

    with connect(migrated, readonly=True) as connection:
        geo = connection.execute(
            "SELECT status, details_json FROM system_connections WHERE system_key='geo'"
        ).fetchone()
        assert geo["status"] == "healthy"
        assert geo["details_json"] == '{"source_kind":"current"}'


def test_fresh_database_has_seven_connections_and_repeat_is_noop(settings) -> None:
    with connect(settings, readonly=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM system_connections"
        ).fetchone()[0] == 7
        assert connection.execute(
            "SELECT COUNT(*) FROM system_connections WHERE system_key='geo'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM system_connections WHERE system_key='geopromax'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE audit_id='audit_migration_0004_geo_connection'"
        ).fetchone()[0] == 1
    assert migrate(settings) == []
    with connect(settings, readonly=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE audit_id='audit_migration_0004_geo_connection'"
        ).fetchone()[0] == 1


def test_v33_runtime_control_planes_and_module_tables_exist(settings) -> None:
    """v3.3 的跨系统核心表不承载运行状态；运行状态必须有独立表。"""
    expected = {
        "source_manifests", "source_manifest_entries", "migration_switches",
        "contract_comparisons", "dual_write_receipts", "command_runs",
        "search_keyword_groups", "search_keyword_settings", "search_refresh_jobs",
        "search_refresh_items", "search_scheduler_state",
        "mp_accounts_runtime", "mp_categories", "mp_account_flags",
        "mp_collection_jobs", "mp_collection_events", "mp_runtime_settings",
        "wiki_edit_sessions", "wiki_file_versions", "wiki_image_index",
        "wiki_image_jobs", "wiki_ocr_records",
        "wm_projects", "wm_project_events", "wm_materials",
        "wm_project_materials", "wm_templates", "wm_project_templates",
        "wm_plans", "wm_packages", "wm_batches", "wm_batch_keywords",
        "wm_batch_mother_links", "wm_drafts",
        "publish_accounts_runtime", "publish_queues", "publish_queue_items",
        "publish_events",
    }
    with connect(settings, readonly=True) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert expected <= tables
        production_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(production_jobs)").fetchall()
        }
        assert {"wm_project_id", "wm_batch_id"} <= production_columns


def test_source_manifest_backfill_is_idempotent_and_hides_absolute_paths(settings, tmp_path) -> None:
    from content_hub.ingestion.source_manifest_backfill import backfill_existing_manifests

    with connect(settings) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_batches(
                batch_id, adapter_key, source_scope, status, source_ref
            ) VALUES ('batch_wechat_fixture', 'wechat-search', 'history', 'succeeded',
                      '/Users/works14/legacy/wechat')
            """
        )
        connection.execute(
            """
            INSERT INTO search_snapshots(
                snapshot_id, platform, keyword, captured_at, source_ref
            ) VALUES ('snap_fixture', 'wechat-search', '测试关键词',
                      '2026-07-15T00:00:00Z',
                      'normalized/snapshots.json')
            """
        )
        connection.commit()
        first = backfill_existing_manifests(connection, settings)
        second = backfill_existing_manifests(connection, settings)
        connection.commit()
        assert first["systems"]["wechat-search"]["manifest_id"] == second["systems"]["wechat-search"]["manifest_id"]
        refs = connection.execute(
            """
            SELECT source_ref FROM ingestion_batches
            UNION ALL SELECT source_ref FROM search_snapshots
            """
        ).fetchall()
        assert all(str(row[0]).startswith("manifest://wechat-search/") for row in refs)
        assert all("/Users/" not in str(row[0]) for row in refs)
        assert connection.execute("SELECT COUNT(*) FROM source_manifests").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE audit_id='audit_migration_0007_source_manifest_backfill'"
        ).fetchone()[0] == 1
