"""持久任务服务：queued → running → succeeded / failed / cancelled / blocked；
支持 attempt_count、locked_by、scheduled_at，可被多进程串行化消费。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..domain.ids import generate_ulid_like
from ..validation.timestamps import utc_now_iso

ALLOWED_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "blocked",
)


class JobsService:
    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    def create(
        self,
        *,
        job_type: str,
        payload: dict[str, Any] | None = None,
        input_signal_ids: list[str] | None = None,
        source_content_ids: list[str] | None = None,
        scheduled_at: str | None = None,
        max_attempts: int = 3,
    ) -> str:
        job_id = generate_ulid_like("job")
        self._conn.execute(
            """
            INSERT INTO production_jobs(
                job_id, job_type, status, input_signal_ids_json,
                source_content_ids_json, created_at, updated_at, scheduled_at,
                payload_json
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                job_type,
                json.dumps(input_signal_ids or [], ensure_ascii=False),
                json.dumps(source_content_ids or [], ensure_ascii=False),
                utc_now_iso(),
                utc_now_iso(),
                scheduled_at or "",
                json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True),
            ),
        )
        self._record_event(job_id, "created", {"max_attempts": max_attempts})
        return job_id

    def claim(self, job_id: str, worker: str) -> bool:
        """尝试获取运行锁。仅当 status=queued 时可获取；locked_by/updated_at 一并更新。"""
        cur = self._conn.execute(
            "SELECT status, locked_by FROM production_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not cur or cur["status"] != "queued":
            return False
        self._conn.execute(
            """
            UPDATE production_jobs
            SET status='running', locked_by=?, locked_at=?, updated_at=?
            WHERE job_id=? AND status='queued'
            """,
            (worker, utc_now_iso(), utc_now_iso(), job_id),
        )
        self._record_event(job_id, "claimed", {"worker": worker})
        return True

    def complete(self, job_id: str, *, output_content_id: str = "", status: str = "succeeded") -> None:
        if status not in {"succeeded", "failed", "cancelled", "blocked"}:
            raise ValueError(f"非法结束状态：{status}")
        self._conn.execute(
            "UPDATE production_jobs SET status=?, output_content_id=COALESCE(?, output_content_id), updated_at=? WHERE job_id=?",
            (status, output_content_id or None, utc_now_iso(), job_id),
        )
        self._record_event(job_id, "completed", {"status": status, "output_content_id": output_content_id})

    def cancel(self, job_id: str, reason: str = "") -> bool:
        cur = self._conn.execute("SELECT status FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not cur:
            return False
        if cur["status"] in {"succeeded", "failed", "cancelled"}:
            return False
        self._conn.execute(
            "UPDATE production_jobs SET status='cancelled', updated_at=? WHERE job_id=?",
            (utc_now_iso(), job_id),
        )
        self._record_event(job_id, "cancelled", {"reason": reason})
        return True

    def detail(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["input_signal_ids"] = json.loads(result.pop("input_signal_ids_json") or "[]")
        result["source_content_ids"] = json.loads(result.pop("source_content_ids_json") or "[]")
        result["payload"] = json.loads(result.pop("payload_json") or "{}")
        events = [
            dict(event)
            for event in self._conn.execute(
                "SELECT event_id, event_type, occurred_at, payload_json FROM job_events WHERE job_id=? ORDER BY occurred_at",
                (job_id,),
            ).fetchall()
        ]
        for event in events:
            event["payload"] = json.loads(event.pop("payload_json") or "{}")
        result["events"] = events
        return result

    def list_recent(self, *, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT job_id, job_type, status, created_at, updated_at, scheduled_at FROM production_jobs "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _record_event(self, job_id: str, event: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO job_events(event_id, job_id, occurred_at, event_type, message, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"ev_{job_id}_{event}",
                job_id,
                utc_now_iso(),
                event,
                None,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
