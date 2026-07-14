"""统一内容工作台 · 摄取层 / 增量检查点。

每个适配器可独立维护一组 checkpoint（{adapter_key, checkpoint_key}）。
checkpoint_value 存 last cursor、cursor 文件哈希或时间戳，便于：
- 增量重放：仅拉取 cursor 之后的批次，避免整目录重写。
- 重跑判定：source hash 变化时强制重新解析。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from ..validation.timestamps import utc_now_iso


@dataclass(slots=True)
class Checkpoint:
    adapter_key: str
    checkpoint_key: str
    cursor_value: str
    source_hash: str
    last_success_at: str
    batch_id: str
    payload: dict[str, Any]

    @classmethod
    def from_row(cls, row: sqlite3.Row | tuple) -> "Checkpoint":
        keys = (
            "adapter_key",
            "checkpoint_key",
            "cursor_value",
            "source_hash",
            "last_success_at",
            "batch_id",
            "payload_json",
        )
        if isinstance(row, sqlite3.Row):
            values = {key: row[key] for key in keys}
        else:
            values = {
                "adapter_key": row[0],
                "checkpoint_key": row[1],
                "cursor_value": row[2],
                "source_hash": row[3],
                "last_success_at": row[4],
                "batch_id": row[5],
                "payload_json": row[6],
            }
        payload_raw = values["payload_json"]
        payload = json.loads(payload_raw) if payload_raw else {}
        return cls(
            adapter_key=values["adapter_key"],
            checkpoint_key=values["checkpoint_key"],
            cursor_value=values["cursor_value"] or "",
            source_hash=values["source_hash"] or "",
            last_success_at=values["last_success_at"] or "",
            batch_id=values["batch_id"] or "",
            payload=payload,
        )


def checkpoint_key_for(scope: str, name: str) -> str:
    """生成形如 ``scope::name`` 的 checkpoint_key。"""
    return f"{scope}::{name}"


class CheckpointStore:
    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    def upsert(
        self,
        *,
        adapter_key: str,
        checkpoint_key: str,
        cursor_value: str,
        source_hash: str = "",
        batch_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO ingestion_checkpoints(
                adapter_key, checkpoint_key, cursor_value, source_hash,
                last_success_at, batch_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(adapter_key, checkpoint_key) DO UPDATE SET
                cursor_value=excluded.cursor_value,
                source_hash=excluded.source_hash,
                last_success_at=excluded.last_success_at,
                batch_id=excluded.batch_id,
                payload_json=excluded.payload_json
            """,
            (
                adapter_key,
                checkpoint_key,
                cursor_value,
                source_hash,
                utc_now_iso(),
                batch_id,
                json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True),
            ),
        )

    def get(self, adapter_key: str, checkpoint_key: str) -> Checkpoint | None:
        row = self._conn.execute(
            """
            SELECT adapter_key, checkpoint_key, cursor_value, source_hash,
                   last_success_at, batch_id, payload_json
            FROM ingestion_checkpoints
            WHERE adapter_key=? AND checkpoint_key=?
            """,
            (adapter_key, checkpoint_key),
        ).fetchone()
        if not row:
            return None
        return Checkpoint.from_row(row)

    def latest_batch(self, adapter_key: str) -> Checkpoint | None:
        row = self._conn.execute(
            """
            SELECT adapter_key, checkpoint_key, cursor_value, source_hash,
                   last_success_at, batch_id, payload_json
            FROM ingestion_checkpoints
            WHERE adapter_key=?
            ORDER BY last_success_at DESC
            LIMIT 1
            """,
            (adapter_key,),
        ).fetchone()
        if not row:
            return None
        return Checkpoint.from_row(row)

    def list_for(self, adapter_key: str) -> list[Checkpoint]:
        rows = self._conn.execute(
            """
            SELECT adapter_key, checkpoint_key, cursor_value, source_hash,
                   last_success_at, batch_id, payload_json
            FROM ingestion_checkpoints
            WHERE adapter_key=?
            ORDER BY checkpoint_key
            """,
            (adapter_key,),
        ).fetchall()
        return [Checkpoint.from_row(row) for row in rows]
