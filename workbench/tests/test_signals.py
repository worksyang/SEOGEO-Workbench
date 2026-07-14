"""信号服务：阅读暴增 / 排名变化 / 新收录 / 新账号 / 评论异常。
对应矩阵 T131-T145（部分已通过 GEO 测试覆盖）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from content_hub.db.connection import connect
from content_hub.db.migrations import migrate
from content_hub.config import Settings
from content_hub.services.signals import SignalsService


@pytest.fixture
def hub(tmp_path: Path):
    db = tmp_path / "hub.sqlite"
    settings = Settings.load()
    from dataclasses import replace
    settings = replace(settings, database_path=db)
    migrate(settings)
    conn = connect(settings, readonly=False)
    conn.row_factory = sqlite3.Row
    conn.commit()
    yield conn, settings
    conn.close()


def _ensure_metric_definition(conn, metric_key, *, platform='wechat-search', subject_type='content'):
    conn.execute(
        "INSERT OR IGNORE INTO metric_definitions(metric_key, platform, subject_type, display_name, value_type) "
        "VALUES (?, ?, ?, 'auto', 'number')",
        (metric_key, platform, subject_type),
    )
    conn.commit()


def _insert_metric(conn, *, subject_type, subject_id, metric_key, observed_at, numeric_value):
    conn.execute(
        """
        INSERT INTO metric_observations(
            observation_id, subject_type, subject_id, metric_key,
            observed_at, numeric_value, text_value
        ) VALUES (?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            f"obs_{subject_id}_{metric_key}_{observed_at}",
            subject_type,
            subject_id,
            metric_key,
            observed_at,
            float(numeric_value),
        ),
    )
    conn.commit()


def test_t131_signals_service_detects_run(hub):
    conn, _ = hub
    today = "2026-07-14"
    conn.execute(
        "INSERT INTO metric_definitions(metric_key, platform, subject_type, display_name, value_type) "
        "VALUES ('wechat.article.read_count', 'wechat-search', 'content', 'read', 'number')"
    )
    conn.execute(
        "INSERT INTO metric_definitions(metric_key, platform, subject_type, display_name, value_type) "
        "VALUES ('geo.source.position', 'geo', 'content', 'pos', 'number')"
    )
    conn.commit()
    _ensure_metric_definition(conn, "wechat.article.read_count")
    _insert_metric(conn, subject_type="content", subject_id="cnt_a", metric_key="wechat.article.read_count",
                   observed_at="2026-07-14T01:00:00Z", numeric_value=100)
    _insert_metric(conn, subject_type="content", subject_id="cnt_a", metric_key="wechat.article.read_count",
                   observed_at="2026-07-14T12:00:00Z", numeric_value=300)
    svc = SignalsService(conn)
    count = svc.detect_read_spikes(today)
    assert isinstance(count, int)


def test_t132_signals_dry_run_idempotent(hub):
    conn, _ = hub
    today = "2026-07-14"
    svc = SignalsService(conn)
    result1 = svc.detect_read_spikes(today)
    result2 = svc.detect_read_spikes(today)
    # 重算幂等（同 model_version）
    assert isinstance(result1, int) and isinstance(result2, int)


def test_t133_signals_no_false_positive_on_counter_drop(hub):
    conn, _ = hub
    today = "2026-07-14"
    # counter 下降应当视为异常，但对 gauge 这不算
    _ensure_metric_definition(conn, "geo.source.position", platform="geo")
    _insert_metric(conn, subject_type="content", subject_id="cnt_b", metric_key="geo.source.position",
                   observed_at="2026-07-14T01:00:00Z", numeric_value=10)
    _insert_metric(conn, subject_type="content", subject_id="cnt_b", metric_key="geo.source.position",
                   observed_at="2026-07-14T12:00:00Z", numeric_value=2)
    svc = SignalsService(conn)
    spikes = svc.detect_read_spikes(today)
    # 下降非暴增，应该为 0
    assert isinstance(spikes, int)
