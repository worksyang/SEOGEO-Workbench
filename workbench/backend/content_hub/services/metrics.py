"""指标服务：观测查询、按主体的最新值、聚合曲线。"""
from __future__ import annotations

import sqlite3
from typing import Any

from .helpers import as_int, parse_json_field, rows_to_dicts


class MetricsService:
    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    def observations(self, *, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
        rows = rows_to_dicts(
            self._conn,
            "SELECT metric_key, observed_at, numeric_value, text_value, snapshot_id, confidence, "
            "source_ref, payload_json FROM metric_observations "
            "WHERE subject_type=? AND subject_id=? ORDER BY observed_at",
            (subject_type, subject_id),
        )
        for row in rows:
            row["payload"] = parse_json_field(row.pop("payload_json"), {})
        return rows

    def latest(self, *, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
        rows = rows_to_dicts(
            self._conn,
            """
            WITH ranked AS (
                SELECT o.*, ROW_NUMBER() OVER (
                    PARTITION BY metric_key ORDER BY observed_at DESC
                ) AS rn
                FROM metric_observations o
                WHERE subject_type=? AND subject_id=?
            )
            SELECT metric_key, observed_at, numeric_value, text_value, snapshot_id
            FROM ranked WHERE rn = 1
            """,
            (subject_type, subject_id),
        )
        return rows
