from __future__ import annotations

import sqlite3

import pytest

from content_hub.db.connection import connect

CORE_TABLES = {
    "contents",
    "creators",
    "content_identifiers",
    "content_discoveries",
    "search_snapshots",
    "search_hits",
    "metric_definitions",
    "metric_observations",
    "comments",
    "comment_events",
    "geo_answers",
    "geo_source_relations",
    "signals",
    "production_jobs",
}

SUPPORT_TABLES = {
    "schema_migrations",
    "ingestion_batches",
    "ingestion_checkpoints",
    "audit_log",
    "keywords",
    "platforms",
    "identity_merge_candidates",
    "identity_merge_map",
    "signal_consumption",
    "job_events",
    "system_connections",
    "correction_jobs",
    "publish_attempts",
}


def insert_content(connection: sqlite3.Connection, content_id: str = "cnt_test") -> None:
    connection.execute(
        """
        INSERT INTO contents(
            content_id, content_type, title, first_seen_at, updated_at
        ) VALUES (?, 'external_article', '测试内容', '2026-07-14T00:00:00Z', '2026-07-14T00:00:00Z')
        """,
        (content_id,),
    )


def test_all_core_and_support_tables_exist(settings) -> None:
    with connect(settings, readonly=True) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert CORE_TABLES <= tables
    assert SUPPORT_TABLES <= tables
    assert len(CORE_TABLES) == 14
    assert len(SUPPORT_TABLES) == 13


def test_sqlite_pragmas_and_integrity(settings) -> None:
    with connect(settings) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_discovery_null_snapshot_is_still_unique(settings) -> None:
    with connect(settings) as connection:
        insert_content(connection)
        values = (
            "cnt_test",
            "wechat-mp",
            "account-monitor",
            "2026-07-14T00:00:00Z",
        )
        connection.execute(
            """
            INSERT INTO content_discoveries(
                discovery_id, content_id, discovery_system, discovery_channel, discovered_at
            ) VALUES ('dsc_one', ?, ?, ?, ?)
            """,
            values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO content_discoveries(
                    discovery_id, content_id, discovery_system, discovery_channel, discovered_at
                ) VALUES ('dsc_two', ?, ?, ?, ?)
                """,
                values,
            )


def test_observation_null_snapshot_is_still_unique(settings) -> None:
    with connect(settings) as connection:
        connection.execute(
            """
            INSERT INTO metric_definitions(
                metric_key, platform, subject_type, display_name
            ) VALUES ('wechat.article.read_count', 'wechat', 'content', '阅读量')
            """
        )
        values = (
            "content",
            "cnt_test",
            "wechat.article.read_count",
            "2026-07-14T00:00:00Z",
            100,
        )
        connection.execute(
            """
            INSERT INTO metric_observations(
                observation_id, subject_type, subject_id, metric_key, observed_at, numeric_value
            ) VALUES ('obs_one', ?, ?, ?, ?, ?)
            """,
            values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO metric_observations(
                    observation_id, subject_type, subject_id, metric_key, observed_at, numeric_value
                ) VALUES ('obs_two', ?, ?, ?, ?, ?)
                """,
                values,
            )


def test_geo_answer_and_relation_replay_are_idempotent(settings) -> None:
    with connect(settings) as connection:
        insert_content(connection)
        answer_values = (
            "cnt_test",
            "豆包",
            "quick",
            "测试问题",
            "2026-07-14T00:00:00Z",
            "hash-one",
        )
        connection.execute(
            """
            INSERT INTO geo_answers(
                answer_id, content_id, app, mode, question_raw, captured_at, answer_hash
            ) VALUES ('ans_one', ?, ?, ?, ?, ?, ?)
            """,
            answer_values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO geo_answers(
                    answer_id, content_id, app, mode, question_raw, captured_at, answer_hash
                ) VALUES ('ans_two', ?, ?, ?, ?, ?, ?)
                """,
                answer_values,
            )
        connection.execute(
            """
            INSERT INTO geo_source_relations(
                relation_id, answer_id, relation_type
            ) VALUES ('rel_one', 'ans_one', 'text_reference')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO geo_source_relations(
                    relation_id, answer_id, relation_type
                ) VALUES ('rel_two', 'ans_one', 'text_reference')
                """
            )


def test_production_job_status_and_lock_constraints(settings) -> None:
    with connect(settings) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO production_jobs(
                    job_id, job_type, status, created_at, updated_at
                ) VALUES (
                    'job_bad', 'writing', 'imaginary',
                    '2026-07-14T00:00:00Z', '2026-07-14T00:00:00Z'
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO production_jobs(
                    job_id, job_type, status, created_at, updated_at, locked_by
                ) VALUES (
                    'job_lock', 'writing', 'queued',
                    '2026-07-14T00:00:00Z', '2026-07-14T00:00:00Z', 'worker-one'
                )
                """
            )


def test_readonly_connection_rejects_writes(settings) -> None:
    with connect(settings, readonly=True) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "INSERT INTO platforms(platform_key, canonical_name) VALUES ('x', 'x')"
            )
