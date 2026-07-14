"""信号服务：阅读暴增 / 排名变化 / 新收录 / 新账号 / 评论异常。

实现 v3.2 §10，所有 signal 都可被重算，可携带 model_version 幂等。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ..domain.ids import generate_ulid_like
from ..validation.timestamps import utc_now_iso

SIGNAL_MODEL = "v3.2.0"


class SignalsService:
    """信号计算入口。可手动执行，也可被定时任务触发。"""

    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    # ── 公共方法 ────────────────────────────────────────────

    def detect_all(self) -> dict[str, int]:
        today = utc_now_iso()[:10]
        return {
            "read_spike": self.detect_read_spikes(today),
            "new_keyword": self.detect_new_keyword_entries(today),
            "rank_change": self.detect_rank_changes(today),
            "new_creator": self.detect_new_creators(today),
            "comment_anomaly": self.detect_comment_anomalies(today),
        }

    def detect_read_spikes(self, signal_date: str, threshold: float = 0.5) -> int:
        """对累计型阅读指标比较最近 2 个有效观测点，增速 > 50% 触发。"""
        rows = self._conn.execute(
            """
            WITH ranked AS (
                SELECT o.*, ROW_NUMBER() OVER (
                    PARTITION BY subject_type, subject_id, metric_key
                    ORDER BY observed_at DESC
                ) AS rn
                FROM metric_observations o
                WHERE numeric_value IS NOT NULL
                  AND metric_key IN (
                    'wechat.article.read_count',
                    'xhs.note.liked_count',
                    'geo.source.read_count'
                )
            )
            SELECT cur.subject_type, cur.subject_id, cur.metric_key,
                   prev.numeric_value AS prev_value, cur.numeric_value AS cur_value,
                   prev.observed_at AS prev_at, cur.observed_at AS cur_at
            FROM ranked cur
            JOIN ranked prev
              ON prev.subject_type = cur.subject_type
             AND prev.subject_id = cur.subject_id
             AND prev.metric_key = cur.metric_key
             AND prev.rn = cur.rn + 1
            WHERE cur.rn = 1 AND prev.numeric_value > 0
              AND (cur.numeric_value - prev.numeric_value) * 1.0 / prev.numeric_value >= ?
            """,
            (threshold,),
        ).fetchall()
        for row in rows:
            self._write_signal(
                signal_type="read_spike",
                subject_type=row["subject_type"],
                subject_id=row["subject_id"],
                signal_date=signal_date,
                value=row["cur_value"],
                baseline=row["prev_value"],
                severity=self._severity_from_delta(row["cur_value"], row["prev_value"]),
                details={
                    "metric_key": row["metric_key"],
                    "previous_at": row["prev_at"],
                    "current_at": row["cur_at"],
                },
            )
        return len(rows)

    def detect_new_keyword_entries(self, signal_date: str) -> int:
        rows = self._conn.execute(
            """
            SELECT s.platform, s.keyword, h.content_id, MIN(s.captured_at) AS first_at
            FROM search_hits h JOIN search_snapshots s ON s.snapshot_id = h.snapshot_id
            WHERE h.content_id IS NOT NULL
              AND date(s.captured_at) = date(?)
            GROUP BY s.platform, s.keyword, h.content_id
            HAVING first_at = MIN(s.captured_at)
            """,
            (signal_date,),
        ).fetchall()
        for row in rows:
            self._write_signal(
                signal_type="new_keyword_entry",
                subject_type="content",
                subject_id=row["content_id"],
                signal_date=signal_date,
                value=1.0,
                severity=0.3,
                details={"platform": row["platform"], "keyword": row["keyword"], "first_at": row["first_at"]},
            )
        return len(rows)

    def detect_rank_changes(self, signal_date: str) -> int:
        rows = self._conn.execute(
            """
            WITH latest AS (
                SELECT h.content_id, s.platform, s.keyword, h.rank,
                       s.captured_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY h.content_id, s.platform, s.keyword
                           ORDER BY s.captured_at DESC
                       ) AS rn
                FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                WHERE h.content_id IS NOT NULL
            )
            SELECT cur.content_id, cur.platform, cur.keyword,
                   prev.rank AS prev_rank, cur.rank AS cur_rank
            FROM latest cur JOIN latest prev
              ON prev.content_id = cur.content_id
             AND prev.platform = cur.platform
             AND prev.keyword = cur.keyword
             AND prev.rn = cur.rn + 1
            WHERE cur.rn = 1
              AND ABS(prev.rank - cur.rank) >= 5
            """,
        ).fetchall()
        for row in rows:
            delta = row["prev_rank"] - row["cur_rank"]
            severity = min(1.0, abs(delta) / 20.0)
            self._write_signal(
                signal_type="rank_change",
                subject_type="content",
                subject_id=row["content_id"],
                signal_date=signal_date,
                value=row["cur_rank"],
                baseline=row["prev_rank"],
                severity=severity,
                details={
                    "platform": row["platform"],
                    "keyword": row["keyword"],
                    "previous_rank": row["prev_rank"],
                    "current_rank": row["cur_rank"],
                },
            )
        return len(rows)

    def detect_new_creators(self, signal_date: str) -> int:
        rows = self._conn.execute(
            "SELECT creator_id, canonical_name, platform FROM creators "
            "WHERE date(first_seen_at)=date(?)",
            (signal_date,),
        ).fetchall()
        for row in rows:
            self._write_signal(
                signal_type="new_creator",
                subject_type="creator",
                subject_id=row["creator_id"],
                signal_date=signal_date,
                value=1.0,
                severity=0.2,
                details={"canonical_name": row["canonical_name"], "platform": row["platform"]},
            )
        return len(rows)

    def detect_comment_anomalies(self, signal_date: str) -> int:
        rows = self._conn.execute(
            "SELECT comment_id, event_type, current_state FROM comment_events "
            "WHERE date(observed_at)=date(?) AND event_type IN ('missing','deleted_confirmed','hidden_suspected')",
            (signal_date,),
        ).fetchall()
        for row in rows:
            self._write_signal(
                signal_type="comment_anomaly",
                subject_type="comment",
                subject_id=row["comment_id"],
                signal_date=signal_date,
                value=1.0,
                severity=0.7,
                details={"event_type": row["event_type"], "current_state": row["current_state"]},
            )
        return len(rows)

    # ── 列表接口 ─────────────────────────────────────────────

    def list_signals(
        self,
        *,
        signal_date: str | None = None,
        limit: int = 100,
        signal_type: str | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if signal_date:
            where += " AND signal_date = ?"
            params.append(signal_date)
        if signal_type:
            where += " AND signal_type = ?"
            params.append(signal_type)
        params.append(limit)
        return [
            dict(row)
            for row in self._conn.execute(
                f"SELECT * FROM signals WHERE 1=1 {where} ORDER BY detected_at DESC LIMIT ?",
                params,
            ).fetchall()
        ]

    # ── 内部 ────────────────────────────────────────────────

    def _severity_from_delta(self, current: float, previous: float) -> float:
        if previous <= 0:
            return 0.5
        delta = (current - previous) / previous
        return min(1.0, max(0.1, delta))

    def _write_signal(
        self,
        *,
        signal_type: str,
        subject_type: str,
        subject_id: str,
        signal_date: str,
        value: float,
        baseline: float | None = None,
        severity: float,
        details: dict[str, Any],
    ) -> None:
        import json as _json
        self._conn.execute(
            """
            INSERT INTO signals(
                signal_id, signal_type, subject_type, subject_id, detected_at,
                signal_date, severity, value, baseline_value, model_version,
                status, details_json, consumed_by_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, '[]')
            """,
            (
                generate_ulid_like("sig"),
                signal_type,
                subject_type,
                subject_id,
                utc_now_iso(),
                signal_date,
                severity,
                value,
                baseline,
                SIGNAL_MODEL,
                _json.dumps(details, ensure_ascii=False, sort_keys=True),
            ),
        )
