from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.domain.ids import generate_ulid_like
from content_hub.errors import ConflictError, NotFoundError, ValidationAppError
from content_hub.features.xhs.service import XhsService
from content_hub.features.xhs.policy import reject_xhs_write
from content_hub.adapters.xhs_search_provider import DryRunXhsSearchProvider, XhsSearchProvider


SYSTEM = "xhs-search"
PLATFORM = "xiaohongshu"
UNASSIGNED_GROUP_ID = "xhs_group_unassigned"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _source_keyword_id(row: Any) -> str:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return str(payload.get("source_keyword_id") or row["keyword_id"])


def _frontend_status(status: str) -> str:
    return {
        "succeeded": "completed",
        "partial_failed": "completed_with_failures",
        "failed": "failed",
        "blocked": "failed",
        "cancelled": "cancelled",
        "queued": "running",
        "running": "running",
    }.get(status, "failed")


class XhsBatchAlreadyRunningError(ConflictError):
    def __init__(self, state: dict[str, Any]):
        self.state = state
        super().__init__("小红书已有批量刷新正在运行")


class XhsBatchRefreshService:
    def __init__(
        self,
        settings: Any,
        *,
        provider: XhsSearchProvider | None = None,
        actor_id: str = "user",
    ) -> None:
        self.settings = settings
        self.provider = provider or DryRunXhsSearchProvider()
        self.actor_id = actor_id or "user"

    def _existing(self, con, key: str, input_hash: str) -> dict[str, Any] | None:
        row = con.execute(
            """SELECT c.input_json,r.refresh_job_id
               FROM command_runs c
               LEFT JOIN search_refresh_jobs r ON r.command_id=c.command_id
               WHERE c.module_key=? AND c.idempotency_key=?""",
            (SYSTEM, key),
        ).fetchone()
        if row is None:
            return None
        if _hash(json.loads(row["input_json"] or "{}")) != input_hash:
            raise ConflictError("小红书批量刷新幂等键已用于不同输入")
        return self._job_payload(con, row["refresh_job_id"]) if row["refresh_job_id"] else None

    def _active(self, con) -> Any:
        return con.execute(
            """SELECT refresh_job_id
               FROM search_refresh_jobs
               WHERE system_key=? AND platform=?
                 AND status IN ('queued','running')
               ORDER BY created_at DESC LIMIT 1""",
            (SYSTEM, PLATFORM),
        ).fetchone()

    def _keyword_map(self, con) -> dict[str, dict[str, Any]]:
        rows = con.execute(
            """SELECT k.*,s.batch_default_selected
               FROM keywords k
               LEFT JOIN search_keyword_settings s
                 ON s.keyword_id=k.keyword_id
                AND s.system_key=? AND s.platform=?
               WHERE k.platform=? AND k.status='active'
               ORDER BY k.keyword_id""",
            (SYSTEM, PLATFORM, PLATFORM),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            source_id = _source_keyword_id(row)
            result[str(row["keyword_id"])] = item
            result[source_id] = item
        return result

    def _resolve_keyword_ids(self, con, requested: list[Any] | None) -> list[str]:
        mapping = self._keyword_map(con)
        if requested:
            resolved: list[str] = []
            invalid: list[str] = []
            for raw in requested:
                value = str(raw or "").strip()
                if not value:
                    continue
                row = mapping.get(value)
                if row is None:
                    invalid.append(value)
                    continue
                internal_id = str(row["keyword_id"])
                if internal_id not in resolved:
                    resolved.append(internal_id)
            if invalid:
                raise ValidationAppError(f"小红书关键词不存在：{', '.join(invalid[:5])}")
            return resolved
        resolved: list[str] = []
        seen: set[str] = set()
        for row in mapping.values():
            internal_id = str(row["keyword_id"])
            if internal_id in seen:
                continue
            if row.get("batch_default_selected") is not None and not bool(row["batch_default_selected"]):
                continue
            seen.add(internal_id)
            resolved.append(internal_id)
        return resolved

    def _create(self, con, *, keyword_ids: list[str], key: str, source: str) -> tuple[str, str]:
        now = _now()
        command_id = generate_ulid_like("cmd")
        job_id = generate_ulid_like("srj")
        payload = {"keyword_ids": keyword_ids, "source": source}
        con.execute(
            """INSERT INTO command_runs(
                command_id,module_key,command_type,idempotency_key,actor_id,status,
                confirmation_json,input_json,output_json,error_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,'running',?,?,?,?,?,?)""",
            (
                command_id,
                SYSTEM,
                "xhs.refresh_all",
                key,
                self.actor_id[:120],
                _json({"confirmed": True}),
                _json(payload),
                "{}",
                "{}",
                now,
                now,
            ),
        )
        con.execute(
            """INSERT INTO search_refresh_jobs(
                refresh_job_id,system_key,platform,command_id,trigger_type,status,
                requested_count,started_at,created_at,updated_at,trigger_source
            ) VALUES(?,?,?,?,?,'running',?,?,?,?,?)""",
            (job_id, SYSTEM, PLATFORM, command_id, "manual", len(keyword_ids), now, now, now, source),
        )
        for ordinal, keyword_id in enumerate(keyword_ids):
            con.execute(
                """INSERT INTO search_refresh_items(
                    refresh_item_id,refresh_job_id,keyword_id,ordinal,status,attempt_count,current_phase
                ) VALUES(?,?,?,?,'queued',0,'queued')""",
                (generate_ulid_like("sri"), job_id, keyword_id, ordinal),
            )
        return command_id, job_id

    def start_batch(
        self,
        *,
        keyword_ids: list[Any] | None,
        key: str,
        source: str = "web_refresh_all",
    ) -> dict[str, Any]:
        reject_xhs_write("refresh_batch_start")
        if not key.strip():
            raise ValidationAppError("小红书批量刷新必须提供幂等键")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    resolved = self._resolve_keyword_ids(con, keyword_ids)
                    if not resolved:
                        raise ValidationAppError("没有可刷新的小红书关键词")
                    payload = {"keyword_ids": resolved, "source": source}
                    old = self._existing(con, key, _hash(payload))
                    if old:
                        return old
                    active = self._active(con)
                    if active:
                        raise XhsBatchAlreadyRunningError(self._job_payload(con, active["refresh_job_id"]))
                    _, job_id = self._create(con, keyword_ids=resolved, key=key, source=source)
                    return self._job_payload(con, job_id)

    def _job_payload(self, con, job_id: str) -> dict[str, Any]:
        row = con.execute(
            "SELECT * FROM search_refresh_jobs WHERE refresh_job_id=? AND system_key=? AND platform=?",
            (job_id, SYSTEM, PLATFORM),
        ).fetchone()
        if row is None:
            raise NotFoundError("小红书刷新批次", job_id)
        items = [
            dict(item)
            for item in con.execute(
                """SELECT i.*,k.keyword,k.payload_json AS keyword_payload
                   FROM search_refresh_items i
                   JOIN keywords k ON k.keyword_id=i.keyword_id
                   WHERE i.refresh_job_id=? ORDER BY i.ordinal""",
                (job_id,),
            ).fetchall()
        ]
        succeeded = [item for item in items if item["status"] == "succeeded"]
        failed = [item for item in items if item["status"] in {"failed", "blocked"}]
        cancelled = [item for item in items if item["status"] == "cancelled"]
        active = row["status"] in {"queued", "running"}
        hub_status = str(row["status"])
        if row["cancel_requested"] and active:
            hub_status = "cancelling"
        frontend = "running" if hub_status == "cancelling" else _frontend_status(hub_status)
        failure_rows: list[dict[str, str]] = []
        failure_reasons: list[str] = []
        for item in failed:
            try:
                error = json.loads(item["error_json"] or "{}")
            except json.JSONDecodeError:
                error = {}
            reason = str(error.get("reason_code") or error.get("message") or item["status"])
            failure_rows.append({"keyword": item["keyword"], "reason": reason})
            if reason not in failure_reasons:
                failure_reasons.append(reason)
        processed = len(succeeded) + len(failed) + len(cancelled)
        resumable = (
            not active
            and bool(failed or cancelled)
            and hub_status in {"failed", "partial_failed", "cancelled"}
        )
        return {
            "batch_id": row["refresh_job_id"],
            "job_id": row["refresh_job_id"],
            "status": frontend,
            "hub_status": hub_status,
            "total": int(row["requested_count"]),
            "requested_count": int(row["requested_count"]),
            "success_count": len(succeeded),
            "succeeded_count": len(succeeded),
            "failed_count": len(failed),
            "cancelled_count": len(cancelled),
            "processed_count": processed,
            "pending_count": len(items) - processed,
            "current_keyword": next((item["keyword"] for item in items if item["status"] == "running"), None),
            "completed_keywords": [item["keyword"] for item in succeeded],
            "failed_keywords": failure_rows,
            "failure_reasons": failure_reasons,
            "cancelled_keywords": [item["keyword"] for item in cancelled],
            "cancel_requested": bool(row["cancel_requested"]),
            "cancel_reason": str(row["cancel_reason"] or "") if "cancel_reason" in row.keys() else "",
            "started_at": row["started_at"] or row["created_at"],
            "finished_at": row["finished_at"],
            "updated_at": row["updated_at"],
            "is_active": active,
            "is_finished": not active and hub_status in {"succeeded", "partial_failed", "failed", "blocked", "cancelled"},
            "source": row["trigger_source"],
            "provider": getattr(self.provider, "kind", "unknown"),
            "resumable": resumable,
        }

    def status(self, job_id: str) -> dict[str, Any] | None:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                "SELECT refresh_job_id FROM search_refresh_jobs WHERE refresh_job_id=? AND system_key=? AND platform=?",
                (job_id, SYSTEM, PLATFORM),
            ).fetchone()
            return self._job_payload(con, job_id) if row else None

    def active_status(self) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            active = self._active(con)
            return self._job_payload(con, active["refresh_job_id"]) if active else {
                "status": "idle",
                "hub_status": "idle",
                "is_active": False,
                "is_finished": False,
                "source": "hub_runtime",
            }

    def _update_counts(self, con, job_id: str) -> None:
        counts = con.execute(
            """SELECT
                SUM(status='succeeded') AS succeeded,
                SUM(status IN ('failed','blocked')) AS failed,
                SUM(status='cancelled') AS cancelled
               FROM search_refresh_items WHERE refresh_job_id=?""",
            (job_id,),
        ).fetchone()
        con.execute(
            """UPDATE search_refresh_jobs
               SET succeeded_count=?,failed_count=?,checkpoint_json=?,updated_at=?
               WHERE refresh_job_id=?""",
            (
                int(counts["succeeded"] or 0),
                int(counts["failed"] or 0),
                _json(
                    {
                        "succeeded_count": int(counts["succeeded"] or 0),
                        "failed_count": int(counts["failed"] or 0),
                        "cancelled_count": int(counts["cancelled"] or 0),
                    }
                ),
                _now(),
                job_id,
            ),
        )

    def _finish(self, con, job_id: str, command_id: str) -> dict[str, Any]:
        payload = self._job_payload(con, job_id)
        if payload["cancel_requested"]:
            final = "cancelled"
        elif payload["success_count"] == payload["total"]:
            final = "succeeded"
        elif payload["success_count"] > 0:
            final = "partial_failed"
        elif payload["failed_count"] == payload["total"]:
            final = "failed"
        else:
            final = "cancelled"
        now = _now()
        con.execute(
            """UPDATE search_refresh_jobs
               SET status=?,finished_at=?,updated_at=?
               WHERE refresh_job_id=?""",
            (final, now, now, job_id),
        )
        con.execute(
            """UPDATE command_runs
               SET status=?,output_json=?,updated_at=?
               WHERE command_id=?""",
            (
                "succeeded" if final == "succeeded" else "failed" if final in {"failed", "partial_failed"} else final,
                _json({**payload, "status": _frontend_status(final), "hub_status": final}),
                now,
                command_id,
            ),
        )
        return self._job_payload(con, job_id)

    def run_batch(self, job_id: str, *, confirm: bool = False) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                "SELECT command_id,status FROM search_refresh_jobs WHERE refresh_job_id=? AND system_key=? AND platform=?",
                (job_id, SYSTEM, PLATFORM),
            ).fetchone()
            if not row:
                return {"status": "missing", "batch_id": job_id}
            command_id = str(row["command_id"] or "")
            if row["status"] not in {"queued", "running"}:
                return self.status(job_id) or {"status": "missing", "batch_id": job_id}
            items = [
                dict(item)
                for item in con.execute(
                    "SELECT refresh_item_id,keyword_id FROM search_refresh_items WHERE refresh_job_id=? ORDER BY ordinal",
                    (job_id,),
                ).fetchall()
            ]
        service = XhsService(self.settings, provider=self.provider)
        if getattr(self.provider, "is_live", False) and (
            confirm is not True or not getattr(self.settings, "xhs_tikhub_token_configured", False)
        ):
            error = {
                "reason_code": "xhs.live_provider_confirmation_required",
                "message": "真实小红书批量刷新必须显式确认且配置 Provider token",
            }
            with writer_lock(self.settings.lock_path):
                with connect(self.settings) as con:
                    with transaction(con):
                        for item in items:
                            con.execute(
                                """UPDATE search_refresh_items
                                   SET status='failed',current_phase='failed',error_json=?,finished_at=?
                                   WHERE refresh_item_id=? AND status IN ('queued','running')""",
                                (_json(error), _now(), item["refresh_item_id"]),
                            )
                        self._update_counts(con, job_id)
                        return self._finish(con, job_id, command_id)

        for item in items:
            with writer_lock(self.settings.lock_path):
                with connect(self.settings) as con:
                    with transaction(con):
                        job = con.execute(
                            "SELECT status,cancel_requested FROM search_refresh_jobs WHERE refresh_job_id=?",
                            (job_id,),
                        ).fetchone()
                        if not job:
                            return {"status": "missing", "batch_id": job_id}
                        if job["cancel_requested"]:
                            con.execute(
                                """UPDATE search_refresh_items
                                   SET status='cancelled',current_phase='cancelled',finished_at=?
                                   WHERE refresh_item_id=? AND status='queued'""",
                                (_now(), item["refresh_item_id"]),
                            )
                            self._update_counts(con, job_id)
                            continue
                        keyword = con.execute(
                            "SELECT keyword FROM keywords WHERE keyword_id=? AND platform=?",
                            (item["keyword_id"], PLATFORM),
                        ).fetchone()
                        if not keyword:
                            error = {"reason_code": "keyword_missing", "message": "关键词已不存在"}
                            con.execute(
                                """UPDATE search_refresh_items
                                   SET status='failed',current_phase='failed',error_json=?,finished_at=?
                                   WHERE refresh_item_id=?""",
                                (_json(error), _now(), item["refresh_item_id"]),
                            )
                            self._update_counts(con, job_id)
                            continue
                        con.execute(
                            """UPDATE search_refresh_items
                               SET status='running',current_phase='provider',attempt_count=attempt_count+1,started_at=?
                               WHERE refresh_item_id=?""",
                            (_now(), item["refresh_item_id"]),
                        )
            try:
                response = self.provider.search(keyword_id=item["keyword_id"], keyword=str(keyword["keyword"]))
                service._persist_shadow_response(
                    command={
                        "command_id": command_id,
                        "refresh_job_id": job_id,
                        "item_id": item["refresh_item_id"],
                    },
                    keyword_id=item["keyword_id"],
                    keyword=str(keyword["keyword"]),
                    provider_kind=getattr(self.provider, "kind", "unknown"),
                    response=response,
                    dry_run=not bool(getattr(self.provider, "is_live", False)),
                )
            except Exception as exc:
                error = {
                    "reason_code": getattr(exc, "reason_code", "xhs.shadow_refresh_failed"),
                    "message": str(exc),
                }
                with writer_lock(self.settings.lock_path):
                    with connect(self.settings) as con:
                        with transaction(con):
                            con.execute(
                                """UPDATE search_refresh_items
                                   SET status='failed',current_phase='failed',error_json=?,finished_at=?
                                   WHERE refresh_item_id=?""",
                                (_json(error), _now(), item["refresh_item_id"]),
                            )
                            self._update_counts(con, job_id)
                            con.execute(
                                "INSERT INTO audit_log(audit_id,occurred_at,actor_type,action,subject_type,subject_id,outcome,details_json) VALUES(?,?,?,?,?,?,?,?)",
                                (
                                    generate_ulid_like("audit"),
                                    _now(),
                                    "system",
                                    "xhs.shadow_refresh",
                                    "refresh_job",
                                    job_id,
                                    "failed",
                                    _json({"keyword_id": item["keyword_id"], **error}),
                                ),
                            )
            else:
                with writer_lock(self.settings.lock_path):
                    with connect(self.settings) as con:
                        with transaction(con):
                            self._update_counts(con, job_id)

        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    return self._finish(con, job_id, command_id)

    def cancel(self, *, batch_id: str, key: str) -> dict[str, Any]:
        reject_xhs_write("refresh_batch_cancel")
        if not key.strip():
            raise ValidationAppError("小红书取消刷新必须提供幂等键")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = con.execute(
                        "SELECT * FROM search_refresh_jobs WHERE refresh_job_id=? AND system_key=? AND platform=?",
                        (batch_id, SYSTEM, PLATFORM),
                    ).fetchone()
                    if not row:
                        raise NotFoundError("小红书刷新批次", batch_id)
                    now = _now()
                    if row["status"] in {"succeeded", "failed", "partial_failed", "cancelled", "blocked"}:
                        return {"status": _frontend_status(row["status"]), "hub_status": row["status"], "message": "批次已结束", "batch": self._job_payload(con, batch_id)}
                    con.execute(
                        """UPDATE search_refresh_jobs
                           SET cancel_requested=1,cancel_requested_at=?,cancel_reason='user_requested',updated_at=?
                           WHERE refresh_job_id=?""",
                        (now, now, batch_id),
                    )
                    con.execute(
                        """UPDATE search_refresh_items
                           SET status='cancelled',current_phase='cancelled',finished_at=?
                           WHERE refresh_job_id=? AND status='queued'""",
                        (now, batch_id),
                    )
                    return {
                        "status": "running",
                        "hub_status": "cancelling",
                        "message": "停止信号已发送，当前关键词跑完后停止",
                        "batch": self._job_payload(con, batch_id),
                    }

    def resume(self, *, batch_id: str, key: str, confirm: bool = False) -> dict[str, Any]:
        reject_xhs_write("refresh_batch_resume")
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                "SELECT status FROM search_refresh_jobs WHERE refresh_job_id=? AND system_key=? AND platform=?",
                (batch_id, SYSTEM, PLATFORM),
            ).fetchone()
            if not row:
                raise NotFoundError("小红书刷新批次", batch_id)
            ids = [
                str(item["keyword_id"])
                for item in con.execute(
                    """SELECT keyword_id FROM search_refresh_items
                       WHERE refresh_job_id=? AND status IN ('failed','blocked','cancelled')
                       ORDER BY ordinal""",
                    (batch_id,),
                ).fetchall()
            ]
        if not ids:
            raise ValidationAppError("该批次没有可恢复的关键词")
        result = self.start_batch(keyword_ids=ids, key=key, source="resume")
        result["resumed_from"] = batch_id
        return result

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        with connect(self.settings, readonly=True) as con:
            rows = con.execute(
                """SELECT refresh_job_id FROM search_refresh_jobs
                   WHERE system_key=? AND platform=?
                     AND trigger_source IN ('web_refresh_all','resume')
                     AND requested_count > 1
                   ORDER BY COALESCE(finished_at,created_at) DESC LIMIT ?""",
                (SYSTEM, PLATFORM, max(1, min(limit, 200))),
            ).fetchall()
            return [self._job_payload(con, row["refresh_job_id"]) for row in rows]
