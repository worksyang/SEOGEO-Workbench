"""审计服务：把人工操作、敏感动作、事实修正写入 audit_log。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from ..validation.timestamps import utc_now_iso


class AuditService:
    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    def record(
        self,
        *,
        action: str,
        subject_type: str,
        subject_id: str,
        actor_id: str = "user",
        actor_type: str = "user",
        outcome: str = "succeeded",
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> str:
        audit_id = f"audit_{uuid.uuid4().hex[:16]}"
        self._conn.execute(
            """
            INSERT INTO audit_log(
                audit_id, occurred_at, actor_type, actor_id, action,
                subject_type, subject_id, request_id, outcome, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                utc_now_iso(),
                actor_type,
                actor_id,
                action,
                subject_type,
                subject_id,
                request_id,
                outcome,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        return audit_id
