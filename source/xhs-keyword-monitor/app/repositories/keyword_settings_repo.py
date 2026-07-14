from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from app.keyword_bucket_resolver import infer_keyword_bucket
from app.topic_resolver import infer_topic


class KeywordSettingsRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS keyword_settings (
                    keyword_id TEXT PRIMARY KEY,
                    keyword_text TEXT NOT NULL,
                    is_pinned INTEGER NOT NULL DEFAULT 0,
                    pin_order INTEGER,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    topic TEXT,
                    keyword_bucket TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    batch_default_selected INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(keyword_settings)").fetchall()}
            if "topic" not in columns:
                conn.execute("ALTER TABLE keyword_settings ADD COLUMN topic TEXT")
            if "keyword_bucket" not in columns:
                conn.execute("ALTER TABLE keyword_settings ADD COLUMN keyword_bucket TEXT")
            if "batch_default_selected" not in columns:
                conn.execute("ALTER TABLE keyword_settings ADD COLUMN batch_default_selected INTEGER NOT NULL DEFAULT 1")
            conn.execute(
                """
                UPDATE keyword_settings
                SET pin_order = NULL
                WHERE is_pinned = 0 AND pin_order IS NOT NULL
                """
            )

    def list_all(self) -> dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT keyword_id, keyword_text, is_pinned, pin_order, is_active, topic, keyword_bucket, note, batch_default_selected, updated_at
                FROM keyword_settings
                """
            ).fetchall()
        return {
            row["keyword_id"]: {
                "keyword_id": row["keyword_id"],
                "keyword_text": row["keyword_text"],
                "is_pinned": bool(row["is_pinned"]),
                "pin_order": row["pin_order"],
                "is_active": bool(row["is_active"]),
                "topic": row["topic"],
                "keyword_bucket": row["keyword_bucket"],
                "note": row["note"],
                "batch_default_selected": bool(row["batch_default_selected"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def get(self, keyword_id: str) -> dict | None:
        return self.list_all().get(keyword_id)

    def set_pin_state(self, keyword_id: str, keyword_text: str, is_pinned: bool) -> dict:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT keyword_id, keyword_text, is_pinned, pin_order, is_active, topic, keyword_bucket, note, updated_at
                FROM keyword_settings
                WHERE keyword_id = ?
                """,
                (keyword_id,),
            ).fetchone()

            if is_pinned:
                if row and row["is_pinned"]:
                    pin_order = row["pin_order"]
                else:
                    pin_order = conn.execute(
                        "SELECT COALESCE(MAX(pin_order), 0) + 1 FROM keyword_settings WHERE is_pinned = 1"
                    ).fetchone()[0]

                if row:
                    conn.execute(
                        """
                        UPDATE keyword_settings
                        SET keyword_text = ?, is_pinned = 1, pin_order = ?, updated_at = ?
                        WHERE keyword_id = ?
                        """,
                        (keyword_text, pin_order, now, keyword_id),
                    )
                else:
                    default_topic = infer_topic(keyword_text)
                    topic_value = default_topic if default_topic != keyword_text else None
                    bucket_value = infer_keyword_bucket(keyword_text)
                    conn.execute(
                        """
                        INSERT INTO keyword_settings
                            (keyword_id, keyword_text, is_pinned, pin_order, is_active, topic, keyword_bucket, note, updated_at)
                        VALUES (?, ?, 1, ?, 1, ?, ?, '', ?)
                        """,
                        (keyword_id, keyword_text, pin_order, topic_value, bucket_value, now),
                    )
            else:
                if row:
                    conn.execute(
                        """
                        UPDATE keyword_settings
                        SET keyword_text = ?, is_pinned = 0, pin_order = NULL, updated_at = ?
                        WHERE keyword_id = ?
                        """,
                        (keyword_text, now, keyword_id),
                    )
                else:
                    default_topic = infer_topic(keyword_text)
                    topic_value = default_topic if default_topic != keyword_text else None
                    bucket_value = infer_keyword_bucket(keyword_text)
                    conn.execute(
                        """
                        INSERT INTO keyword_settings
                            (keyword_id, keyword_text, is_pinned, pin_order, is_active, topic, keyword_bucket, note, updated_at)
                        VALUES (?, ?, 0, NULL, 1, ?, ?, '', ?)
                        """,
                        (keyword_id, keyword_text, topic_value, bucket_value, now),
                    )

        state = self.get(keyword_id)
        if state is None:
            raise RuntimeError("failed to persist keyword setting")
        return state

    def set_topic(self, keyword_id: str, keyword_text: str, topic: str | None) -> dict:
        now = datetime.now().isoformat(timespec="seconds")
        topic_value = (topic or "").strip() or None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT keyword_id
                FROM keyword_settings
                WHERE keyword_id = ?
                """,
                (keyword_id,),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE keyword_settings
                    SET keyword_text = ?, topic = ?, updated_at = ?
                    WHERE keyword_id = ?
                    """,
                    (keyword_text, topic_value, now, keyword_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO keyword_settings
                        (keyword_id, keyword_text, is_pinned, pin_order, is_active, topic, note, updated_at)
                    VALUES (?, ?, 0, NULL, 1, ?, '', ?)
                    """,
                    (keyword_id, keyword_text, topic_value, now),
                )
        state = self.get(keyword_id)
        if state is None:
            raise RuntimeError("failed to persist keyword topic")
        return state

    def set_bucket(self, keyword_id: str, keyword_text: str, keyword_bucket: str | None) -> dict:
        now = datetime.now().isoformat(timespec="seconds")
        bucket_value = (keyword_bucket or "").strip() or None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT keyword_id
                FROM keyword_settings
                WHERE keyword_id = ?
                """,
                (keyword_id,),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE keyword_settings
                    SET keyword_text = ?, keyword_bucket = ?, updated_at = ?
                    WHERE keyword_id = ?
                    """,
                    (keyword_text, bucket_value, now, keyword_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO keyword_settings
                        (keyword_id, keyword_text, is_pinned, pin_order, is_active, topic, keyword_bucket, note, updated_at)
                    VALUES (?, ?, 0, NULL, 1, NULL, ?, '', ?)
                    """,
                    (keyword_id, keyword_text, bucket_value, now),
                )
        state = self.get(keyword_id)
        if state is None:
            raise RuntimeError("failed to persist keyword bucket")
        return state

    def sync_keywords(self, keywords: list[dict]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            for item in keywords:
                default_topic = infer_topic(item["keyword_text"])
                topic_value = default_topic if default_topic != item["keyword_text"] else None
                bucket_value = infer_keyword_bucket(item["keyword_text"])
                conn.execute(
                    """
                    INSERT INTO keyword_settings
                        (keyword_id, keyword_text, is_pinned, pin_order, is_active, topic, keyword_bucket, note, batch_default_selected, updated_at)
                    VALUES (?, ?, 0, NULL, 1, ?, ?, '', 1, ?)
                    ON CONFLICT(keyword_id) DO UPDATE SET
                        keyword_text = excluded.keyword_text,
                        topic = COALESCE(keyword_settings.topic, excluded.topic),
                        keyword_bucket = COALESCE(keyword_settings.keyword_bucket, excluded.keyword_bucket)
                    """,
                    (item["keyword_id"], item["keyword_text"], topic_value, bucket_value, now),
                )

    def save_batch_default_selection(self, items: list[dict[str, str | bool]]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            for item in items:
                keyword_id = str(item.get("keyword_id") or "").strip()
                keyword_text = str(item.get("keyword_text") or "").strip()
                if not keyword_id or not keyword_text:
                    continue
                selected = 1 if bool(item.get("batch_default_selected", True)) else 0
                default_topic = infer_topic(keyword_text)
                topic_value = default_topic if default_topic != keyword_text else None
                bucket_value = infer_keyword_bucket(keyword_text)
                conn.execute(
                    """
                    INSERT INTO keyword_settings
                        (keyword_id, keyword_text, is_pinned, pin_order, is_active, topic, keyword_bucket, note, batch_default_selected, updated_at)
                    VALUES (?, ?, 0, NULL, 1, ?, ?, '', ?, ?)
                    ON CONFLICT(keyword_id) DO UPDATE SET
                        keyword_text = excluded.keyword_text,
                        batch_default_selected = excluded.batch_default_selected,
                        updated_at = excluded.updated_at
                    """,
                    (keyword_id, keyword_text, topic_value, bucket_value, selected, now),
                )
