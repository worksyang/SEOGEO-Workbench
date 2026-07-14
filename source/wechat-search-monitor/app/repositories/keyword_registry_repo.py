from __future__ import annotations

import json
import sqlite3
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.ingest.common import kw_id
from app.keyword_bucket_resolver import infer_keyword_bucket
from app.topic_resolver import infer_topic


ACTIVE = "active"
ARCHIVED = "archived"
AUTO_REFRESH_POLICY = "auto"
MANUAL_REFRESH_POLICY = "manual"
SUPPORTED_REFRESH_FREQUENCIES = {1, 3, 7, 15}
OBSERVATION_REFRESH_INTERVAL_HOURS = 3


def effective_refresh_interval_hours(item: dict[str, Any]) -> float:
    """调度用有效间隔；观察期自动词按3小时切片，人工周期不受影响。"""
    if (
        item.get("lifecycle_stage") == "observing"
        and item.get("refresh_frequency_source") != MANUAL_REFRESH_POLICY
    ):
        return float(OBSERVATION_REFRESH_INTERVAL_HOURS)
    return float(max(1, int(item.get("refresh_frequency_days") or 1)) * 24)


class KeywordRegistryRepository:
    """Single source of truth for keyword identity, lifecycle and UI controls.

    Observation fields are updated by rebuild. Human-controlled fields are only
    updated by keyword-management actions; rebuild must never overwrite them.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS keyword_groups (
                    group_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL UNIQUE,
                    display_order INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS keyword_registry (
                    keyword_id TEXT PRIMARY KEY,
                    keyword_text TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
                    source TEXT NOT NULL DEFAULT 'manual',
                    group_id TEXT,
                    keyword_order INTEGER,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT,
                    is_pinned INTEGER NOT NULL DEFAULT 0,
                    pin_order INTEGER,
                    topic TEXT,
                    keyword_bucket TEXT,
                    batch_default_selected INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    snapshot_count INTEGER NOT NULL DEFAULT 0,
                    refresh_frequency_days INTEGER NOT NULL DEFAULT 1,
                    refresh_frequency_source TEXT NOT NULL DEFAULT 'auto',
                    refresh_policy_reason TEXT NOT NULL DEFAULT '',
                    last_refresh_at TEXT,
                    last_refresh_attempt_at TEXT,
                    last_refresh_status TEXT,
                    commercial_value_score INTEGER NOT NULL DEFAULT 5,
                    commercial_value_source TEXT NOT NULL DEFAULT 'auto',
                    commercial_value_reason TEXT NOT NULL DEFAULT '',
                    lifecycle_stage TEXT NOT NULL DEFAULT 'established',
                    observation_started_at TEXT,
                    observation_deadline_at TEXT,
                    discovery_candidate_id TEXT,
                    auto_archive_locked INTEGER NOT NULL DEFAULT 0,
                    archive_reason_code TEXT,
                    archive_reason_detail TEXT,
                    FOREIGN KEY(group_id) REFERENCES keyword_groups(group_id)
                );

                CREATE INDEX IF NOT EXISTS idx_keyword_registry_status
                    ON keyword_registry(status);
                CREATE INDEX IF NOT EXISTS idx_keyword_registry_group
                    ON keyword_registry(group_id, keyword_order);
                """
            )
            self._ensure_column(
                conn,
                "keyword_registry",
                "refresh_frequency_days",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                conn,
                "keyword_registry",
                "refresh_frequency_source",
                "TEXT NOT NULL DEFAULT 'auto'",
            )
            self._ensure_column(
                conn,
                "keyword_registry",
                "refresh_policy_reason",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(conn, "keyword_registry", "last_refresh_at", "TEXT")
            self._ensure_column(conn, "keyword_registry", "last_refresh_attempt_at", "TEXT")
            self._ensure_column(conn, "keyword_registry", "last_refresh_status", "TEXT")
            self._ensure_column(
                conn,
                "keyword_registry",
                "commercial_value_score",
                "INTEGER NOT NULL DEFAULT 5",
            )
            self._ensure_column(
                conn,
                "keyword_registry",
                "commercial_value_source",
                "TEXT NOT NULL DEFAULT 'auto'",
            )
            self._ensure_column(
                conn,
                "keyword_registry",
                "commercial_value_reason",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn,
                "keyword_registry",
                "lifecycle_stage",
                "TEXT NOT NULL DEFAULT 'established'",
            )
            self._ensure_column(conn, "keyword_registry", "observation_started_at", "TEXT")
            self._ensure_column(conn, "keyword_registry", "observation_deadline_at", "TEXT")
            self._ensure_column(conn, "keyword_registry", "discovery_candidate_id", "TEXT")
            self._ensure_column(
                conn,
                "keyword_registry",
                "auto_archive_locked",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(conn, "keyword_registry", "archive_reason_code", "TEXT")
            self._ensure_column(conn, "keyword_registry", "archive_reason_detail", "TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_keyword_registry_refresh_schedule
                ON keyword_registry(status, refresh_frequency_days, last_refresh_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_keyword_registry_lifecycle
                ON keyword_registry(status, lifecycle_stage, observation_deadline_at)
                """
            )
            # 历史快照的最后观测时间可作为刷新状态的迁移基线。后续批跑会以
            # last_refresh_at 为唯一更新入口，不会被 rebuild 覆盖。
            conn.execute(
                """
                UPDATE keyword_registry
                SET last_refresh_at = last_seen_at,
                    last_refresh_status = COALESCE(last_refresh_status, 'success')
                WHERE last_refresh_at IS NULL
                  AND last_seen_at IS NOT NULL
                """
            )

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def is_empty(self) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM keyword_registry").fetchone()[0] == 0

    def migrate_legacy(
        self,
        config_path: Path,
        normalized_path: Path,
        *,
        drop_legacy_settings: bool = False,
    ) -> dict[str, int]:
        """One-time merge: config words become active, observed-only words archived."""
        if not self.is_empty():
            return self.counts()

        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        normalized = json.loads(Path(normalized_path).read_text(encoding="utf-8"))
        now = self._now()

        with self._connect() as conn:
            legacy_settings: dict[str, sqlite3.Row] = {}
            has_legacy = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='keyword_settings'"
            ).fetchone()
            if has_legacy:
                legacy_settings = {
                    row["keyword_id"]: row
                    for row in conn.execute("SELECT * FROM keyword_settings").fetchall()
                }

            active_ids: set[str] = set()
            for group in config.get("groups", []):
                group_id = str(group.get("group_id") or "").strip()
                if not group_id:
                    continue
                created_at = group.get("created_at") or config.get("updated_at") or now
                conn.execute(
                    """
                    INSERT INTO keyword_groups
                        (group_id, label, display_order, created_at, updated_at, archived_at)
                    VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        group_id,
                        str(group.get("label") or "未分类"),
                        int(group.get("order") or 0),
                        created_at,
                        group.get("updated_at") or config.get("updated_at") or now,
                    ),
                )
                for order, item in enumerate(group.get("keywords", []), 1):
                    text = str(item.get("keyword_text") or "").strip()
                    if not text:
                        continue
                    keyword_id = str(item.get("keyword_id") or kw_id(text))
                    active_ids.add(keyword_id)
                    state = legacy_settings.get(keyword_id)
                    default_topic = infer_topic(text)
                    conn.execute(
                        """
                        INSERT INTO keyword_registry (
                            keyword_id, keyword_text, status, source, group_id, keyword_order,
                            note, created_at, updated_at, archived_at,
                            is_pinned, pin_order, topic, keyword_bucket,
                            batch_default_selected, first_seen_at, last_seen_at, snapshot_count
                        ) VALUES (?, ?, 'active', 'manual', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, 0)
                        """,
                        (
                            keyword_id,
                            text,
                            group_id,
                            order,
                            str(item.get("note") or ""),
                            item.get("created_at") or now,
                            item.get("updated_at") or now,
                            int(state["is_pinned"]) if state else 0,
                            state["pin_order"] if state else None,
                            state["topic"] if state else (
                                default_topic if default_topic != text else None
                            ),
                            state["keyword_bucket"] if state else infer_keyword_bucket(text),
                            int(state["batch_default_selected"]) if state else 1,
                        ),
                    )

            for item in normalized:
                keyword_id = str(item.get("keyword_id") or "").strip()
                text = str(item.get("keyword_text") or "").strip()
                if not keyword_id or not text:
                    continue
                if keyword_id in active_ids:
                    conn.execute(
                        """
                        UPDATE keyword_registry
                        SET first_seen_at = ?, last_seen_at = ?, snapshot_count = ?
                        WHERE keyword_id = ?
                        """,
                        (
                            item.get("first_seen_at"),
                            item.get("last_seen_at"),
                            int(item.get("snapshot_count") or 0),
                            keyword_id,
                        ),
                    )
                    continue
                state = legacy_settings.get(keyword_id)
                default_topic = infer_topic(text)
                conn.execute(
                    """
                    INSERT INTO keyword_registry (
                        keyword_id, keyword_text, status, source, group_id, keyword_order,
                        note, created_at, updated_at, archived_at,
                        is_pinned, pin_order, topic, keyword_bucket,
                        batch_default_selected, first_seen_at, last_seen_at, snapshot_count
                    ) VALUES (?, ?, 'archived', 'observed', NULL, NULL, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        keyword_id,
                        text,
                        item.get("first_seen_at") or now,
                        now,
                        now,
                        int(state["is_pinned"]) if state else 0,
                        state["pin_order"] if state else None,
                        state["topic"] if state else (
                            default_topic if default_topic != text else None
                        ),
                        state["keyword_bucket"] if state else infer_keyword_bucket(text),
                        int(state["batch_default_selected"]) if state else 1,
                        item.get("first_seen_at"),
                        item.get("last_seen_at"),
                        int(item.get("snapshot_count") or 0),
                    ),
                )

            if drop_legacy_settings and has_legacy:
                conn.execute("DROP TABLE keyword_settings")

        return self.counts()

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS total FROM keyword_registry GROUP BY status"
            ).fetchall()
        result = {row["status"]: int(row["total"]) for row in rows}
        return {
            "total": sum(result.values()),
            "active": result.get(ACTIVE, 0),
            "archived": result.get(ARCHIVED, 0),
        }

    def load_payload(self, *, include_archived: bool = False) -> dict[str, Any]:
        status_clause = "" if include_archived else "WHERE k.status = 'active'"
        with self._connect() as conn:
            groups = conn.execute(
                """
                SELECT group_id, label, display_order, created_at, updated_at
                FROM keyword_groups
                WHERE archived_at IS NULL
                ORDER BY display_order, label
                """
            ).fetchall()
            rows = conn.execute(
                f"""
                SELECT k.*
                FROM keyword_registry k
                {status_clause}
                ORDER BY COALESCE(k.keyword_order, 999999), k.keyword_text
                """
            ).fetchall()

        by_group: dict[str, list[dict[str, Any]]] = {}
        archived: list[dict[str, Any]] = []
        for row in rows:
            item = self._row_to_item(row)
            if row["status"] == ARCHIVED or not row["group_id"]:
                archived.append(item)
            else:
                by_group.setdefault(row["group_id"], []).append(item)

        payload_groups = []
        for group in groups:
            payload_groups.append({
                "group_id": group["group_id"],
                "label": group["label"],
                "order": group["display_order"],
                "created_at": group["created_at"],
                "updated_at": group["updated_at"],
                "keywords": by_group.get(group["group_id"], []),
            })
        if include_archived and archived:
            payload_groups.append({
                "group_id": "grp_archived",
                "label": "历史归档",
                "order": 999999,
                "keywords": archived,
            })
        return {
            "version": 2,
            "updated_at": self._now(),
            "groups": payload_groups,
        }

    def load(self) -> dict[str, Any]:
        """Compatibility name for services that consume grouped active keywords."""
        return self.load_payload()

    def list_keywords(self, *, include_archived: bool = True) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE status = 'active'"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM keyword_registry
                {where}
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                         COALESCE(keyword_order, 999999), keyword_text
                """
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def list_settings(self) -> dict[str, dict[str, Any]]:
        return {
            item["keyword_id"]: item
            for item in self.list_keywords(include_archived=True)
        }

    def active_keyword_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT keyword_id FROM keyword_registry WHERE status = 'active'"
            ).fetchall()
        return {row["keyword_id"] for row in rows}

    def create_group(self, label: str) -> dict[str, Any]:
        label = str(label or "").strip()
        if not label:
            raise ValueError("分组名称不能为空")
        now = self._now()
        group_id = f"grp_{hashlib.md5(label.encode('utf-8')).hexdigest()[:8]}"
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM keyword_groups WHERE label = ? AND archived_at IS NULL",
                (label,),
            ).fetchone():
                raise ValueError(f"分组已存在：{label}")
            order = conn.execute(
                "SELECT COALESCE(MAX(display_order), 0) + 1 FROM keyword_groups WHERE archived_at IS NULL"
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO keyword_groups
                    (group_id, label, display_order, created_at, updated_at, archived_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (group_id, label, order, now, now),
            )
        return {"group_id": group_id, "label": label, "order": order, "keywords": []}

    def update_group(
        self,
        group_id: str,
        label: str | None = None,
        order: int | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM keyword_groups WHERE group_id = ? AND archived_at IS NULL",
                (group_id,),
            ).fetchone()
            if not row:
                raise FileNotFoundError(f"group not found: {group_id}")
            new_label = row["label"] if label is None else str(label or "").strip()
            if not new_label:
                raise ValueError("分组名称不能为空")
            duplicate = conn.execute(
                """
                SELECT 1 FROM keyword_groups
                WHERE label = ? AND group_id != ? AND archived_at IS NULL
                """,
                (new_label, group_id),
            ).fetchone()
            if duplicate:
                raise ValueError(f"分组已存在：{new_label}")
            new_order = row["display_order"] if order is None else int(order)
            conn.execute(
                """
                UPDATE keyword_groups
                SET label = ?, display_order = ?, updated_at = ?
                WHERE group_id = ?
                """,
                (new_label, new_order, now, group_id),
            )
        return {
            "group_id": group_id,
            "label": new_label,
            "order": new_order,
        }

    def delete_group(self, group_id: str) -> dict[str, Any]:
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM keyword_groups WHERE group_id = ? AND archived_at IS NULL",
                (group_id,),
            ).fetchone()
            if not row:
                raise FileNotFoundError(f"group not found: {group_id}")
            active_count = conn.execute(
                """
                SELECT COUNT(*) FROM keyword_registry
                WHERE group_id = ? AND status = 'active'
                """,
                (group_id,),
            ).fetchone()[0]
            if active_count:
                raise ValueError("请先清空分组内关键词，再删除分组")
            conn.execute(
                "UPDATE keyword_groups SET archived_at = ?, updated_at = ? WHERE group_id = ?",
                (now, now, group_id),
            )
        return {"group_id": group_id, "deleted": True}

    def create_keyword(
        self,
        group_id: str,
        keyword_text: str,
        note: str = "",
        *,
        source: str = "manual",
    ) -> dict[str, Any]:
        text = str(keyword_text or "").strip()
        if not text:
            raise ValueError("关键词不能为空")
        keyword_id = kw_id(text)
        now = self._now()
        with self._connect() as conn:
            if not conn.execute(
                "SELECT 1 FROM keyword_groups WHERE group_id = ? AND archived_at IS NULL",
                (group_id,),
            ).fetchone():
                raise FileNotFoundError(f"group not found: {group_id}")
            row = conn.execute(
                "SELECT * FROM keyword_registry WHERE keyword_id = ? OR keyword_text = ?",
                (keyword_id, text),
            ).fetchone()
            order = conn.execute(
                """
                SELECT COALESCE(MAX(keyword_order), 0) + 1
                FROM keyword_registry
                WHERE group_id = ? AND status = 'active'
                """,
                (group_id,),
            ).fetchone()[0]
            if row:
                if row["status"] == ACTIVE:
                    raise ValueError(f"关键词已存在：{text}")
                conn.execute(
                    """
                    UPDATE keyword_registry
                    SET status = 'active', source = ?, group_id = ?,
                        keyword_order = ?, note = ?, archived_at = NULL,
                        archive_reason_code = NULL, archive_reason_detail = NULL,
                        updated_at = ?
                    WHERE keyword_id = ?
                    """,
                    (
                        str(source or "manual").strip() or "manual",
                        group_id,
                        order,
                        str(note or "").strip(),
                        now,
                        row["keyword_id"],
                    ),
                )
                keyword_id = row["keyword_id"]
            else:
                default_topic = infer_topic(text)
                conn.execute(
                    """
                    INSERT INTO keyword_registry (
                        keyword_id, keyword_text, status, source, group_id, keyword_order,
                        note, created_at, updated_at, archived_at,
                        is_pinned, pin_order, topic, keyword_bucket,
                        batch_default_selected, first_seen_at, last_seen_at, snapshot_count,
                        refresh_frequency_days, refresh_frequency_source,
                        refresh_policy_reason, last_refresh_at,
                        last_refresh_attempt_at, last_refresh_status
                    ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?, 1, NULL, NULL, 0, 1, 'auto', '新词：观察期每日刷新', NULL, NULL, NULL)
                    """,
                    (
                        keyword_id,
                        text,
                        str(source or "manual").strip() or "manual",
                        group_id,
                        order,
                        str(note or "").strip(),
                        now,
                        now,
                        default_topic if default_topic != text else None,
                        infer_keyword_bucket(text),
                    ),
                )
        return self.get(keyword_id) or {}

    def set_refresh_policy(
        self,
        keyword_id: str,
        *,
        refresh_frequency_days: int | None = None,
        source: str = MANUAL_REFRESH_POLICY,
    ) -> dict[str, Any]:
        row = self.get(keyword_id)
        if not row or row["status"] != ACTIVE:
            raise FileNotFoundError(f"keyword not found: {keyword_id}")

        source = str(source or "").strip().lower()
        if source not in {AUTO_REFRESH_POLICY, MANUAL_REFRESH_POLICY}:
            raise ValueError("refresh policy source must be auto or manual")

        if source == AUTO_REFRESH_POLICY:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE keyword_registry
                    SET refresh_frequency_days = 1,
                        refresh_frequency_source = 'auto',
                        refresh_policy_reason = '自动策略：等待下一次评估',
                        updated_at = ?
                    WHERE keyword_id = ? AND status = 'active'
                    """,
                    (self._now(), keyword_id),
                )
            return self.get(keyword_id) or {}

        if refresh_frequency_days is None:
            raise ValueError("refresh_frequency_days is required for manual policy")
        days = int(refresh_frequency_days)
        if days not in SUPPORTED_REFRESH_FREQUENCIES:
            raise ValueError("refresh_frequency_days must be one of 1, 3, 7, 15")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keyword_registry
                SET refresh_frequency_days = ?,
                    refresh_frequency_source = 'manual',
                    refresh_policy_reason = ?,
                    updated_at = ?
                WHERE keyword_id = ? AND status = 'active'
                """,
                (days, f"人工设定：每 {days} 天刷新", self._now(), keyword_id),
            )
        return self.get(keyword_id) or {}

    def set_commercial_value(
        self,
        keyword_id: str,
        *,
        score: int,
        reason: str,
        source: str = "auto",
    ) -> dict[str, Any]:
        row = self.get(keyword_id)
        if not row:
            raise FileNotFoundError(f"keyword not found: {keyword_id}")
        value = int(score)
        if value < 1 or value > 10:
            raise ValueError("commercial value score must be between 1 and 10")
        source = str(source or "auto").strip().lower()
        if source not in {"auto", "manual"}:
            raise ValueError("commercial value source must be auto or manual")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keyword_registry
                SET commercial_value_score = ?,
                    commercial_value_source = ?,
                    commercial_value_reason = ?,
                    updated_at = ?
                WHERE keyword_id = ?
                """,
                (
                    value,
                    source,
                    str(reason or "").strip(),
                    self._now(),
                    keyword_id,
                ),
            )
        return self.get(keyword_id) or {}

    def apply_auto_commercial_values(
        self,
        updates: dict[str, dict[str, Any]],
    ) -> int:
        """批量写入自动商业价值分；人工评分不会被覆盖。"""
        if not updates:
            return 0
        now = self._now()
        updated = 0
        with self._connect() as conn:
            for keyword_id, item in updates.items():
                score = int(item.get("commercial_value_score") or 0)
                if score < 1 or score > 10:
                    continue
                result = conn.execute(
                    """
                    UPDATE keyword_registry
                    SET commercial_value_score = ?,
                        commercial_value_reason = ?,
                        updated_at = ?
                    WHERE keyword_id = ?
                      AND commercial_value_source = 'auto'
                    """,
                    (
                        score,
                        str(item.get("commercial_value_reason") or "").strip(),
                        now,
                        keyword_id,
                    ),
                )
                updated += int(result.rowcount or 0)
        return updated

    def set_discovery_lifecycle(
        self,
        keyword_id: str,
        *,
        lifecycle_stage: str,
        observation_started_at: str | None = None,
        observation_deadline_at: str | None = None,
        discovery_candidate_id: str | None = None,
        auto_archive_locked: bool | None = None,
    ) -> dict[str, Any]:
        row = self.get(keyword_id)
        if not row:
            raise FileNotFoundError(f"keyword not found: {keyword_id}")
        lock_value = (
            int(row.get("auto_archive_locked") or 0)
            if auto_archive_locked is None
            else (1 if auto_archive_locked else 0)
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keyword_registry
                SET lifecycle_stage = ?,
                    observation_started_at = ?,
                    observation_deadline_at = ?,
                    discovery_candidate_id = ?,
                    auto_archive_locked = ?,
                    updated_at = ?
                WHERE keyword_id = ?
                """,
                (
                    str(lifecycle_stage or "established").strip() or "established",
                    observation_started_at,
                    observation_deadline_at,
                    discovery_candidate_id,
                    lock_value,
                    self._now(),
                    keyword_id,
                ),
            )
        return self.get(keyword_id) or {}

    def set_auto_archive_lock(self, keyword_id: str, locked: bool) -> dict[str, Any]:
        row = self.get(keyword_id)
        if not row:
            raise FileNotFoundError(f"keyword not found: {keyword_id}")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keyword_registry
                SET auto_archive_locked = ?, updated_at = ?
                WHERE keyword_id = ?
                """,
                (1 if locked else 0, self._now(), keyword_id),
            )
        return self.get(keyword_id) or {}

    def apply_auto_refresh_policies(
        self,
        updates: dict[str, dict[str, Any]],
    ) -> int:
        """批量写入可复算的自动刷新结论；人工锁定的词永远不覆盖。"""
        if not updates:
            return 0
        updated = 0
        now = self._now()
        with self._connect() as conn:
            for keyword_id, item in updates.items():
                days = int(item.get("refresh_frequency_days") or 1)
                if days not in SUPPORTED_REFRESH_FREQUENCIES:
                    continue
                reason = str(item.get("refresh_policy_reason") or "").strip()
                result = conn.execute(
                    """
                    UPDATE keyword_registry
                    SET refresh_frequency_days = ?,
                        refresh_policy_reason = ?,
                        updated_at = ?
                    WHERE keyword_id = ?
                      AND status = 'active'
                      AND refresh_frequency_source = 'auto'
                    """,
                    (days, reason, now, keyword_id),
                )
                updated += int(result.rowcount or 0)
        return updated

    def record_refresh_results(
        self,
        *,
        succeeded_keyword_ids: list[str] | set[str],
        failed_keyword_ids: list[str] | set[str],
        refreshed_at: str | None = None,
    ) -> dict[str, int]:
        """写入抓取状态。失败不覆盖上次成功刷新时间，避免旧数据被当作新数据。"""
        now = refreshed_at or self._now()
        return self.record_refresh_events(
            succeeded_at_by_id={
                str(item or "").strip(): now
                for item in succeeded_keyword_ids
                if str(item or "").strip()
            },
            failed_keyword_ids=failed_keyword_ids,
            refreshed_at=now,
        )

    def record_refresh_events(
        self,
        *,
        succeeded_at_by_id: dict[str, str],
        failed_keyword_ids: list[str] | set[str],
        refreshed_at: str | None = None,
    ) -> dict[str, int]:
        """按逐词实际完成时间写状态，避免长批次把所有词的时钟推到批次末尾。"""
        now = refreshed_at or self._now()
        succeeded_times = {
            str(keyword_id or "").strip(): str(completed_at or now)
            for keyword_id, completed_at in succeeded_at_by_id.items()
            if str(keyword_id or "").strip()
        }
        succeeded = set(succeeded_times)
        failed = {str(item or "").strip() for item in failed_keyword_ids} - succeeded
        succeeded.discard("")
        failed.discard("")
        with self._connect() as conn:
            success_count = 0
            failure_count = 0
            for keyword_id, completed_at in sorted(succeeded_times.items()):
                result = conn.execute(
                    """
                    UPDATE keyword_registry
                    SET last_refresh_at = ?,
                        last_refresh_attempt_at = ?,
                        last_refresh_status = 'success',
                        updated_at = ?
                    WHERE keyword_id = ?
                      AND status = 'active'
                    """,
                    (completed_at, completed_at, now, keyword_id),
                )
                success_count += int(result.rowcount or 0)
            if failed:
                placeholders = ",".join("?" for _ in failed)
                result = conn.execute(
                    f"""
                    UPDATE keyword_registry
                    SET last_refresh_attempt_at = ?,
                        last_refresh_status = 'failed',
                        updated_at = ?
                    WHERE keyword_id IN ({placeholders})
                      AND status = 'active'
                    """,
                    (now, now, *sorted(failed)),
                )
                failure_count = int(result.rowcount or 0)
        return {"succeeded": success_count, "failed": failure_count}

    def update_keyword(
        self,
        keyword_id: str,
        keyword_text: str | None = None,
        note: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, Any]:
        row = self.get(keyword_id)
        if not row or row["status"] != ACTIVE:
            raise FileNotFoundError(f"keyword not found: {keyword_id}")
        text = row["keyword_text"] if keyword_text is None else str(keyword_text or "").strip()
        if not text:
            raise ValueError("关键词不能为空")
        if text != row["keyword_text"]:
            # Preserve historical identity: text changes archive the old entity and
            # create/reactivate the new deterministic keyword ID.
            target_group = group_id or row["group_id"]
            new_item = self.create_keyword(
                target_group,
                text,
                row["note"] if note is None else note,
            )
            self.archive_keyword(keyword_id)
            return new_item

        now = self._now()
        target_group = group_id or row["group_id"]
        with self._connect() as conn:
            if target_group and not conn.execute(
                "SELECT 1 FROM keyword_groups WHERE group_id = ? AND archived_at IS NULL",
                (target_group,),
            ).fetchone():
                raise FileNotFoundError(f"group not found: {target_group}")
            conn.execute(
                """
                UPDATE keyword_registry
                SET group_id = ?, note = ?, updated_at = ?
                WHERE keyword_id = ? AND status = 'active'
                """,
                (
                    target_group,
                    row["note"] if note is None else str(note or "").strip(),
                    now,
                    keyword_id,
                ),
            )
        return self.get(keyword_id) or {}

    def archive_keyword(
        self,
        keyword_id: str,
        *,
        reason_code: str | None = None,
        reason_detail: str | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM keyword_registry WHERE keyword_id = ? AND status = 'active'",
                (keyword_id,),
            ).fetchone()
            if not row:
                raise FileNotFoundError(f"keyword not found: {keyword_id}")
            conn.execute(
                """
                UPDATE keyword_registry
                SET status = 'archived', archived_at = ?, updated_at = ?,
                    is_pinned = 0, pin_order = NULL,
                    lifecycle_stage = 'archived',
                    archive_reason_code = ?,
                    archive_reason_detail = ?
                WHERE keyword_id = ?
                """,
                (
                    now,
                    now,
                    str(reason_code or "").strip() or None,
                    str(reason_detail or "").strip() or None,
                    keyword_id,
                ),
            )
        return {
            "keyword_id": keyword_id,
            "deleted": True,
            "status": ARCHIVED,
            "archive_reason_code": str(reason_code or "").strip() or None,
        }

    def get(self, keyword_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM keyword_registry WHERE keyword_id = ?",
                (keyword_id,),
            ).fetchone()
        return self._row_to_item(row) if row else None

    def set_pin_state(self, keyword_id: str, keyword_text: str, is_pinned: bool) -> dict[str, Any]:
        row = self._require_keyword(keyword_id, keyword_text)
        now = self._now()
        with self._connect() as conn:
            if is_pinned and not row["is_pinned"]:
                pin_order = conn.execute(
                    "SELECT COALESCE(MAX(pin_order), 0) + 1 FROM keyword_registry WHERE is_pinned = 1"
                ).fetchone()[0]
            else:
                pin_order = row["pin_order"] if is_pinned else None
            conn.execute(
                """
                UPDATE keyword_registry
                SET is_pinned = ?, pin_order = ?, updated_at = ?
                WHERE keyword_id = ?
                """,
                (1 if is_pinned else 0, pin_order, now, keyword_id),
            )
        return self.get(keyword_id) or {}

    def set_topic(self, keyword_id: str, keyword_text: str, topic: str | None) -> dict[str, Any]:
        self._require_keyword(keyword_id, keyword_text)
        return self._set_field(keyword_id, "topic", str(topic or "").strip() or None)

    def set_bucket(
        self,
        keyword_id: str,
        keyword_text: str,
        keyword_bucket: str | None,
    ) -> dict[str, Any]:
        self._require_keyword(keyword_id, keyword_text)
        return self._set_field(
            keyword_id,
            "keyword_bucket",
            str(keyword_bucket or "").strip() or None,
        )

    def set_note(self, keyword_id: str, keyword_text: str, note: str) -> dict[str, Any]:
        self._require_keyword(keyword_id, keyword_text)
        return self._set_field(keyword_id, "note", str(note or ""))

    def save_batch_default_selection(self, items: list[dict[str, Any]]) -> None:
        now = self._now()
        with self._connect() as conn:
            for item in items:
                keyword_id = str(item.get("keyword_id") or "").strip()
                if not keyword_id:
                    continue
                conn.execute(
                    """
                    UPDATE keyword_registry
                    SET batch_default_selected = ?, updated_at = ?
                    WHERE keyword_id = ? AND status = 'active'
                    """,
                    (
                        1 if bool(item.get("batch_default_selected", True)) else 0,
                        now,
                        keyword_id,
                    ),
                )

    def _require_keyword(self, keyword_id: str, keyword_text: str) -> dict[str, Any]:
        row = self.get(keyword_id)
        if row:
            return row
        # API callers may set state immediately after creating a new word.
        raise FileNotFoundError(f"keyword not found: {keyword_id or keyword_text}")

    def _set_field(self, keyword_id: str, field: str, value: Any) -> dict[str, Any]:
        if field not in {"topic", "keyword_bucket", "note"}:
            raise ValueError(f"unsupported keyword field: {field}")
        with self._connect() as conn:
            conn.execute(
                f"UPDATE keyword_registry SET {field} = ?, updated_at = ? WHERE keyword_id = ?",
                (value, self._now(), keyword_id),
            )
        return self.get(keyword_id) or {}

    def sync_observations(self, keywords: list[dict[str, Any]]) -> None:
        now = self._now()
        with self._connect() as conn:
            for item in keywords:
                keyword_id = str(item.get("keyword_id") or "").strip()
                text = str(item.get("keyword_text") or "").strip()
                if not keyword_id or not text:
                    continue
                default_topic = infer_topic(text)
                conn.execute(
                    """
                    INSERT INTO keyword_registry (
                        keyword_id, keyword_text, status, source, group_id, keyword_order,
                        note, created_at, updated_at, archived_at,
                        is_pinned, pin_order, topic, keyword_bucket,
                        batch_default_selected, first_seen_at, last_seen_at, snapshot_count
                    ) VALUES (?, ?, 'archived', 'observed', NULL, NULL, '', ?, ?, ?, 0, NULL, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(keyword_id) DO UPDATE SET
                        first_seen_at = excluded.first_seen_at,
                        last_seen_at = excluded.last_seen_at,
                        snapshot_count = excluded.snapshot_count,
                        updated_at = excluded.updated_at
                    """,
                    (
                        keyword_id,
                        text,
                        item.get("first_seen_at") or now,
                        now,
                        now,
                        default_topic if default_topic != text else None,
                        infer_keyword_bucket(text),
                        item.get("first_seen_at"),
                        item.get("last_seen_at"),
                        int(item.get("snapshot_count") or 0),
                    ),
                )

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
        frequency_days = int(row["refresh_frequency_days"] or 1)
        effective_interval_hours = effective_refresh_interval_hours(
            {
                "lifecycle_stage": row["lifecycle_stage"] or "established",
                "refresh_frequency_source": (
                    row["refresh_frequency_source"] or AUTO_REFRESH_POLICY
                ),
                "refresh_frequency_days": frequency_days,
            }
        )
        last_refresh_at = row["last_refresh_at"] or row["last_seen_at"]
        next_refresh_at = None
        refresh_age_days = None
        is_refresh_due = True
        if last_refresh_at:
            try:
                refreshed_dt = datetime.fromisoformat(str(last_refresh_at))
                next_dt = refreshed_dt + timedelta(hours=effective_interval_hours)
                next_refresh_at = next_dt.isoformat(timespec="seconds")
                refresh_age_days = max(
                    0,
                    int((datetime.now() - refreshed_dt).total_seconds() // 86400),
                )
                is_refresh_due = datetime.now() >= next_dt
            except ValueError:
                pass
        return {
            "keyword_id": row["keyword_id"],
            "keyword_text": row["keyword_text"],
            "status": row["status"],
            "enabled": row["status"] == ACTIVE,
            "is_active": row["status"] == ACTIVE,
            "source": row["source"],
            "group_id": row["group_id"],
            "keyword_order": row["keyword_order"],
            "note": row["note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
            "is_pinned": bool(row["is_pinned"]),
            "pin_order": row["pin_order"],
            "topic": row["topic"],
            "keyword_bucket": row["keyword_bucket"],
            "batch_default_selected": bool(row["batch_default_selected"]),
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "snapshot_count": int(row["snapshot_count"] or 0),
            "refresh_frequency_days": frequency_days,
            "effective_refresh_interval_hours": effective_interval_hours,
            "refresh_frequency_source": row["refresh_frequency_source"] or AUTO_REFRESH_POLICY,
            "refresh_policy_reason": row["refresh_policy_reason"] or "",
            "last_refresh_at": last_refresh_at,
            "last_refresh_attempt_at": row["last_refresh_attempt_at"],
            "last_refresh_status": row["last_refresh_status"],
            "next_refresh_at": next_refresh_at,
            "refresh_age_days": refresh_age_days,
            "is_refresh_due": is_refresh_due,
            "commercial_value_score": int(row["commercial_value_score"] or 5),
            "commercial_value_source": row["commercial_value_source"] or "auto",
            "commercial_value_reason": row["commercial_value_reason"] or "",
            "lifecycle_stage": row["lifecycle_stage"] or "established",
            "observation_started_at": row["observation_started_at"],
            "observation_deadline_at": row["observation_deadline_at"],
            "discovery_candidate_id": row["discovery_candidate_id"],
            "auto_archive_locked": bool(row["auto_archive_locked"]),
            "archive_reason_code": row["archive_reason_code"],
            "archive_reason_detail": row["archive_reason_detail"],
        }
