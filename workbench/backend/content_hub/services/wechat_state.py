from __future__ import annotations

import json
import hashlib
import uuid
from typing import Any, Callable

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.errors import AppError, ConflictError, ValidationAppError
from content_hub.repositories.wechat_state import (
    WechatStateRepository,
    canonical_payload,
    now_iso,
)
from content_hub.services.audit import AuditService


MODULE = "wechat-search"


class StateCommandService:
    def __init__(self, settings: Any):
        self.settings = settings

    def execute(
        self,
        command_type: str,
        payload: dict[str, Any],
        operation: Callable[[WechatStateRepository], dict[str, Any]],
        *,
        idempotency_key: str | None = None,
        actor_id: str = "user",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        key = (idempotency_key or "").strip()
        if not key:
            raise ValidationAppError("状态命令必须提供非空 idempotency_key。")
        input_json = canonical_payload(payload)
        now = now_iso()
        action = f"wechat.{command_type}"
        subject_payload = (
            payload.get("command")
            if isinstance(payload.get("command"), dict)
            else payload
        )
        failure: AppError | None = None
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con, transaction(con):
                prior = con.execute(
                    "SELECT * FROM command_runs WHERE module_key=? AND idempotency_key=?",
                    (MODULE, key),
                ).fetchone()
                if prior:
                    if prior["input_json"] != input_json or prior["command_type"] != command_type:
                        raise ConflictError("idempotency key 已用于不同请求")
                    if prior["status"] == "succeeded":
                        return json.loads(prior["output_json"] or "{}")
                    if prior["status"] in {"failed", "blocked"}:
                        error = json.loads(prior["error_json"] or "{}")
                        raise AppError(error.get("message", "命令执行失败"), error.get("code", "COMMAND_FAILED"), int(error.get("status", 400)))
                command_id = f"cmd_wechat_{uuid.uuid4().hex}"
                con.execute(
                    """INSERT INTO command_runs(
                       command_id,module_key,command_type,idempotency_key,actor_id,request_id,
                       status,input_json,output_json,error_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?, 'running',?,'{}','{}',?,?)""",
                    (command_id, MODULE, command_type, key, actor_id[:120] or "user",
                     request_id, input_json, now, now),
                )
                try:
                    output = operation(WechatStateRepository(con))
                except Exception as exc:
                    if isinstance(exc, AppError):
                        error = {"code": exc.code, "message": exc.message, "status": exc.status_code}
                        status = exc.status_code
                    else:
                        error = {"code": "COMMAND_FAILED", "message": "状态写入失败", "status": 500}
                        status = 500
                    con.execute(
                        "UPDATE command_runs SET status='failed',error_json=?,updated_at=? WHERE command_id=?",
                        (canonical_payload(error), now_iso(), command_id),
                    )
                    AuditService(con).record(
                        action=action, subject_type="wechat_keyword",
                        subject_id=str(
                            subject_payload.get("keyword_id")
                            or subject_payload.get("group_id")
                            or ""
                        ),
                        actor_id=actor_id, outcome="failed", request_id=request_id,
                        details={"command_id": command_id, "error": error},
                    )
                    self._receipt(con, command_id, key, "not_written", "failed", "blocked", {"error": error}, now_iso())
                    failure = AppError(error["message"], error["code"], status)
                else:
                    output = self._project(
                        con,
                        output,
                        command_type,
                        subject_payload,
                    )
                    con.execute(
                        "UPDATE command_runs SET status='succeeded',output_json=?,updated_at=? WHERE command_id=?",
                        (canonical_payload(output), now_iso(), command_id),
                    )
                    AuditService(con).record(
                        action=action, subject_type="wechat_keyword", subject_id=str(output.get("keyword_id") or output.get("group_id") or ""),
                        actor_id=actor_id, outcome="succeeded", request_id=request_id,
                        details={"command_id": command_id, "idempotency_key": key},
                    )
                    self._receipt(con, command_id, key, "projected", "succeeded", "matched", {"projection": "wechat_legacy_projections"}, now_iso())
        if failure is not None:
            raise failure
        return output

    @staticmethod
    def _receipt(con, command_id: str, key: str, legacy: str, hub: str, reconcile: str, details: dict[str, Any], now: str) -> None:
        con.execute(
            """INSERT INTO dual_write_receipts(
               receipt_id,module_key,command_id,idempotency_key,legacy_status,hub_status,
               reconcile_status,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(module_key,idempotency_key) DO UPDATE SET
              command_id=excluded.command_id,legacy_status=excluded.legacy_status,
              hub_status=excluded.hub_status,reconcile_status=excluded.reconcile_status,
              details_json=excluded.details_json""",
            (f"dwr_wechat_{uuid.uuid4().hex}", MODULE, command_id, key, legacy, hub, reconcile, canonical_payload(details), now),
        )

    @staticmethod
    def _project(
        con,
        output: dict[str, Any],
        command_type: str,
        command_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Keep compatibility projections readable after a state write.

        The projection is deliberately updated in the same transaction as the
        command. It never replaces the v3.3 facts or writes to source/**.
        """
        states = StateCommandService._projection_states(con)
        keyword_id = str(output.get("keyword_id") or "")
        source_keyword_id = str(
            (command_payload or {}).get("keyword_id") or ""
        )
        detail_ids = sorted(
            {
                value
                for value in (keyword_id, source_keyword_id)
                if value
            }
        )
        detail_clause = ""
        params: list[Any] = []
        if detail_ids:
            detail_clause = (
                " OR (projection_kind='keyword' AND subject_id IN ("
                + ",".join("?" for _ in detail_ids)
                + "))"
            )
            params.extend(detail_ids)
        rows = con.execute(
            """SELECT projection_id,projection_kind,subject_id,payload_json,
                      source_manifest_id,source_ref,updated_at
               FROM wechat_legacy_projections
               WHERE projection_kind IN ('bootstrap','full','keyword_manage')
            """
            + detail_clause
            + " ORDER BY updated_at DESC,projection_id DESC",
            params,
        ).fetchall()
        latest_rows: dict[tuple[str, str], Any] = {}
        for row in rows:
            latest_rows.setdefault(
                (str(row["projection_kind"]), str(row["subject_id"])),
                row,
            )
        detail_payloads: dict[str, tuple[Any, dict[str, Any]]] = {}
        for row in latest_rows.values():
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if row["projection_kind"] == "keyword_manage":
                payload = StateCommandService._merge_manage_projection(payload, states)
            elif row["projection_kind"] in {"bootstrap", "full"}:
                payload = StateCommandService._merge_collection_projection(payload, states)
            elif row["projection_kind"] == "keyword":
                subject_id = str(row["subject_id"])
                subject_state = states["by_id"].get(subject_id)
                if subject_state:
                    StateCommandService._merge_keyword_nodes(
                        payload,
                        {subject_id: subject_state},
                    )
                detail_payloads[subject_id] = (row, payload)
            encoded = canonical_payload(payload)
            con.execute(
                """UPDATE wechat_legacy_projections
                   SET payload_json=?,source_hash=?,source_ref=?,updated_at=?
                   WHERE projection_id=?""",
                (encoded, hashlib.sha256(encoded.encode()).hexdigest(),
                 f"hub:wechat-state:{command_type}", now_iso(), row["projection_id"]),
            )
        if (
            keyword_id
            and keyword_id not in detail_payloads
            and command_type in {"keyword.create", "keyword.update"}
        ):
            target_state = states["by_id"].get(keyword_id, output)
            source_detail = detail_payloads.get(source_keyword_id)
            if source_detail:
                source_row, source_payload = source_detail
                payload = json.loads(canonical_payload(source_payload))
                source_manifest_id = str(source_row["source_manifest_id"])
            else:
                payload = {}
                source_manifest_id = "hub-wechat-state"
            StateCommandService._remap_keyword_detail(
                payload,
                source_keyword_id,
                keyword_id,
                target_state,
            )
            encoded = canonical_payload(payload)
            con.execute(
                """INSERT INTO wechat_legacy_projections(
                   projection_id,projection_kind,subject_id,payload_json,
                   source_hash,source_manifest_id,source_ref,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    f"wechat-state-keyword-{uuid.uuid4().hex}",
                    "keyword",
                    keyword_id,
                    encoded,
                    hashlib.sha256(encoded.encode()).hexdigest(),
                    source_manifest_id,
                    f"hub:wechat-state:{command_type}",
                    now_iso(),
                ),
            )
        return output

    @staticmethod
    def _remap_keyword_detail(
        payload: dict[str, Any],
        source_keyword_id: str,
        target_keyword_id: str,
        target_state: dict[str, Any],
    ) -> None:
        """复制详情动态事实时只替换关键词身份和当前运行状态。"""

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                node_id = str(value.get("keyword_id") or "")
                if node_id in {source_keyword_id, target_keyword_id}:
                    value["keyword_id"] = target_keyword_id
                    value.update(target_state)
                    if "keyword" in value:
                        value["keyword"] = target_state.get(
                            "keyword_text",
                            value.get("keyword", ""),
                        )
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload)
        payload.update(target_state)
        payload["keyword_id"] = target_keyword_id
        payload["keyword"] = target_state.get("keyword_text", "")

    @staticmethod
    def _projection_states(con) -> dict[str, Any]:
        repo = WechatStateRepository(con)
        rows = con.execute(
            """SELECT k.keyword_id
               FROM keywords k
               LEFT JOIN search_keyword_settings s
                 ON s.keyword_id=k.keyword_id
                AND s.system_key='wechat-search'
                AND s.platform='wechat-search'
               WHERE k.platform='wechat-search'
               ORDER BY COALESCE(s.keyword_order,999999),k.keyword,k.keyword_id"""
        ).fetchall()
        by_id = {str(row["keyword_id"]): repo._state(str(row["keyword_id"])) for row in rows}
        groups = con.execute(
            """SELECT group_id,group_name,sort_order FROM search_keyword_groups
               WHERE system_key='wechat-search' AND platform='wechat-search'
                 AND archived_at IS NULL ORDER BY sort_order,group_name,group_id"""
        ).fetchall()
        return {
            "by_id": by_id,
            "active": {key: value for key, value in by_id.items() if value.get("status") == "active"},
            "groups": [
                {"group_id": row["group_id"], "label": row["group_name"], "order": row["sort_order"]}
                for row in groups
            ],
        }

    @staticmethod
    def _merge_keyword_nodes(value: Any, states: dict[str, dict[str, Any]]) -> None:
        if isinstance(value, dict):
            if str(value.get("keyword_id") or "") in states:
                value.update(states[str(value["keyword_id"])])
            for child in value.values():
                StateCommandService._merge_keyword_nodes(child, states)
        elif isinstance(value, list):
            for child in value:
                StateCommandService._merge_keyword_nodes(child, states)

    @staticmethod
    def _merge_collection_projection(payload: dict[str, Any], states: dict[str, Any]) -> dict[str, Any]:
        active = states["active"]
        existing = payload.get("keywords")
        if isinstance(existing, list):
            by_id = {
                str(item.get("keyword_id")): item
                for item in existing
                if isinstance(item, dict) and item.get("keyword_id")
            }
            for key, state in active.items():
                if key in by_id:
                    by_id[key].update(state)
                else:
                    legacy_state = dict(state)
                    legacy_state.setdefault("keyword", state.get("keyword_text", ""))
                    existing.append(legacy_state)
            payload["keywords"] = [item for item in existing if str(item.get("keyword_id") or "") in active]
        StateCommandService._merge_keyword_nodes(payload, active)
        if isinstance(payload.get("scope"), dict):
            payload["scope"].update({"total": len(active), "pinned": sum(bool(x.get("is_pinned")) for x in active.values())})
        if "pinned_keyword_count" in payload:
            payload["pinned_keyword_count"] = sum(bool(x.get("is_pinned")) for x in active.values())
        if "keyword_bucket_options" in payload:
            payload["keyword_bucket_options"] = sorted({x["keyword_bucket"] for x in active.values() if x.get("keyword_bucket")})
        return payload

    @staticmethod
    def _merge_manage_projection(payload: dict[str, Any], states: dict[str, Any]) -> dict[str, Any]:
        active = states["active"]
        old_groups = payload.get("groups") if isinstance(payload.get("groups"), list) else []
        old_by_group = {
            str(group.get("group_id")): group for group in old_groups
            if isinstance(group, dict) and group.get("group_id")
        }
        # A keyword may move between groups. Index every historical projection
        # node globally before rebuilding groups, otherwise moving it would
        # discard frozen dynamic fields (today_best/coverage/score/runs...).
        old_by_keyword: dict[str, dict[str, Any]] = {}
        for item in payload.get("keywords") if isinstance(payload.get("keywords"), list) else []:
            if isinstance(item, dict) and item.get("keyword_id"):
                old_by_keyword[str(item["keyword_id"])] = item
        for group in old_groups:
            for item in group.get("keywords", []) if isinstance(group, dict) and isinstance(group.get("keywords"), list) else []:
                if isinstance(item, dict) and item.get("keyword_id"):
                    old_by_keyword[str(item["keyword_id"])] = item
        new_groups: list[dict[str, Any]] = []
        merged_nodes: dict[str, dict[str, Any]] = {}
        for group in states["groups"]:
            current = dict(old_by_group.get(group["group_id"], {}))
            current.update(group)
            current["keywords"] = []
            for key, state in active.items():
                if state.get("group_id") == group["group_id"]:
                    item = dict(old_by_keyword.get(key, {}))
                    item.update(state)
                    current["keywords"].append(item)
                    merged_nodes[key] = item
            current["total"] = len(current["keywords"])
            current["ranked_count"] = sum(1 for item in current["keywords"] if item.get("today_best"))
            current["not_ranked_count"] = current["total"] - current["ranked_count"]
            new_groups.append(current)
        payload["groups"] = new_groups
        if isinstance(payload.get("keywords"), list):
            payload["keywords"] = [
                merged_nodes.get(key, dict(old_by_keyword.get(key, {}), **state))
                for key, state in active.items()
            ]
        ranked_total = sum(1 for item in merged_nodes.values() if item.get("today_best"))
        total = len(merged_nodes)
        payload.update({
            "total": total,
            "ranked_total": ranked_total,
            "not_ranked_total": total - ranked_total,
        })
        return payload
