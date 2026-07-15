"""命令双写回执：记录 legacy、hub 与 reconcile 的真实状态。"""
from __future__ import annotations

import json
from typing import Any

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.domain.ids import generate_ulid_like
from content_hub.services.migration import utc_now


class DualWriteReceiptService:
    def __init__(self, settings: Any, *, module_key: str) -> None:
        self.settings = settings
        self.module_key = module_key

    def record(
        self,
        *,
        command_id: str | None,
        idempotency_key: str,
        legacy_status: str,
        hub_status: str,
        reconcile_status: str,
        details: dict[str, Any] | None = None,
        command_type: str = "search.keyword_refresh",
        actor_id: str = "user",
    ) -> dict[str, str]:
        key = idempotency_key.strip()[:200]
        if not key:
            raise ValueError("双写回执必须提供 idempotency_key。")
        now = utc_now()
        resolved_command_id = command_id or generate_ulid_like("cmd")
        receipt_id = generate_ulid_like("dwr")
        payload = {
            "module_key": self.module_key,
            "legacy_status": legacy_status,
            "hub_status": hub_status,
            "reconcile_status": reconcile_status,
            **(details or {}),
        }
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    if command_id is None:
                        con.execute(
                            """
                            INSERT OR IGNORE INTO command_runs(
                                command_id,module_key,command_type,idempotency_key,actor_id,
                                status,input_json,output_json,error_json,created_at,updated_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                resolved_command_id,
                                self.module_key,
                                command_type,
                                key,
                                actor_id[:120] or "user",
                                "blocked" if reconcile_status == "blocked" else "failed",
                                json.dumps({"idempotency_key": key}, ensure_ascii=False),
                                "{}",
                                json.dumps(details or {}, ensure_ascii=False),
                                now,
                                now,
                            ),
                        )
                    con.execute(
                        """
                        INSERT INTO dual_write_receipts(
                            receipt_id,module_key,command_id,idempotency_key,
                            legacy_status,hub_status,reconcile_status,details_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(module_key,idempotency_key) DO UPDATE SET
                            command_id=excluded.command_id,
                            legacy_status=excluded.legacy_status,
                            hub_status=excluded.hub_status,
                            reconcile_status=excluded.reconcile_status,
                            details_json=excluded.details_json
                        """,
                        (
                            receipt_id,
                            self.module_key,
                            resolved_command_id,
                            key,
                            legacy_status,
                            hub_status,
                            reconcile_status,
                            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                            now,
                        ),
                    )
        return {"command_id": resolved_command_id, "receipt_id": receipt_id, "idempotency_key": key}
