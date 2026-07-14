from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from .config import DEFAULT_RUN_MP_IDS, DEFAULT_SETTINGS


DEFAULT_CATEGORY_NAME = "其他"
INSURANCE_CATEGORY_NAME = "港险"
DEFAULT_CATEGORIES = (INSURANCE_CATEGORY_NAME, DEFAULT_CATEGORY_NAME)
INSURANCE_KEYWORDS = (
    "港险",
    "香港保险",
    "保险",
    "保诚",
    "友邦",
    "安盛",
    "宏利",
    "心水保",
    "大湾通",
)


class AppStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_flags (
                    mp_id TEXT PRIMARY KEY,
                    mp_name TEXT NOT NULL DEFAULT '',
                    monitor_enabled INTEGER NOT NULL DEFAULT 1,
                    run_enabled INTEGER NOT NULL DEFAULT 1,
                    category_name TEXT NOT NULL DEFAULT '其他',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS categories (
                    name TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._ensure_account_flags_columns(conn)
            self._ensure_default_categories(conn)
            conn.commit()

    def _ensure_account_flags_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(account_flags)").fetchall()
        }
        if "category_name" not in columns:
            conn.execute(
                "ALTER TABLE account_flags ADD COLUMN category_name TEXT NOT NULL DEFAULT '其他'"
            )
            conn.execute(
                """
                UPDATE account_flags
                SET category_name = ?
                WHERE mp_id IN ({placeholders})
                """.format(placeholders=",".join("?" for _ in DEFAULT_RUN_MP_IDS)),
                (INSURANCE_CATEGORY_NAME, *DEFAULT_RUN_MP_IDS),
            )
            for keyword in INSURANCE_KEYWORDS:
                conn.execute(
                    """
                    UPDATE account_flags
                    SET category_name = ?
                    WHERE mp_name LIKE ?
                    """,
                    (INSURANCE_CATEGORY_NAME, f"%{keyword}%"),
                )

    def _ensure_default_categories(self, conn: sqlite3.Connection) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        for name in DEFAULT_CATEGORIES:
            conn.execute(
                """
                INSERT INTO categories(name, created_at)
                VALUES(?, ?)
                ON CONFLICT(name) DO NOTHING
                """,
                (name, now),
            )

    def _infer_category_name(self, mp_id: str, mp_name: str) -> str:
        if mp_id in DEFAULT_RUN_MP_IDS:
            return INSURANCE_CATEGORY_NAME
        if any(keyword in mp_name for keyword in INSURANCE_KEYWORDS):
            return INSURANCE_CATEGORY_NAME
        return DEFAULT_CATEGORY_NAME

    def get_settings(self) -> Dict[str, Any]:
        settings = dict(DEFAULT_SETTINGS)
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        for row in rows:
            try:
                settings[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                settings[row["key"]] = row["value"]
        return settings

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        allowed = set(DEFAULT_SETTINGS.keys())
        clean_updates = {key: value for key, value in updates.items() if key in allowed}
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            for key, value in clean_updates.items():
                conn.execute(
                    """
                    INSERT INTO settings(key, value, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
            conn.commit()
        return self.get_settings()

    def merge_account_flags(self, mps: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        accounts = []
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            for mp in mps:
                mp_id = str(mp.get("id") or mp.get("mp_id") or "").strip()
                if not mp_id:
                    continue
                mp_name = str(mp.get("mp_name") or mp.get("name") or "未知公众号")
                row = conn.execute(
                    """
                    SELECT monitor_enabled, run_enabled, category_name
                    FROM account_flags
                    WHERE mp_id = ?
                    """,
                    (mp_id,),
                ).fetchone()
                inferred_category = self._infer_category_name(mp_id, mp_name)
                conn.execute(
                    """
                    INSERT INTO categories(name, created_at)
                    VALUES(?, ?)
                    ON CONFLICT(name) DO NOTHING
                    """,
                    (inferred_category, now),
                )
                if row is None:
                    monitor_default = 1 if int(mp.get("status") or 0) == 1 else 0
                    run_default = 1 if mp_id in DEFAULT_RUN_MP_IDS else 0
                    conn.execute(
                        """
                        INSERT INTO account_flags(
                            mp_id, mp_name, monitor_enabled, run_enabled, category_name, updated_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (mp_id, mp_name, monitor_default, run_default, inferred_category, now),
                    )
                    monitor_enabled = bool(monitor_default)
                    run_enabled = bool(run_default)
                    category_name = inferred_category
                else:
                    category_name = str(row["category_name"] or "").strip() or inferred_category
                    if not row["category_name"]:
                        conn.execute(
                            """
                            UPDATE account_flags
                            SET category_name = ?, updated_at = ?
                            WHERE mp_id = ?
                            """,
                            (category_name, now, mp_id),
                        )
                    conn.execute(
                        "UPDATE account_flags SET mp_name = ?, updated_at = ? WHERE mp_id = ?",
                        (mp_name, now, mp_id),
                    )
                    monitor_enabled = bool(row["monitor_enabled"])
                    run_enabled = bool(row["run_enabled"])

                merged = dict(mp)
                merged["mp_id"] = mp_id
                merged["mp_name"] = mp_name
                merged["monitor_enabled"] = monitor_enabled
                merged["run_enabled"] = run_enabled
                merged["category_name"] = category_name
                merged["server_status"] = mp.get("status")
                accounts.append(merged)
            conn.commit()
        return accounts

    def update_account_flags(
        self,
        mp_id: str,
        monitor_enabled: Optional[bool] = None,
        run_enabled: Optional[bool] = None,
        category_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM account_flags WHERE mp_id = ?",
                (mp_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO account_flags(mp_id, mp_name, monitor_enabled, run_enabled, category_name, updated_at)
                    VALUES(?, '', 1, 1, ?, ?)
                    """,
                    (mp_id, DEFAULT_CATEGORY_NAME, now),
                )

            if monitor_enabled is not None:
                conn.execute(
                    "UPDATE account_flags SET monitor_enabled = ?, updated_at = ? WHERE mp_id = ?",
                    (1 if monitor_enabled else 0, now, mp_id),
                )
            if run_enabled is not None:
                conn.execute(
                    "UPDATE account_flags SET run_enabled = ?, updated_at = ? WHERE mp_id = ?",
                    (1 if run_enabled else 0, now, mp_id),
                )
            if category_name is not None:
                clean_category = self._normalize_category_name(category_name)
                conn.execute(
                    """
                    INSERT INTO categories(name, created_at)
                    VALUES(?, ?)
                    ON CONFLICT(name) DO NOTHING
                    """,
                    (clean_category, now),
                )
                conn.execute(
                    "UPDATE account_flags SET category_name = ?, updated_at = ? WHERE mp_id = ?",
                    (clean_category, now, mp_id),
                )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM account_flags WHERE mp_id = ?",
                (mp_id,),
            ).fetchone()

        return {
            "mp_id": updated["mp_id"],
            "mp_name": updated["mp_name"],
            "monitor_enabled": bool(updated["monitor_enabled"]),
            "run_enabled": bool(updated["run_enabled"]),
            "category_name": updated["category_name"],
            "updated_at": updated["updated_at"],
        }

    def _normalize_category_name(self, name: str) -> str:
        clean = str(name or "").strip()
        if not clean:
            raise ValueError("分类名称不能为空")
        if len(clean) > 24:
            raise ValueError("分类名称不能超过 24 个字符")
        return clean

    def list_categories(self) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.name, COUNT(a.mp_id) AS account_count, c.created_at
                FROM categories c
                LEFT JOIN account_flags a ON a.category_name = c.name
                GROUP BY c.name
                ORDER BY account_count DESC, c.created_at ASC, c.name ASC
                """
            ).fetchall()
        return [
            {
                "name": row["name"],
                "account_count": int(row["account_count"] or 0),
                "created_at": row["created_at"],
                "protected": row["name"] == DEFAULT_CATEGORY_NAME,
            }
            for row in rows
        ]

    def create_category(self, name: str) -> Dict[str, Any]:
        clean_name = self._normalize_category_name(name)
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO categories(name, created_at)
                VALUES(?, ?)
                ON CONFLICT(name) DO NOTHING
                """,
                (clean_name, now),
            )
            conn.commit()
        return {"name": clean_name, "account_count": 0, "created_at": now, "protected": False}

    def delete_category(self, name: str) -> None:
        clean_name = self._normalize_category_name(name)
        if clean_name == DEFAULT_CATEGORY_NAME:
            raise ValueError("默认分类不能删除")

        with self._lock, self._connect() as conn:
            self._ensure_default_categories(conn)
            row = conn.execute(
                "SELECT COUNT(*) AS account_count FROM account_flags WHERE category_name = ?",
                (clean_name,),
            ).fetchone()
            if int(row["account_count"] or 0) > 0:
                raise ValueError("分类下还有公众号，请先移动到其他分类后再删除")
            conn.execute("DELETE FROM categories WHERE name = ?", (clean_name,))
            conn.commit()

    def selected_run_mp_ids(self) -> Set[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT mp_id FROM account_flags WHERE run_enabled = 1"
            ).fetchall()
        return {str(row["mp_id"]) for row in rows}
