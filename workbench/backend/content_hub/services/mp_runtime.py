"""公众号采集命令运行层。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.domain.ids import generate_ulid_like


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MpCollectionRuntime:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def begin(self, *, actor_id: str, idempotency_key: str, settings_json: dict[str, Any]) -> dict[str, Any]:
        key = idempotency_key.strip()[:200]
        if not key:
            raise ValueError("公众号采集必须提供 idempotency_key。")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    previous = con.execute(
                        """
                        SELECT c.command_id,c.status,j.collection_job_id
                        FROM command_runs c JOIN mp_collection_jobs j ON j.command_id=c.command_id
                        WHERE c.module_key='wechat-mp' AND c.idempotency_key=?
                        """,
                        (key,),
                    ).fetchone()
                    if previous:
                        return {
                            "created": False,
                            "command_id": previous["command_id"],
                            "collection_job_id": previous["collection_job_id"],
                            "status": previous["status"],
                        }
                    now = _now()
                    command_id = generate_ulid_like("cmd")
                    job_id = generate_ulid_like("mpj")
                    con.execute(
                        """
                        INSERT INTO command_runs(
                            command_id,module_key,command_type,idempotency_key,actor_id,status,
                            confirmation_json,input_json,created_at,updated_at
                        ) VALUES(?,'wechat-mp','mp.collection',?,?,'running',?,?,?,?)
                        """,
                        (
                            command_id, key, actor_id[:120] or "user",
                            json.dumps({"confirmed": True}, ensure_ascii=False),
                            json.dumps(settings_json, ensure_ascii=False),
                            now, now,
                        ),
                    )
                    con.execute(
                        """
                        INSERT INTO mp_collection_jobs(
                            collection_job_id,command_id,status,account_count,settings_json,created_at,updated_at
                        ) VALUES(?,?,'running',?,?,?,?)
                        """,
                        (
                            job_id, command_id,
                            int(settings_json.get("account_count") or 0),
                            json.dumps(settings_json, ensure_ascii=False),
                            now, now,
                        ),
                    )
                    return {"created": True, "command_id": command_id, "collection_job_id": job_id}

    def finish(self, collection_job_id: str, *, status: str, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
        if status not in {"queued", "running", "succeeded", "partial_failed", "failed", "cancelled", "blocked"}:
            raise ValueError("非法公众号任务状态")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = con.execute(
                        "SELECT command_id FROM mp_collection_jobs WHERE collection_job_id=?",
                        (collection_job_id,),
                    ).fetchone()
                    if not row:
                        return
                    now = _now()
                    terminal = status in {"succeeded", "partial_failed", "failed", "cancelled", "blocked"}
                    command_status = "succeeded" if status == "succeeded" else "failed" if status in {"failed", "partial_failed"} else status
                    con.execute(
                        """
                        UPDATE mp_collection_jobs SET status=?,checkpoint_json=?,
                          finished_at=CASE WHEN ? THEN ? ELSE finished_at END,updated_at=?
                        WHERE collection_job_id=?
                        """,
                        (status, json.dumps({"upstream": result or {}}, ensure_ascii=False), int(terminal), now, now, collection_job_id),
                    )
                    con.execute(
                        """
                        UPDATE command_runs SET status=?,output_json=?,error_json=?,updated_at=?
                        WHERE command_id=?
                        """,
                        (command_status, json.dumps(result or {}, ensure_ascii=False), json.dumps(error or {}, ensure_ascii=False), now, row["command_id"]),
                    )
                    con.execute(
                        """
                        INSERT INTO mp_collection_events(
                            collection_event_id,collection_job_id,event_type,status,message,details_json,occurred_at
                        ) VALUES(?,?,?, ?,NULL,?,?)
                        """,
                        (generate_ulid_like("mpe"), collection_job_id, "upstream_result", status, json.dumps({"result": result or {}, "error": error or {}}, ensure_ascii=False), now),
                    )
