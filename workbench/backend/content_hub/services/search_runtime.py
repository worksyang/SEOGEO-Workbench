"""搜索刷新命令运行层。

微信与小红书保留各自旧页面/API 语义，但所有由 Hub 发起的刷新都先落入
command_runs + search_refresh_jobs + search_refresh_items。这样刷新不会只留下
一个 Toast 或旧服务 job id，而是具有幂等、状态、失败和恢复证据。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.domain.ids import generate_ulid_like


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SearchRefreshRuntime:
    def __init__(self, settings: Any, *, system_key: str, platform: str) -> None:
        self.settings = settings
        self.system_key = system_key
        self.platform = platform

    def begin(
        self,
        *,
        keyword_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        key = idempotency_key.strip()[:200]
        if not key:
            raise ValueError("刷新必须提供 idempotency_key。")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    existing = con.execute(
                        """
                        SELECT c.command_id, c.status, r.refresh_job_id, r.checkpoint_json
                        FROM command_runs c
                        JOIN search_refresh_jobs r ON r.command_id=c.command_id
                        WHERE c.module_key=? AND c.idempotency_key=?
                        """,
                        (self.system_key, key),
                    ).fetchone()
                    if existing:
                        return {
                            "created": False,
                            "command_id": existing["command_id"],
                            "refresh_job_id": existing["refresh_job_id"],
                            "status": existing["status"],
                            "checkpoint": json.loads(existing["checkpoint_json"] or "{}"),
                        }
                    command_id = generate_ulid_like("cmd")
                    refresh_job_id = generate_ulid_like("srj")
                    item_id = generate_ulid_like("sri")
                    now = _now()
                    con.execute(
                        """
                        INSERT INTO command_runs(
                            command_id,module_key,command_type,idempotency_key,actor_id,status,
                            confirmation_json,input_json,created_at,updated_at
                        ) VALUES(?,?,?,?,?,'running',?,?,?,?)
                        """,
                        (
                            command_id,
                            self.system_key,
                            "search.keyword_refresh",
                            key,
                            actor_id[:120] or "user",
                            json.dumps({"confirmed": True}, ensure_ascii=False),
                            json.dumps(
                                {"keyword_id": keyword_id, "platform": self.platform},
                                ensure_ascii=False,
                            ),
                            now,
                            now,
                        ),
                    )
                    con.execute(
                        """
                        INSERT INTO search_refresh_jobs(
                            refresh_job_id,system_key,platform,command_id,trigger_type,status,
                            requested_count,succeeded_count,failed_count,created_at,updated_at
                        ) VALUES(?,?,?,?,'manual','running',1,0,0,?,?)
                        """,
                        (refresh_job_id, self.system_key, self.platform, command_id, now, now),
                    )
                    con.execute(
                        """
                        INSERT INTO search_refresh_items(
                            refresh_item_id,refresh_job_id,keyword_id,ordinal,status,attempt_count
                        ) VALUES(?,?,?,0,'running',1)
                        """,
                        (item_id, refresh_job_id, keyword_id),
                    )
                    return {
                        "created": True,
                        "command_id": command_id,
                        "refresh_job_id": refresh_job_id,
                        "item_id": item_id,
                        "status": "running",
                    }

    def finish(
        self,
        refresh_job_id: str,
        *,
        status: str,
        external_result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"queued", "running", "succeeded", "partial_failed", "failed", "cancelled", "blocked"}:
            raise ValueError(f"非法刷新状态：{status}")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    job = con.execute(
                        "SELECT command_id FROM search_refresh_jobs WHERE refresh_job_id=?",
                        (refresh_job_id,),
                    ).fetchone()
                    if not job:
                        return
                    now = _now()
                    terminal = status in {"succeeded", "partial_failed", "failed", "cancelled", "blocked"}
                    outcome = "succeeded" if status == "succeeded" else "failed" if status in {"failed", "partial_failed"} else status
                    con.execute(
                        """
                        UPDATE search_refresh_jobs
                        SET status=?,succeeded_count=?,failed_count=?,checkpoint_json=?,
                            finished_at=CASE WHEN ? THEN ? ELSE finished_at END,updated_at=?
                        WHERE refresh_job_id=?
                        """,
                        (
                            status,
                            1 if status == "succeeded" else 0,
                            1 if status in {"failed", "partial_failed", "blocked"} else 0,
                            json.dumps({"external_result": external_result or {}}, ensure_ascii=False),
                            1 if terminal else 0,
                            now,
                            now,
                            refresh_job_id,
                        ),
                    )
                    con.execute(
                        """
                        UPDATE search_refresh_items
                        SET status=?,error_json=?,finished_at=CASE WHEN ? THEN ? ELSE finished_at END
                        WHERE refresh_job_id=?
                        """,
                        (
                            status,
                            json.dumps(error or {}, ensure_ascii=False),
                            1 if terminal else 0,
                            now,
                            refresh_job_id,
                        ),
                    )
                    con.execute(
                        """
                        UPDATE command_runs
                        SET status=?,output_json=?,error_json=?,updated_at=?
                        WHERE command_id=?
                        """,
                        (
                            outcome,
                            json.dumps({"refresh_job_id": refresh_job_id, "external_result": external_result or {}}, ensure_ascii=False),
                            json.dumps(error or {}, ensure_ascii=False),
                            now,
                            job["command_id"],
                        ),
                    )

    def status(self, refresh_job_id: str) -> dict[str, Any] | None:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                """
                SELECT r.*,c.status AS command_status,c.command_id,c.output_json,c.error_json
                FROM search_refresh_jobs r
                JOIN command_runs c ON c.command_id=r.command_id
                WHERE r.refresh_job_id=?
                """,
                (refresh_job_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "refresh_job_id": row["refresh_job_id"],
                "command_id": row["command_id"],
                "status": row["status"],
                "command_status": row["command_status"],
                "requested_count": row["requested_count"],
                "succeeded_count": row["succeeded_count"],
                "failed_count": row["failed_count"],
                "checkpoint": json.loads(row["checkpoint_json"] or "{}"),
                "output": json.loads(row["output_json"] or "{}"),
                "error": json.loads(row["error_json"] or "{}"),
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            }
