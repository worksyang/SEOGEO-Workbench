"""微信关键词刷新/调度写运行层。

这个模块刻意不依赖旧 8765 服务、Aidso、浏览器或网络。Provider 必须由调用方显式
注入；未注入时永远是 disabled。任务、事件、快照和审计都以 SQLite 为事实源。
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.domain.ids import generate_ulid_like
from content_hub.errors import ConflictError, NotFoundError, ValidationAppError


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class RefreshProvider(Protocol):
    kind: str

    def fetch(
        self,
        *,
        keyword_id: str,
        keyword: str,
        incremental: bool = False,
        refresh_round: Any = None,
    ) -> dict[str, Any]:
        """返回固定的 provider 结果；异常表示该词失败。"""


class DisabledRefreshProvider:
    kind = "disabled"

    def fetch(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("wechat refresh provider is disabled")


class FakeWechatRefreshProvider:
    """无网络的固定 Provider，供专项测试和显式 dry-run 注入使用。"""

    kind = "fake"

    def __init__(self, results: dict[str, dict[str, Any]] | None = None, *, fail_ids: set[str] | None = None):
        self.results = results or {}
        self.fail_ids = fail_ids or set()

    def fetch(self, *, keyword_id: str, keyword: str, incremental: bool = False, refresh_round: Any = None) -> dict[str, Any]:
        if keyword_id in self.fail_ids:
            raise RuntimeError(f"recorded provider failure: {keyword_id}")
        value = self.results.get(keyword_id)
        if value is not None:
            return json.loads(json.dumps(value, ensure_ascii=False))
        return {
            "captured_at": _now(),
            "result_count": 1,
            "features": {"suggestions": [], "related": [], "provider": "fake"},
            "hits": [{"rank": 1, "title_raw": f"{keyword} 固定样本", "url_raw": f"https://example.invalid/wechat/{keyword_id}"}],
            "metrics": [],
            "source_ref": "provider:fake",
            "incremental": incremental,
            "refresh_round": refresh_round,
        }


class InvalidKeywordIDsError(ValidationAppError):
    def __init__(self, invalid_keyword_ids: list[str]):
        self.invalid_keyword_ids = invalid_keyword_ids
        super().__init__("keyword_ids contains invalid items")


class BatchAlreadyRunningError(ConflictError):
    def __init__(self, state: dict[str, Any]):
        self.state = state
        super().__init__("batch already running")


class WechatRefreshService:
    MODULE = "wechat-search"
    PLATFORM = "wechat-search"

    def __init__(self, settings: Any, *, provider: RefreshProvider | None = None, actor_id: str = "user"):
        self.settings = settings
        self.provider = provider or DisabledRefreshProvider()
        self.actor_id = actor_id or "user"

    def _manifest(self, con, *, source_ref: str, source_hash: str, now: str) -> str:
        manifest_id = f"manifest_wechat_refresh_{source_hash[:24]}"
        con.execute(
            """INSERT OR IGNORE INTO source_manifests(
                manifest_id,system_key,source_kind,root_fingerprint,manifest_hash,entry_count,captured_at,immutable,payload_json
            ) VALUES(?,?,?,?,?,0,?,1,?)""",
            (manifest_id, self.MODULE, "refresh-provider", source_hash, source_hash, now, _json({"source_ref": source_ref})),
        )
        return manifest_id

    def _audit(self, con, *, action: str, subject_type: str, subject_id: str, outcome: str, details: dict[str, Any], request_id: str | None = None) -> str:
        audit_id = generate_ulid_like("cmd").replace("cmd_", "audit_", 1)
        con.execute(
            """INSERT INTO audit_log(
                audit_id,occurred_at,actor_type,actor_id,action,subject_type,subject_id,request_id,outcome,details_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (audit_id, _now(), "user", self.actor_id, action, subject_type, subject_id, request_id, outcome, _json(details)),
        )
        return audit_id

    def _event(self, con, *, job_id: str, item_id: str | None, event_type: str, status: str, message: str = "", details: dict[str, Any] | None = None) -> None:
        con.execute(
            """INSERT INTO search_refresh_events(
                event_id,refresh_job_id,refresh_item_id,event_type,status,message,details_json,occurred_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (generate_ulid_like("evt"), job_id, item_id, event_type, status, message, _json(details or {}), _now()),
        )

    def _receipt(self, con, *, command_id: str, key: str, legacy_status: str, hub_status: str, reconcile_status: str, details: dict[str, Any]) -> dict[str, str]:
        receipt_id = generate_ulid_like("dwr")
        con.execute(
            """INSERT INTO dual_write_receipts(
                receipt_id,module_key,command_id,idempotency_key,legacy_status,hub_status,
                reconcile_status,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(module_key,idempotency_key) DO UPDATE SET
                command_id=excluded.command_id,legacy_status=excluded.legacy_status,
                hub_status=excluded.hub_status,reconcile_status=excluded.reconcile_status,
                details_json=excluded.details_json""",
            (receipt_id, self.MODULE, command_id, key, legacy_status, hub_status, reconcile_status, _json(details), _now()),
        )
        row = con.execute(
            "SELECT receipt_id,command_id,idempotency_key FROM dual_write_receipts WHERE module_key=? AND idempotency_key=?",
            (self.MODULE, key),
        ).fetchone()
        return {"receipt_id": row["receipt_id"], "command_id": row["command_id"], "idempotency_key": row["idempotency_key"]}

    def _existing(self, con, key: str, input_hash: str) -> dict[str, Any] | None:
        row = con.execute(
            """SELECT c.*,r.refresh_job_id,r.status AS job_status,r.checkpoint_json
               FROM command_runs c LEFT JOIN search_refresh_jobs r ON r.command_id=c.command_id
               WHERE c.module_key=? AND c.idempotency_key=?""",
            (self.MODULE, key),
        ).fetchone()
        if not row:
            return None
        old_input = json.loads(row["input_json"] or "{}")
        if _hash(old_input) != input_hash:
            raise ConflictError("idempotency key 已用于不同输入。")
        output = json.loads(row["output_json"] or "{}")
        return output if isinstance(output, dict) else {"command_id": row["command_id"], "status": row["status"], "refresh_job_id": row["refresh_job_id"]}

    def _keyword(self, con, keyword_id: str) -> dict[str, Any]:
        row = con.execute(
            "SELECT keyword_id,platform,keyword,status FROM keywords WHERE keyword_id=?",
            (keyword_id,),
        ).fetchone()
        if not row or row["platform"] != self.PLATFORM:
            raise NotFoundError("微信关键词", keyword_id)
        if row["status"] == "archived":
            raise ValidationAppError("归档关键词不能刷新。")
        return dict(row)

    def _create_command(self, con, *, key: str, command_type: str, input_payload: dict[str, Any], confirmation: dict[str, Any] | None = None) -> tuple[str, str]:
        command_id = generate_ulid_like("cmd")
        job_id = generate_ulid_like("srj")
        now = _now()
        con.execute(
            """INSERT INTO command_runs(
                command_id,module_key,command_type,idempotency_key,actor_id,status,
                confirmation_json,input_json,output_json,error_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,'running',?,?,?,?,?,?)""",
            (command_id, self.MODULE, command_type, key, self.actor_id[:120], _json(confirmation or {}),
             _json(input_payload), "{}", "{}", now, now),
        )
        return command_id, job_id

    def _active_batch(self, con) -> dict[str, Any] | None:
        row = con.execute(
            """SELECT * FROM search_refresh_jobs
               WHERE system_key=? AND platform=? AND trigger_type IN ('manual','scheduled')
                 AND status IN ('queued','running') AND cancel_requested=0
               ORDER BY created_at DESC LIMIT 1""",
            (self.MODULE, self.PLATFORM),
        ).fetchone()
        return dict(row) if row else None

    def _active_single(self, con) -> dict[str, Any] | None:
        row = con.execute(
            """SELECT * FROM search_refresh_jobs
               WHERE system_key=? AND platform=? AND trigger_type='manual'
                 AND requested_count=1 AND status IN ('queued','running')
                 AND cancel_requested=0
               ORDER BY created_at DESC LIMIT 1""",
            (self.MODULE, self.PLATFORM),
        ).fetchone()
        return dict(row) if row else None

    def _set_active_job(self, con, job_id: str) -> None:
        con.execute(
            """INSERT OR IGNORE INTO search_scheduler_state(
                system_key,platform,enabled,next_run_at,last_run_at,active_refresh_job_id,updated_at,payload_json
            ) VALUES(?,?,0,NULL,NULL,?,?,?)""",
            (self.MODULE, self.PLATFORM, job_id, _now(), "{}"),
        )
        con.execute(
            "UPDATE search_scheduler_state SET active_refresh_job_id=?,updated_at=? WHERE system_key=? AND platform=?",
            (job_id, _now(), self.MODULE, self.PLATFORM),
        )

    def _write_snapshot(self, con, *, keyword: dict[str, Any], job_id: str, item_id: str, result: dict[str, Any], trigger_type: str) -> tuple[str, dict[str, Any]]:
        now = str(result.get("captured_at") or _now())
        source_ref = str(result.get("source_ref") or f"provider:{getattr(self.provider, 'kind', 'unknown')}")
        source_hash = _hash({"keyword_id": keyword["keyword_id"], "job_id": job_id, "result": result})
        snapshot_id = f"snp_{source_hash[:24]}"
        manifest_id = self._manifest(con, source_ref=source_ref, source_hash=source_hash, now=now)
        hits = result.get("hits") if isinstance(result.get("hits"), list) else []
        con.execute(
            """INSERT INTO search_snapshots(
                snapshot_id,platform,keyword,keyword_id,captured_at,trigger_type,result_count,features_json,source_ref,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(snapshot_id) DO NOTHING""",
            (snapshot_id, self.PLATFORM, keyword["keyword"], keyword["keyword_id"], now, trigger_type,
             result.get("result_count", len(hits)), _json(result.get("features") or {}), source_ref, _json(result)),
        )
        for ordinal, hit in enumerate(hits, 1):
            rank = int(hit.get("rank") or ordinal)
            hit_id = f"hit_{hashlib.sha256(f'{snapshot_id}:{rank}'.encode()).hexdigest()[:24]}"
            content_id = hit.get("content_id")
            if content_id and not con.execute("SELECT 1 FROM contents WHERE content_id=?", (content_id,)).fetchone():
                content_id = None
            con.execute(
                """INSERT INTO search_hits(
                    hit_id,snapshot_id,rank,content_id,title_raw,url_raw,creator_name_raw,payload_json
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(hit_id) DO NOTHING""",
                (hit_id, snapshot_id, rank, content_id, hit.get("title_raw") or hit.get("title"), hit.get("url_raw") or hit.get("url"), hit.get("creator_name_raw") or hit.get("account"), _json(hit)),
            )
        metrics = result.get("metrics") if isinstance(result.get("metrics"), list) else []
        for metric in metrics:
            metric_key = str(metric.get("metric_key") or "").strip()
            if not metric_key:
                continue
            con.execute(
                """INSERT OR IGNORE INTO metric_definitions(
                    metric_key,platform,subject_type,display_name,value_type,unit,accumulation_mode,description,active
                ) VALUES(?,?,?,?,?,?,?,?,1)""",
                (metric_key, self.PLATFORM, str(metric.get("subject_type") or "keyword"), str(metric.get("display_name") or metric_key), "number", metric.get("unit"), str(metric.get("accumulation_mode") or "gauge"), str(metric.get("description") or "")),
            )
            value = metric.get("numeric_value", metric.get("value"))
            if value is None:
                continue
            subject_id = str(metric.get("subject_id") or keyword["keyword_id"])
            observation_id = f"obs_{hashlib.sha256(f'{snapshot_id}:{metric_key}:{subject_id}'.encode()).hexdigest()[:24]}"
            con.execute(
                """INSERT OR IGNORE INTO metric_observations(
                    observation_id,subject_type,subject_id,metric_key,observed_at,numeric_value,snapshot_id,source_ref,confidence,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (observation_id, str(metric.get("subject_type") or "keyword"), str(metric.get("subject_id") or keyword["keyword_id"]), metric_key, now, float(value), snapshot_id, source_ref, metric.get("confidence"), _json(metric)),
            )
        return snapshot_id, {"source_ref": source_ref, "source_manifest_id": manifest_id, "captured_at": now, "hit_count": len(hits), "metric_count": len(metrics)}

    def _runtime_projection(self, con, *, subject_id: str, payload: dict[str, Any], source_hash: str, manifest_id: str, source_ref: str) -> None:
        con.execute(
            """INSERT OR REPLACE INTO wechat_legacy_projections(
                projection_id,projection_kind,subject_id,payload_json,source_hash,source_manifest_id,source_ref,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (f"proj_{hashlib.sha256(f'runtime:{subject_id}:{source_hash}'.encode()).hexdigest()[:24]}", "runtime", subject_id,
             _json(payload), source_hash, manifest_id, source_ref, _now()),
        )

    def _finish_item(self, con, *, job_id: str, item_id: str, keyword: dict[str, Any], status: str, result: dict[str, Any] | None, error: dict[str, Any] | None, trigger_type: str) -> dict[str, Any]:
        row = con.execute("SELECT ordinal FROM search_refresh_items WHERE refresh_item_id=?", (item_id,)).fetchone()
        ordinal = int(row["ordinal"]) if row else 0
        snapshot_id = None
        evidence: dict[str, Any] = {}
        if status == "succeeded":
            snapshot_id, evidence = self._write_snapshot(con, keyword=keyword, job_id=job_id, item_id=item_id, result=result or {}, trigger_type=trigger_type)
        now = _now()
        con.execute(
            """UPDATE search_refresh_items
               SET status=?,current_phase=?,snapshot_id=?,source_manifest_id=?,error_json=?,
                   finished_at=?
               WHERE refresh_item_id=?""",
            (status, "completed" if status == "succeeded" else status, snapshot_id, evidence.get("source_manifest_id"), _json(error or {}), now, item_id),
        )
        counts = con.execute(
            """SELECT
                SUM(status='succeeded') AS succeeded,
                SUM(status IN ('failed','blocked')) AS failed,
                SUM(status='cancelled') AS cancelled
               FROM search_refresh_items WHERE refresh_job_id=?""",
            (job_id,),
        ).fetchone()
        con.execute(
            "UPDATE search_refresh_jobs SET succeeded_count=?,failed_count=?,checkpoint_json=?,updated_at=? WHERE refresh_job_id=?",
            (
                int(counts["succeeded"] or 0),
                int(counts["failed"] or 0),
                _json({
                    "last_item_id": item_id,
                    "last_keyword_id": keyword["keyword_id"],
                    "last_status": status,
                    "succeeded_count": int(counts["succeeded"] or 0),
                    "failed_count": int(counts["failed"] or 0),
                    "cancelled_count": int(counts["cancelled"] or 0),
                }),
                now,
                job_id,
            ),
        )
        self._event(con, job_id=job_id, item_id=item_id, event_type=status, status=status, details={**evidence, **(error or {})})
        return {"keyword_id": keyword["keyword_id"], "keyword": keyword["keyword"], "status": status, "snapshot_id": snapshot_id, **evidence, **(error or {})}

    def _job_payload(self, con, job_id: str) -> dict[str, Any]:
        row = con.execute("SELECT * FROM search_refresh_jobs WHERE refresh_job_id=?", (job_id,)).fetchone()
        if not row:
            raise NotFoundError("微信刷新批次", job_id)
        items = [dict(x) for x in con.execute("SELECT i.*,k.keyword FROM search_refresh_items i JOIN keywords k ON k.keyword_id=i.keyword_id WHERE i.refresh_job_id=? ORDER BY i.ordinal", (job_id,))]
        success = [x for x in items if x["status"] == "succeeded"]
        failed = [x for x in items if x["status"] in {"failed", "blocked"}]
        blocked = [x for x in items if x["status"] == "blocked"]
        cancelled = [x for x in items if x["status"] == "cancelled"]
        active = row["status"] in {"queued", "running"} and not row["cancel_requested"]
        status = "cancelling" if row["cancel_requested"] and active else str(row["status"])
        if row["cancel_requested"] and not active and not failed and not row["status"] == "succeeded":
            status = "cancelled"
        return {
            "batch_id": row["refresh_job_id"], "job_id": row["refresh_job_id"], "status": status,
            "total": row["requested_count"], "requested_count": row["requested_count"],
            "success_count": len(success), "succeeded_count": len(success), "failed_count": len(failed),
            "blocked_count": len(blocked),
            "cancelled_count": len(cancelled), "processed_count": len(success) + len(failed) + len(cancelled),
            "pending_count": len(items) - len(success) - len(failed) - len(cancelled),
            "current_keyword": next((x["keyword"] for x in items if x["status"] == "running"), None),
            "completed_keywords": [x["keyword"] for x in success],
            "failed_keywords": [x["keyword"] for x in failed],
            "cancelled_keywords": [x["keyword"] for x in cancelled],
            "started_at": row["started_at"], "finished_at": row["finished_at"], "updated_at": row["updated_at"],
            "cancel_requested": bool(row["cancel_requested"]), "is_active": active,
            "is_finished": not active and status not in {"queued", "running", "cancelling"},
            "source": row["trigger_source"],
        }

    def _complete_job(self, con, *, job_id: str, command_id: str, key: str, status: str, output: dict[str, Any], error: dict[str, Any] | None = None) -> dict[str, Any]:
        now = _now()
        con.execute(
            """UPDATE search_refresh_jobs SET status=?,succeeded_count=?,failed_count=?,finished_at=?,updated_at=? WHERE refresh_job_id=?""",
            (status, int(output.get("success_count") or 0), int(output.get("failed_count") or 0), now, now, job_id),
        )
        con.execute(
            "UPDATE command_runs SET status=?,output_json=?,error_json=?,updated_at=? WHERE command_id=?",
            ("succeeded" if status == "succeeded" else "failed" if status in {"failed", "partial_failed"} else status, _json(output), _json(error or {}), now, command_id),
        )
        con.execute("UPDATE search_scheduler_state SET active_refresh_job_id=NULL, last_run_at=?, updated_at=? WHERE system_key=? AND platform=?", (now, now, self.MODULE, self.PLATFORM))
        return output

    def refresh_one(self, *, keyword_id: str, key: str, request_keyword: str = "", request_id: str | None = None, confirm: bool = True) -> dict[str, Any]:
        if not key.strip():
            raise ValidationAppError("必须提供 Idempotency-Key。")
        payload = {"keyword_id": keyword_id, "platform": self.PLATFORM, "keyword": request_keyword.strip(), "confirm": bool(confirm)}
        input_hash = _hash(payload)
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    old = self._existing(con, key, input_hash)
                    if old:
                        return old
                    if self._active_single(con):
                        raise ConflictError("single refresh already running")
                    keyword = self._keyword(con, keyword_id)
                    command_id, job_id = self._create_command(con, key=key, command_type="wechat.keyword.refresh", input_payload=payload, confirmation={"confirmed": bool(confirm)})
                    now = _now()
                    con.execute("INSERT INTO search_refresh_jobs(refresh_job_id,system_key,platform,command_id,trigger_type,status,requested_count,started_at,created_at,updated_at,trigger_source) VALUES(?,?,?,?,?,'running',1,?,?,?,?)", (job_id, self.MODULE, self.PLATFORM, command_id, "manual", now, now, now, "web_refresh"))
                    self._set_active_job(con, job_id)
                    item_id = generate_ulid_like("sri")
                    con.execute("INSERT INTO search_refresh_items(refresh_item_id,refresh_job_id,keyword_id,ordinal,status,attempt_count,current_phase,started_at) VALUES(?,?,?,0,'running',1,'provider',?)", (item_id, job_id, keyword_id, now))
                    self._event(con, job_id=job_id, item_id=item_id, event_type="started", status="running")
                    try:
                        result = self.provider.fetch(keyword_id=keyword_id, keyword=keyword["keyword"])
                        item = self._finish_item(con, job_id=job_id, item_id=item_id, keyword=keyword, status="succeeded", result=result, error=None, trigger_type="manual")
                        output = {"job_id": job_id, "refresh_job_id": job_id, "command_id": command_id, "status": "succeeded", "keyword_id": keyword_id, "keyword": keyword["keyword"], "source": "hub", "provider": getattr(self.provider, "kind", "unknown"), "result": item}
                        self._complete_job(con, job_id=job_id, command_id=command_id, key=key, status="succeeded", output=output)
                        output.update(self._job_payload(con, job_id))
                        con.execute("UPDATE command_runs SET output_json=?,updated_at=? WHERE command_id=?", (_json(output), _now(), command_id))
                        manifest_id = item.get("source_manifest_id")
                        if manifest_id:
                            self._runtime_projection(con, subject_id=job_id, payload={"runtime_subtype": "single_job", **output}, source_hash=_hash(output), manifest_id=manifest_id, source_ref=str(item.get("source_ref") or "provider"))
                        receipt = self._receipt(con, command_id=command_id, key=key, legacy_status="not_attempted", hub_status="succeeded", reconcile_status="pending", details={"job_id": job_id, "provider": getattr(self.provider, "kind", "unknown")})
                        self._audit(con, action="wechat.refresh", subject_type="refresh_job", subject_id=job_id, outcome="succeeded", details={"keyword_id": keyword_id, **receipt}, request_id=request_id)
                        output["dual_write_receipt"] = receipt
                        con.execute("UPDATE command_runs SET output_json=?,updated_at=? WHERE command_id=?", (_json(output), _now(), command_id))
                        return output
                    except Exception as exc:
                        error = {"error": str(exc), "reason_code": "provider_disabled" if getattr(self.provider, "kind", "") == "disabled" else "provider_failed"}
                        self._finish_item(con, job_id=job_id, item_id=item_id, keyword=keyword, status="blocked" if getattr(self.provider, "kind", "") == "disabled" else "failed", result=None, error=error, trigger_type="manual")
                        output = {"job_id": job_id, "refresh_job_id": job_id, "command_id": command_id, "status": "blocked" if getattr(self.provider, "kind", "") == "disabled" else "failed", "keyword_id": keyword_id, "keyword": keyword["keyword"], "blocked": getattr(self.provider, "kind", "") == "disabled", "upstream_called": False, "error": error}
                        self._complete_job(con, job_id=job_id, command_id=command_id, key=key, status="blocked" if output["blocked"] else "failed", output=output, error=error)
                        output.update(self._job_payload(con, job_id))
                        output["status"] = "blocked" if output["blocked"] else "failed"
                        con.execute("UPDATE command_runs SET output_json=?,updated_at=? WHERE command_id=?", (_json(output), _now(), command_id))
                        failure_hash = _hash(output)
                        failure_manifest = self._manifest(
                            con,
                            source_ref=f"provider:{getattr(self.provider, 'kind', 'unknown')}",
                            source_hash=failure_hash,
                            now=_now(),
                        )
                        self._runtime_projection(
                            con,
                            subject_id=job_id,
                            payload={"runtime_subtype": "single_job", **output},
                            source_hash=failure_hash,
                            manifest_id=failure_manifest,
                            source_ref=f"provider:{getattr(self.provider, 'kind', 'unknown')}",
                        )
                        receipt = self._receipt(con, command_id=command_id, key=key, legacy_status="not_attempted", hub_status=output["status"], reconcile_status="blocked", details=error)
                        self._audit(con, action="wechat.refresh", subject_type="refresh_job", subject_id=job_id, outcome="blocked" if output["blocked"] else "failed", details={**error, **receipt}, request_id=request_id)
                        output["dual_write_receipt"] = receipt
                        con.execute("UPDATE command_runs SET output_json=?,updated_at=? WHERE command_id=?", (_json(output), _now(), command_id))
                        return output

    def refresh_batch(self, *, keyword_ids: list[str] | None, key: str, source: str = "web_refresh_all", incremental: bool = False, refresh_round: Any = None, request_id: str | None = None) -> dict[str, Any]:
        if not key.strip():
            raise ValidationAppError("必须提供 Idempotency-Key。")
        unique_ids = list(dict.fromkeys(str(item).strip() for item in (keyword_ids or []) if str(item).strip()))
        if incremental and not unique_ids:
            raise ValidationAppError("incremental refresh requires keyword_ids")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    candidate_rows = con.execute(
                        """SELECT k.keyword_id,k.platform,k.keyword,k.status
                           FROM keywords k
                           LEFT JOIN search_keyword_settings s
                             ON s.keyword_id=k.keyword_id
                            AND s.system_key=? AND s.platform=?
                           WHERE k.platform=? AND k.status='active'
                             AND COALESCE(s.batch_default_selected,1)=1
                           ORDER BY k.keyword_id""",
                        (self.MODULE, self.PLATFORM, self.PLATFORM),
                    ).fetchall()
                    candidate_map = {str(row["keyword_id"]): dict(row) for row in candidate_rows}
                    if not unique_ids:
                        unique_ids = list(candidate_map)
                    if not unique_ids:
                        raise ValidationAppError("no keywords found")
                    payload = {"keyword_ids": unique_ids, "incremental": bool(incremental), "refresh_round": refresh_round, "source": source}
                    input_hash = _hash(payload)
                    old = self._existing(con, key, input_hash)
                    if old:
                        return old
                    invalid_ids = [keyword_id for keyword_id in unique_ids if keyword_id not in candidate_map]
                    if invalid_ids:
                        raise InvalidKeywordIDsError(invalid_ids)
                    keywords = [candidate_map[keyword_id] for keyword_id in unique_ids]
                    active = self._active_batch(con)
                    if active:
                        raise BatchAlreadyRunningError(self._job_payload(con, active["refresh_job_id"]))
                    command_id, job_id = self._create_command(con, key=key, command_type="wechat.refresh_all", input_payload=payload)
                    now = _now()
                    con.execute("INSERT INTO search_refresh_jobs(refresh_job_id,system_key,platform,command_id,trigger_type,status,requested_count,started_at,created_at,updated_at,trigger_source) VALUES(?,?,?,?,?,'running',?,?,?, ?,?)", (job_id, self.MODULE, self.PLATFORM, command_id, "scheduled" if source == "scheduler" else "manual", len(unique_ids), now, now, now, source))
                    self._set_active_job(con, job_id)
                    for ordinal, keyword in enumerate(keywords):
                        item_id = generate_ulid_like("sri")
                        con.execute("INSERT INTO search_refresh_items(refresh_item_id,refresh_job_id,keyword_id,ordinal,status,attempt_count,current_phase) VALUES(?,?,?,?,'queued',0,'queued')", (item_id, job_id, keyword["keyword_id"], ordinal))
                    self._event(con, job_id=job_id, item_id=None, event_type="started", status="running", details={"count": len(keywords), "incremental": incremental})
                    for item in con.execute("SELECT refresh_item_id,keyword_id FROM search_refresh_items WHERE refresh_job_id=? ORDER BY ordinal", (job_id,)).fetchall():
                        keyword = next(x for x in keywords if x["keyword_id"] == item["keyword_id"])
                        con.execute("UPDATE search_refresh_items SET status='running',current_phase='provider',attempt_count=attempt_count+1,started_at=? WHERE refresh_item_id=?", (_now(), item["refresh_item_id"]))
                        if con.execute("SELECT cancel_requested FROM search_refresh_jobs WHERE refresh_job_id=?", (job_id,)).fetchone()["cancel_requested"]:
                            con.execute("UPDATE search_refresh_items SET status='cancelled',current_phase='cancelled',finished_at=? WHERE refresh_item_id=?", (_now(), item["refresh_item_id"]))
                            continue
                        try:
                            result = self.provider.fetch(keyword_id=keyword["keyword_id"], keyword=keyword["keyword"], incremental=incremental, refresh_round=refresh_round)
                            self._finish_item(con, job_id=job_id, item_id=item["refresh_item_id"], keyword=keyword, status="succeeded", result=result, error=None, trigger_type="scheduled" if source == "scheduler" else "manual")
                        except Exception as exc:
                            self._finish_item(con, job_id=job_id, item_id=item["refresh_item_id"], keyword=keyword, status="blocked" if getattr(self.provider, "kind", "") == "disabled" else "failed", result=None, error={"error": str(exc), "reason_code": "provider_disabled" if getattr(self.provider, "kind", "") == "disabled" else "provider_failed"}, trigger_type="scheduled" if source == "scheduler" else "manual")
                    output = self._job_payload(con, job_id)
                    if output["blocked_count"] == output["total"] and output["success_count"] == 0:
                        final = "blocked"
                    elif output["success_count"] == output["total"]:
                        final = "succeeded"
                    elif output["success_count"] or output["failed_count"]:
                        final = "partial_failed"
                    else:
                        final = "failed"
                    self._complete_job(con, job_id=job_id, command_id=command_id, key=key, status=final, output=output)
                    output = self._job_payload(con, job_id)
                    output["status"] = final
                    con.execute("UPDATE command_runs SET output_json=?,updated_at=? WHERE command_id=?", (_json(output), _now(), command_id))
                    manifest = con.execute(
                        """SELECT i.source_manifest_id,m.payload_json
                           FROM search_refresh_items i
                           JOIN source_manifests m ON m.manifest_id=i.source_manifest_id
                           WHERE i.refresh_job_id=? AND i.source_manifest_id IS NOT NULL LIMIT 1""",
                        (job_id,),
                    ).fetchone()
                    if manifest:
                            self._runtime_projection(
                                con,
                                subject_id=job_id,
                                payload={"runtime_subtype": "batch", **output},
                                source_hash=_hash(output),
                                manifest_id=manifest["source_manifest_id"],
                                source_ref=(json.loads(manifest["payload_json"] or "{}").get("source_ref") or "provider"),
                            )
                    else:
                        failure_hash = _hash(output)
                        failure_manifest = self._manifest(
                            con,
                            source_ref=f"provider:{getattr(self.provider, 'kind', 'unknown')}",
                            source_hash=failure_hash,
                            now=_now(),
                        )
                        self._runtime_projection(
                            con,
                            subject_id=job_id,
                            payload={"runtime_subtype": "batch", **output},
                            source_hash=failure_hash,
                            manifest_id=failure_manifest,
                            source_ref=f"provider:{getattr(self.provider, 'kind', 'unknown')}",
                        )
                    receipt = self._receipt(con, command_id=command_id, key=key, legacy_status="not_attempted", hub_status=final, reconcile_status="pending" if final == "succeeded" else "blocked", details={"batch_id": job_id, "source": source})
                    self._audit(
                        con,
                        action="wechat.refresh_all",
                        subject_type="refresh_job",
                        subject_id=job_id,
                        outcome="succeeded" if final == "succeeded" else "blocked" if final == "blocked" else "failed",
                        details={**receipt, "source": source, "status": final},
                        request_id=request_id,
                    )
                    output["dual_write_receipt"] = receipt
                    return output

    def cancel_batch(self, *, batch_id: str, key: str, request_id: str | None = None) -> dict[str, Any]:
        if not key.strip():
            raise ValidationAppError("必须提供 Idempotency-Key。")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = con.execute("SELECT * FROM search_refresh_jobs WHERE refresh_job_id=? AND system_key=? AND platform=?", (batch_id, self.MODULE, self.PLATFORM)).fetchone()
                    if not row:
                        raise NotFoundError("微信刷新批次", batch_id)
                    payload = {"batch_id": batch_id}
                    old = self._existing(con, key, _hash(payload))
                    if old:
                        return old
                    command_id, cancel_job = self._create_command(con, key=key, command_type="wechat.refresh_all.cancel", input_payload=payload)
                    if row["status"] in {"succeeded", "failed", "partial_failed", "cancelled", "blocked"}:
                        output = {"status": row["status"], "message": "批次已结束", "batch": self._job_payload(con, batch_id)}
                        con.execute("UPDATE command_runs SET status='succeeded',output_json=?,updated_at=? WHERE command_id=?", (_json(output), _now(), command_id))
                        batch_hash = _hash(output["batch"])
                        manifest = con.execute(
                            """SELECT i.source_manifest_id,m.payload_json
                               FROM search_refresh_items i
                               JOIN source_manifests m ON m.manifest_id=i.source_manifest_id
                               WHERE i.refresh_job_id=? AND i.source_manifest_id IS NOT NULL LIMIT 1""",
                            (batch_id,),
                        ).fetchone()
                        if not manifest:
                            manifest_id = self._manifest(con, source_ref="provider:cancel", source_hash=batch_hash, now=_now())
                            source_ref = "provider:cancel"
                        else:
                            manifest_id = manifest["source_manifest_id"]
                            source_ref = json.loads(manifest["payload_json"] or "{}").get("source_ref") or "provider"
                        self._runtime_projection(
                            con,
                            subject_id=batch_id,
                            payload={"runtime_subtype": "batch", **output["batch"]},
                            source_hash=batch_hash,
                            manifest_id=manifest_id,
                            source_ref=source_ref,
                        )
                        receipt = self._receipt(con, command_id=command_id, key=key, legacy_status="not_attempted", hub_status="succeeded", reconcile_status="pending", details={"batch_id": batch_id, "finished": True})
                        self._audit(con, action="wechat.refresh_all.cancel", subject_type="refresh_job", subject_id=batch_id, outcome="succeeded", details=receipt, request_id=request_id)
                        output["dual_write_receipt"] = receipt
                        return output
                    now = _now()
                    con.execute("UPDATE search_refresh_jobs SET cancel_requested=1,cancel_requested_at=?,updated_at=? WHERE refresh_job_id=?", (now, now, batch_id))
                    con.execute("UPDATE search_refresh_items SET status='cancelled',current_phase='cancelled',finished_at=? WHERE refresh_job_id=? AND status='queued'", (now, batch_id))
                    output = {"status": "cancelling", "message": "取消信号已发送，当前关键词跑完后停止", "batch": self._job_payload(con, batch_id)}
                    con.execute("UPDATE command_runs SET status='succeeded',output_json=?,updated_at=? WHERE command_id=?", (_json(output), now, command_id))
                    batch_hash = _hash(output["batch"])
                    manifest = con.execute(
                        """SELECT i.source_manifest_id,m.payload_json
                           FROM search_refresh_items i
                           JOIN source_manifests m ON m.manifest_id=i.source_manifest_id
                           WHERE i.refresh_job_id=? AND i.source_manifest_id IS NOT NULL LIMIT 1""",
                        (batch_id,),
                    ).fetchone()
                    if not manifest:
                        manifest_id = self._manifest(con, source_ref="provider:cancel", source_hash=batch_hash, now=now)
                        source_ref = "provider:cancel"
                    else:
                        manifest_id = manifest["source_manifest_id"]
                        source_ref = json.loads(manifest["payload_json"] or "{}").get("source_ref") or "provider"
                    self._runtime_projection(
                        con,
                        subject_id=batch_id,
                        payload={"runtime_subtype": "batch", **output["batch"]},
                        source_hash=batch_hash,
                        manifest_id=manifest_id,
                        source_ref=source_ref,
                    )
                    receipt = self._receipt(con, command_id=command_id, key=key, legacy_status="not_attempted", hub_status="succeeded", reconcile_status="pending", details={"batch_id": batch_id})
                    self._audit(con, action="wechat.refresh_all.cancel", subject_type="refresh_job", subject_id=batch_id, outcome="succeeded", details=receipt, request_id=request_id)
                    output["dual_write_receipt"] = receipt
                    return output

    def scheduler_config(self, *, payload: dict[str, Any], key: str, request_id: str | None = None) -> dict[str, Any]:
        if not key.strip():
            raise ValidationAppError("必须提供 Idempotency-Key。")
        allowed = {"enabled", "interval_hours", "daily_keyword_budget", "max_keywords_per_batch"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationAppError(f"不支持的 scheduler 字段：{sorted(unknown)[0]}")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = con.execute("SELECT payload_json FROM search_scheduler_state WHERE system_key=? AND platform=?", (self.MODULE, self.PLATFORM)).fetchone()
                    current = json.loads(row["payload_json"] or "{}") if row else {}
                    merged = {**current, **payload}
                    if "enabled" in payload:
                        merged["enabled"] = bool(payload["enabled"])
                    elif row is not None:
                        merged["enabled"] = bool(con.execute(
                            "SELECT enabled FROM search_scheduler_state WHERE system_key=? AND platform=?",
                            (self.MODULE, self.PLATFORM),
                        ).fetchone()["enabled"])
                    else:
                        merged["enabled"] = False
                    try:
                        interval = float(merged.get("interval_hours", 3.0))
                        budget = int(merged.get("daily_keyword_budget", 1550))
                        maximum = int(merged.get("max_keywords_per_batch", 250))
                    except (TypeError, ValueError) as exc:
                        raise ValidationAppError("scheduler 数值字段格式无效。") from exc
                    if not 0.1 <= interval <= 168: raise ValidationAppError("interval_hours 必须在 0.1–168 之间。")
                    if not 1 <= budget <= 100000: raise ValidationAppError("daily_keyword_budget 超出范围。")
                    if not 1 <= maximum <= 10000: raise ValidationAppError("max_keywords_per_batch 超出范围。")
                    merged.update({"enabled": bool(merged.get("enabled", False)), "interval_hours": interval, "daily_keyword_budget": budget, "max_keywords_per_batch": maximum})
                    old = self._existing(con, key, _hash(merged))
                    if old: return old
                    command_id, _ = self._create_command(con, key=key, command_type="wechat.scheduler.config", input_payload=merged)
                    now = _now()
                    next_run = (
                        (datetime.now(UTC) + timedelta(hours=interval)).isoformat(timespec="seconds").replace("+00:00", "Z")
                        if merged["enabled"] else None
                    )
                    merged.update({
                        "next_run_at": next_run,
                        "last_error": current.get("last_error"),
                        "provider_kind": getattr(self.provider, "kind", "disabled"),
                        "base_url": current.get("base_url", str(getattr(self.settings, "wechat_source_url", ""))),
                        "last_triggered_at": current.get("last_triggered_at"),
                        "last_result": current.get("last_result"),
                        "last_plan": current.get("last_plan"),
                        "last_discovery": current.get("last_discovery"),
                        "budget": current.get("budget", {}),
                        "budget_breakdown": current.get("budget_breakdown", {}),
                    })
                    con.execute("""INSERT INTO search_scheduler_state(system_key,platform,enabled,next_run_at,updated_at,payload_json)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(system_key,platform) DO UPDATE SET enabled=excluded.enabled,next_run_at=excluded.next_run_at,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                        (self.MODULE, self.PLATFORM, 1 if merged["enabled"] else 0, next_run, now, _json(merged)))
                    output = {"status": "succeeded", **self.scheduler_status_from_connection(con), "command_id": command_id}
                    con.execute("UPDATE command_runs SET status='succeeded',output_json=?,updated_at=? WHERE command_id=?", (_json(output), now, command_id))
                    scheduler_hash = _hash(output)
                    scheduler_manifest = self._manifest(con, source_ref="provider:scheduler", source_hash=scheduler_hash, now=now)
                    self._runtime_projection(
                        con,
                        subject_id="scheduler",
                        payload={"runtime_subtype": "scheduler", **self.scheduler_status_from_connection(con)},
                        source_hash=scheduler_hash,
                        manifest_id=scheduler_manifest,
                        source_ref="provider:scheduler",
                    )
                    receipt = self._receipt(con, command_id=command_id, key=key, legacy_status="not_attempted", hub_status="succeeded", reconcile_status="pending", details={"config": merged})
                    self._audit(con, action="wechat.scheduler.config", subject_type="scheduler", subject_id="scheduler", outcome="succeeded", details=receipt, request_id=request_id)
                    output["dual_write_receipt"] = receipt
                    return output

    def scheduler_status_from_connection(self, con) -> dict[str, Any]:
        row = con.execute(
            "SELECT * FROM search_scheduler_state WHERE system_key=? AND platform=?",
            (self.MODULE, self.PLATFORM),
        ).fetchone()
        if not row:
            return {
                "enabled": False, "is_active": False, "interval_hours": 3.0,
                "base_url": str(getattr(self.settings, "wechat_source_url", "")),
                "next_run_at": None, "last_triggered_at": None, "last_result": None,
                "daily_keyword_budget": 1550, "max_keywords_per_batch": 250,
                "budget": {}, "budget_breakdown": {}, "last_plan": None, "last_discovery": None,
            }
        payload = json.loads(row["payload_json"] or "{}")
        return {
            "enabled": bool(row["enabled"]),
            "is_active": bool(row["active_refresh_job_id"]),
            "interval_hours": payload.get("interval_hours", 3.0),
            "base_url": payload.get("base_url", str(getattr(self.settings, "wechat_source_url", ""))),
            "daily_keyword_budget": payload.get("daily_keyword_budget", 1550),
            "max_keywords_per_batch": payload.get("max_keywords_per_batch", 250),
            "next_run_at": row["next_run_at"],
            "last_run_at": row["last_run_at"],
            "last_triggered_at": payload.get("last_triggered_at"),
            "last_result": payload.get("last_result"),
            "budget": payload.get("budget", {}),
            "budget_breakdown": payload.get("budget_breakdown", {}),
            "last_plan": payload.get("last_plan"),
            "last_discovery": payload.get("last_discovery"),
            "last_error": payload.get("last_error"),
        }

    def scheduler_trigger(self, *, key: str, request_id: str | None = None) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            state = con.execute("SELECT payload_json FROM search_scheduler_state WHERE system_key=? AND platform=?", (self.MODULE, self.PLATFORM)).fetchone()
            config = json.loads(state["payload_json"] or "{}") if state else {}
            max_batch = int(config.get("max_keywords_per_batch", 250))
            ids = [
                x["keyword_id"]
                for x in con.execute(
                    """SELECT k.keyword_id
                       FROM keywords k
                       LEFT JOIN search_keyword_settings s
                         ON s.keyword_id=k.keyword_id
                        AND s.system_key=? AND s.platform=?
                       WHERE k.platform=? AND k.status='active'
                         AND COALESCE(s.batch_default_selected,1)=1
                       ORDER BY k.keyword_id LIMIT ?""",
                    (self.MODULE, self.PLATFORM, self.PLATFORM, max_batch),
                )
            ]
        if not ids:
            raise ValidationAppError("no keywords found")
        batch = self.refresh_batch(keyword_ids=ids, key=key, source="scheduler", request_id=request_id)
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    state_row = con.execute(
                        "SELECT payload_json FROM search_scheduler_state WHERE system_key=? AND platform=?",
                        (self.MODULE, self.PLATFORM),
                    ).fetchone()
                    state_payload = json.loads(state_row["payload_json"] or "{}") if state_row else {}
                    state_payload.update({
                        "last_triggered_at": _now(),
                        "last_result": f"done:{batch.get('status')}",
                        "last_error": None if batch.get("status") == "succeeded" else batch.get("error"),
                        "last_plan": state_payload.get("last_plan"),
                        "last_discovery": state_payload.get("last_discovery"),
                    })
                    now = _now()
                    con.execute(
                        "UPDATE search_scheduler_state SET payload_json=?,updated_at=? WHERE system_key=? AND platform=?",
                        (_json(state_payload), now, self.MODULE, self.PLATFORM),
                    )
                    status = self.scheduler_status_from_connection(con)
                    status_hash = _hash(status)
                    status_manifest = self._manifest(con, source_ref="provider:scheduler", source_hash=status_hash, now=now)
                    self._runtime_projection(
                        con,
                        subject_id="scheduler",
                        payload={"runtime_subtype": "scheduler", **status},
                        source_hash=status_hash,
                        manifest_id=status_manifest,
                        source_ref="provider:scheduler",
                    )
                    self._audit(
                        con,
                        action="wechat.scheduler.trigger",
                        subject_type="scheduler",
                        subject_id="scheduler",
                        outcome="succeeded" if batch.get("status") == "succeeded" else "blocked" if batch.get("status") == "blocked" else "failed",
                        details={"batch_id": batch.get("batch_id"), "status": batch.get("status")},
                        request_id=request_id,
                    )
        return {
            **status,
            "source": "scheduler",
            "trigger_status": "blocked" if batch.get("status") == "blocked" else "triggered",
            "blocked": batch.get("status") == "blocked",
            "batch_id": batch.get("batch_id"),
            "batch_status": batch.get("status"),
            "batch": batch,
        }

    def runtime(self, job_id: str, *, batch: bool) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                """SELECT 1
                   FROM search_refresh_jobs
                   WHERE refresh_job_id=? AND system_key=? AND platform=?""",
                (job_id, self.MODULE, self.PLATFORM),
            ).fetchone()
            if not row:
                raise NotFoundError("微信刷新任务" if not batch else "微信刷新批次", job_id)
            return self._job_payload(con, job_id)

    def history(self) -> list[dict[str, Any]]:
        with connect(self.settings, readonly=True) as con:
            rows = con.execute("SELECT refresh_job_id FROM search_refresh_jobs WHERE system_key=? AND platform=? ORDER BY created_at DESC", (self.MODULE, self.PLATFORM)).fetchall()
            return [self._job_payload(con, row["refresh_job_id"]) for row in rows]

    def scheduler_status(self) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            row = con.execute("SELECT * FROM search_scheduler_state WHERE system_key=? AND platform=?", (self.MODULE, self.PLATFORM)).fetchone()
            if not row:
                return {"enabled": False, "is_active": False, "interval_hours": 3.0, "daily_keyword_budget": 1550, "max_keywords_per_batch": 250, "next_run_at": None, "last_run_at": None}
            payload = json.loads(row["payload_json"] or "{}")
            return {"enabled": bool(row["enabled"]), "is_active": bool(row["active_refresh_job_id"]), "interval_hours": payload.get("interval_hours", 3.0), "daily_keyword_budget": payload.get("daily_keyword_budget", 1550), "max_keywords_per_batch": payload.get("max_keywords_per_batch", 250), "next_run_at": row["next_run_at"], "last_run_at": row["last_run_at"], "last_error": payload.get("last_error"), "last_plan": payload.get("last_plan"), "last_discovery": payload.get("last_discovery")}
