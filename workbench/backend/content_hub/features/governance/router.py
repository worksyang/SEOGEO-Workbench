"""Governance 路由：身份合并候选、对账报告、信号消费、矫正任务。
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import WriterLockTimeout, writer_lock
from content_hub.domain.ids import generate_ulid_like
from content_hub.ingestion.reconcile import ReconcileEngine
from content_hub.services.audit import AuditService
from content_hub.services.backup import BackupService
from content_hub.services.migration import (
    MODES,
    WECHAT_CUTOVER_READ_CONTRACTS,
    WECHAT_FIXED_HUB_CONTRACTS,
)
from content_hub.services.safety import scrub_public_payload

router = APIRouter(prefix="/api/v1/governance", tags=["governance"])


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _backup_service(request: Request) -> BackupService:
    return BackupService.from_settings(request.app.state.settings)


def _public_json(raw: str | None, request: Request) -> Any:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        value = {}
    value = scrub_public_payload(value, asset_root=Path(request.app.state.settings.asset_store_path))
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) <= 8192:
        return value
    return {
        "truncated": True,
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "bytes": len(encoded.encode("utf-8")),
    }


def _public_diff(row: sqlite3.Row, request: Request) -> dict:
    item = dict(row)
    item["legacy_value"] = _public_json(item.pop("legacy_value_json", "null"), request)
    item["hub_value"] = _public_json(item.pop("hub_value_json", "null"), request)
    item["truncated"] = bool(item.get("truncated"))
    return item


def _wechat_cutover_inventory(connection: sqlite3.Connection) -> tuple[dict[str, sqlite3.Row], dict[str, sqlite3.Row]]:
    rows = connection.execute(
        """
        SELECT switch_id,contract_key,data_mode,enabled,rollback_mode
        FROM migration_switches
        WHERE module_key='wechat-search'
        """
    ).fetchall()
    by_contract = {str(row["contract_key"]): row for row in rows}
    reads = {
        contract: by_contract[contract]
        for contract in WECHAT_CUTOVER_READ_CONTRACTS
        if contract in by_contract
    }
    fixed_hub = {
        contract: by_contract[contract]
        for contract in WECHAT_FIXED_HUB_CONTRACTS
        if contract in by_contract
    }
    return reads, fixed_hub


def _validate_wechat_cutover_inventory(
    reads: dict[str, sqlite3.Row],
    fixed_hub: dict[str, sqlite3.Row],
    *,
    expected_mode: str | None,
) -> str:
    missing_reads = sorted(set(WECHAT_CUTOVER_READ_CONTRACTS) - set(reads))
    if missing_reads:
        raise HTTPException(
            status_code=409,
            detail=f"微信读契约清单不完整，整批未修改：{','.join(missing_reads)}",
        )
    disabled_reads = sorted(
        contract for contract, row in reads.items() if int(row["enabled"]) != 1
    )
    if disabled_reads:
        raise HTTPException(
            status_code=409,
            detail=f"微信读契约存在未启用项，整批未修改：{','.join(disabled_reads)}",
        )
    modes = {str(row["data_mode"]) for row in reads.values()}
    if expected_mode is not None:
        conflicts = sorted(
            contract
            for contract, row in reads.items()
            if str(row["data_mode"]) != expected_mode
        )
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"expected_mode={expected_mode} 与当前状态冲突，整批未修改："
                    f"{','.join(conflicts)}"
                ),
            )
        current_mode = expected_mode
    else:
        if len(modes) != 1:
            raise HTTPException(
                status_code=409,
                detail="22 个微信读契约当前模式不一致，整批未修改；请先显式修复状态。",
            )
        current_mode = next(iter(modes))

    missing_writes = sorted(set(WECHAT_FIXED_HUB_CONTRACTS) - set(fixed_hub))
    unsafe_writes = sorted(
        contract
        for contract, row in fixed_hub.items()
        if int(row["enabled"]) != 1
        or str(row["data_mode"]) != "hub"
        or str(row["rollback_mode"]) != "hub"
    )
    if missing_writes or unsafe_writes:
        affected = sorted(set(missing_writes) | set(unsafe_writes))
        raise HTTPException(
            status_code=409,
            detail=f"微信写入/外部契约未保持 Hub-only，整批未修改：{','.join(affected)}",
        )
    return current_mode


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _wechat_cutover_readiness(connection: sqlite3.Connection) -> dict[str, Any]:
    """在当前事务快照中检查微信整组切 Hub 所需的不可绕过证据。"""
    blockers: list[dict[str, Any]] = []

    batch = connection.execute(
        """
        SELECT batch_id,status,finished_at,created_at,updated_at,payload_json
        FROM ingestion_batches
        WHERE adapter_key='wechat-search' AND source_scope='history'
        ORDER BY created_at DESC, batch_id DESC
        LIMIT 1
        """
    ).fetchone()
    if batch is None:
        blockers.append({"code": "history_batch_missing", "message": "缺少微信 history ingestion batch"})
        return {"ready": False, "blockers": blockers}

    batch_id = str(batch["batch_id"])
    finished_at = str(batch["finished_at"] or "")
    payload = _json_object(batch["payload_json"])
    manifest = payload.get("manifest")
    manifest_id = str(
        payload.get("manifest_id")
        or (manifest.get("manifest_id") if isinstance(manifest, dict) else "")
        or ""
    )
    readiness: dict[str, Any] = {
        "ready": False,
        "blockers": blockers,
        "batch_id": batch_id,
        "finished_at": finished_at or None,
        "manifest_id": manifest_id or None,
    }

    if str(batch["status"]) != "succeeded":
        blockers.append(
            {"code": "history_batch_not_succeeded", "message": "最新微信 history ingestion batch 未成功完成"}
        )
    if not finished_at:
        blockers.append({"code": "history_batch_unfinished", "message": "最新微信 history ingestion batch 缺少 finished_at"})

    if finished_at:
        newer_bad = connection.execute(
            """
            SELECT 1
            FROM ingestion_batches
            WHERE adapter_key='wechat-search' AND source_scope='history'
              AND status IN ('running','partial_failed','failed')
              AND (
                    created_at > ?
                 OR COALESCE(updated_at, finished_at, created_at) > ?
              )
            LIMIT 1
            """,
            (finished_at, finished_at),
        ).fetchone()
        if newer_bad is not None:
            blockers.append(
                {"code": "newer_incomplete_batch", "message": "存在更新的 running/partial_failed/failed 微信 history batch"}
            )

    audit = payload.get("audit") if isinstance(payload.get("audit"), dict) else {}
    reconcile = audit.get("reconcile") if isinstance(audit.get("reconcile"), dict) else None
    if reconcile is None and isinstance(payload.get("reconcile"), dict):
        reconcile = payload["reconcile"]
    if not isinstance(reconcile, dict) or reconcile.get("status") != "matched" or reconcile.get("verified") is not True:
        blockers.append({"code": "reconcile_not_verified", "message": "微信 history batch 缺少 matched 且 verified 的 reconcile"})

    asset_integrity = payload.get("asset_integrity")
    if not isinstance(asset_integrity, dict) or asset_integrity.get("verified") is not True:
        blockers.append({"code": "asset_integrity_not_verified", "message": "微信 history batch 的 asset_integrity 未 verified"})

    checkpoint = connection.execute(
        """
        SELECT batch_id,source_hash,last_success_at
        FROM ingestion_checkpoints
        WHERE adapter_key='wechat-search' AND checkpoint_key='normalized'
        """
    ).fetchone()
    if (
        checkpoint is None
        or str(checkpoint["batch_id"] or "") != batch_id
        or not manifest_id
        or str(checkpoint["source_hash"] or "") != manifest_id
        or not str(checkpoint["last_success_at"] or "")
    ):
        blockers.append(
            {"code": "normalized_checkpoint_mismatch", "message": "normalized checkpoint 未与最新 batch/report manifest 对齐"}
        )

    if finished_at:
        missing_comparisons: list[str] = []
        for contract in WECHAT_CUTOVER_READ_CONTRACTS:
            comparison = connection.execute(
                """
                SELECT status
                FROM contract_comparisons
                WHERE module_key='wechat-search' AND contract_key=? AND compared_at > ?
                ORDER BY compared_at DESC, comparison_id DESC
                LIMIT 1
                """,
                (contract, finished_at),
            ).fetchone()
            if comparison is None or str(comparison["status"]) != "matched":
                missing_comparisons.append(contract)
        if missing_comparisons:
            blockers.append(
                {
                    "code": "contract_comparisons_incomplete",
                    "message": "微信读契约缺少 batch 完成后的最新 matched 对账",
                    "contract_keys": missing_comparisons,
                }
            )

        for action, code, message in (
            ("backup.snapshot", "backup_snapshot_missing", "缺少 batch 完成后的 succeeded backup.snapshot 审计"),
            ("backup.restore_drill", "backup_restore_drill_missing", "缺少 batch 完成后的 succeeded backup.restore_drill 审计"),
        ):
            audit_row = connection.execute(
                """
                SELECT details_json
                FROM audit_log
                WHERE action=? AND outcome='succeeded' AND occurred_at > ?
                ORDER BY occurred_at DESC, audit_id DESC
                LIMIT 1
                """,
                (action, finished_at),
            ).fetchone()
            if audit_row is None:
                blockers.append({"code": code, "message": message})
            elif action == "backup.restore_drill":
                details = _json_object(audit_row["details_json"])
                if details.get("runtime_database_unchanged") is not True:
                    blockers.append(
                        {
                            "code": "backup_restore_runtime_changed",
                            "message": "最新 succeeded backup.restore_drill 未证明 runtime_database_unchanged=true",
                        }
                    )

    readiness["ready"] = not blockers
    return readiness


@router.get("/backups")
def backups(request: Request) -> dict:
    records = _backup_service(request).list_backups()
    return {
        "ok": True,
        "data": {
            "items": [record.to_dict() for record in records],
            "total": len(records),
            "verifiable": sum(1 for record in records if record.verifiable),
        },
    }


@router.post("/backups")
def create_online_backup(request: Request, payload: dict | None = None) -> dict:
    body = payload or {}
    service = _backup_service(request)
    before = {record.name for record in service.list_backups()}
    try:
        record = service.snapshot(
            label=str(body.get("label") or "online"),
            reuse=body.get("reuse", True) is not False,
            actor_id=str(body.get("operator") or "user"),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail="备份创建或验证失败，请查看本机日志。") from exc
    return {"ok": True, "data": {"backup": record.to_dict(), "reused": record.name in before}}


@router.post("/backups/{backup_name:path}/restore-drill")
def restore_drill(request: Request, backup_name: str, payload: dict | None = None) -> dict:
    body = payload or {}
    try:
        result = _backup_service(request).restore_drill(
            backup_name,
            actor_id=str(body.get("operator") or "user"),
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise HTTPException(status_code=409, detail="恢复演练失败，请查看本机日志。") from exc
    return {"ok": True, "data": result}


@router.get("/identity")
def identity_candidates(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict:
    with connect(request.app.state.settings, readonly=True) as connection:
        rows = connection.execute(
            "SELECT candidate_id, left_content_id, right_content_id, confidence, status, evidence_json, "
            "created_at, reviewed_at, reviewed_by FROM identity_merge_candidates ORDER BY confidence DESC, created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        items: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            except Exception:
                item["evidence"] = {}
            items.append(item)
    return {"ok": True, "data": {"items": items, "total": len(items)}}


@router.post("/identity/{candidate_id}/merge")
def merge_candidate(request: Request, candidate_id: str, payload: dict) -> dict:
    audit = AuditService(connect(request.app.state.settings, readonly=False))
    audit.record(
        action="identity.merge",
        subject_type="content",
        subject_id=candidate_id,
        actor_id=payload.get("operator") or "user",
        details={"decision": "approve"},
    )
    return {"ok": True, "data": {"candidate_id": candidate_id, "status": "queued"}}


@router.get("/locks")
def locks(request: Request) -> dict:
    settings = request.app.state.settings
    with connect(settings, readonly=True) as connection:
        connections = [dict(row) for row in connection.execute(
            "SELECT system_key, display_name, status, last_checked_at FROM system_connections ORDER BY system_key"
        ).fetchall()]
        audit_rows = connection.execute(
            "SELECT action, subject_id, occurred_at, actor_id, outcome, details_json FROM audit_log ORDER BY occurred_at DESC LIMIT 30"
        ).fetchall()
        audit = []
        for row in audit_rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json") or "{}")
            except Exception:
                item["details"] = {}
            item["details"] = scrub_public_payload(item["details"], asset_root=Path(request.app.state.settings.asset_store_path))
            audit.append(item)
    return {"ok": True, "data": {"connections": connections, "audit": audit}}


@router.get("/switches")
def migration_switches(request: Request) -> dict:
    with connect(request.app.state.settings, readonly=True) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT switch_id,module_key,contract_key,data_mode,enabled,rollback_mode,
                       updated_at,updated_by,reason
                FROM migration_switches ORDER BY module_key,contract_key
                """
            ).fetchall()
        ]
    return {"ok": True, "data": {"items": rows, "total": len(rows)}}


@router.post("/switches/wechat-search/cutover")
def cutover_wechat_search(request: Request, payload: dict | None = None) -> dict:
    body = payload or {}
    target_mode = str(body.get("data_mode") or "").strip()
    expected_mode_raw = str(body.get("expected_mode") or "").strip()
    expected_mode = expected_mode_raw or None
    dry_run = body.get("dry_run") is True or body.get("preflight") is True
    actor = str(body.get("actor") or body.get("operator") or "user").strip()[:120] or "user"
    reason = str(body.get("reason") or "").strip()[:1000]

    if target_mode not in MODES:
        raise HTTPException(
            status_code=400,
            detail="data_mode 仅允许 legacy、compare 或 hub",
        )
    if expected_mode is not None and expected_mode not in MODES:
        raise HTTPException(
            status_code=400,
            detail="expected_mode 仅允许 legacy、compare 或 hub",
        )
    if target_mode == "hub" and body.get("confirm") is not True:
        raise HTTPException(status_code=409, detail="切换到 hub 模式必须明确 confirm=true")
    if target_mode == "hub" and not reason:
        raise HTTPException(status_code=409, detail="切换到 hub 模式必须填写 reason")

    settings = request.app.state.settings
    now = _utc_now()
    try:
        with writer_lock(settings.lock_path, timeout_seconds=0.25):
            with connect(settings, readonly=False) as connection:
                with transaction(connection):
                    reads, fixed_hub = _wechat_cutover_inventory(connection)
                    current_mode = _validate_wechat_cutover_inventory(
                        reads,
                        fixed_hub,
                        expected_mode=expected_mode,
                    )
                    readiness = (
                        _wechat_cutover_readiness(connection)
                        if target_mode == "hub"
                        else {"ready": True, "blockers": []}
                    )
                    contracts = [
                        {
                            "contract_key": contract,
                            "before_mode": current_mode,
                            "after_mode": target_mode,
                        }
                        for contract in WECHAT_CUTOVER_READ_CONTRACTS
                    ]
                    if dry_run:
                        return {
                            "ok": True,
                            "data": {
                                "module_key": "wechat-search",
                                "data_mode": target_mode,
                                "previous_mode": current_mode,
                                "expected_mode": expected_mode,
                                "dry_run": True,
                                "changed_count": 0,
                                "would_change_count": len(WECHAT_CUTOVER_READ_CONTRACTS),
                                "contract_count": len(WECHAT_CUTOVER_READ_CONTRACTS),
                                "fixed_hub_contract_count": len(WECHAT_FIXED_HUB_CONTRACTS),
                                "contract_keys": list(WECHAT_CUTOVER_READ_CONTRACTS),
                                "contracts": contracts,
                                "actor": actor,
                                "reason": reason,
                                "readiness": readiness,
                            },
                        }

                    if target_mode == "hub" and not readiness["ready"]:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "message": "微信整组切换到 hub 的 readiness gate 未通过，整批未修改。",
                                "readiness": readiness,
                            },
                        )

                    placeholders = ",".join("?" for _ in WECHAT_CUTOVER_READ_CONTRACTS)
                    cursor = connection.execute(
                        f"""
                        UPDATE migration_switches
                        SET data_mode=?,updated_at=?,updated_by=?,reason=?
                        WHERE module_key='wechat-search'
                          AND enabled=1
                          AND data_mode=?
                          AND contract_key IN ({placeholders})
                        """,
                        (
                            target_mode,
                            now,
                            actor,
                            reason,
                            current_mode,
                            *WECHAT_CUTOVER_READ_CONTRACTS,
                        ),
                    )
                    changed_count = int(cursor.rowcount)
                    if changed_count != len(WECHAT_CUTOVER_READ_CONTRACTS):
                        raise HTTPException(
                            status_code=409,
                            detail="微信读契约状态发生并发变化，整批已回滚。",
                        )
                    AuditService(connection).record(
                        action="migration_switch.wechat_cutover",
                        subject_type="migration_switch_group",
                        subject_id="wechat-search",
                        actor_id=actor,
                        outcome="succeeded",
                        request_id=request.headers.get("X-Request-ID"),
                        details={
                            "module_key": "wechat-search",
                            "contract_keys": list(WECHAT_CUTOVER_READ_CONTRACTS),
                            "contracts": contracts,
                            "contract_count": len(contracts),
                            "before_mode": current_mode,
                            "after_mode": target_mode,
                            "previous_mode": current_mode,
                            "data_mode": target_mode,
                            "expected_mode": expected_mode,
                            "actor": actor,
                            "reason": reason,
                            "changed_count": changed_count,
                        },
                    )
    except WriterLockTimeout as exc:
        raise HTTPException(
            status_code=409,
            detail="微信迁移总开关正被其他写事务占用，请重试。",
        ) from exc
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise HTTPException(
                status_code=409,
                detail="微信迁移总开关发生 SQLite 并发冲突，请重试。",
            ) from exc
        raise

    return {
        "ok": True,
        "data": {
            "module_key": "wechat-search",
            "data_mode": target_mode,
            "previous_mode": current_mode,
            "expected_mode": expected_mode,
            "dry_run": False,
            "changed_count": changed_count,
            "contract_count": len(WECHAT_CUTOVER_READ_CONTRACTS),
            "fixed_hub_contract_count": len(WECHAT_FIXED_HUB_CONTRACTS),
            "contract_keys": list(WECHAT_CUTOVER_READ_CONTRACTS),
            "contracts": contracts,
            "actor": actor,
            "reason": reason,
            "updated_at": now,
        },
    }


@router.put("/switches/{module_key}/{contract_key}")
def set_migration_switch(
    request: Request,
    module_key: str,
    contract_key: str,
    payload: dict,
) -> dict:
    mode = str(payload.get("data_mode") or "").strip()
    rollback_mode = str(payload.get("rollback_mode") or "legacy").strip()
    if mode not in {"legacy", "compare", "hub"} or rollback_mode not in {"legacy", "compare", "hub"}:
        raise HTTPException(status_code=400, detail="data_mode/rollback_mode 仅允许 legacy、compare 或 hub")
    if mode == "hub" and payload.get("confirm") is not True:
        raise HTTPException(status_code=409, detail="切换到 hub 模式必须明确 confirm=true")
    if not module_key or not contract_key:
        raise HTTPException(status_code=400, detail="module_key 与 contract_key 不能为空")
    if module_key == "wechat-search" and contract_key in WECHAT_CUTOVER_READ_CONTRACTS:
        raise HTTPException(
            status_code=409,
            detail="微信读契约必须通过整组 cutover 修改，禁止通用 PUT 单契约切换。",
        )
    if (
        module_key == "wechat-search"
        and contract_key in WECHAT_FIXED_HUB_CONTRACTS
        and (mode != "hub" or rollback_mode != "hub")
    ):
        raise HTTPException(
            status_code=409,
            detail="微信写入/外部契约固定为 hub/hub，禁止切回旧源。",
        )
    settings = request.app.state.settings
    now = _utc_now()
    actor = str(payload.get("operator") or "user")[:120]
    reason = str(payload.get("reason") or "")[:1000]
    with writer_lock(settings.lock_path):
        with connect(settings, readonly=False) as connection:
            previous = connection.execute(
                "SELECT data_mode FROM migration_switches WHERE module_key=? AND contract_key=?",
                (module_key, contract_key),
            ).fetchone()
            switch_id = (
                connection.execute(
                    "SELECT switch_id FROM migration_switches WHERE module_key=? AND contract_key=?",
                    (module_key, contract_key),
                ).fetchone()
            )
            switch_id = switch_id["switch_id"] if switch_id else generate_ulid_like("sw")
            connection.execute(
                """
                INSERT INTO migration_switches(
                    switch_id,module_key,contract_key,data_mode,enabled,rollback_mode,
                    updated_at,updated_by,reason
                ) VALUES(?,?,?,?,1,?,?,?,?)
                ON CONFLICT(module_key,contract_key) DO UPDATE SET
                    data_mode=excluded.data_mode,enabled=excluded.enabled,
                    rollback_mode=excluded.rollback_mode,updated_at=excluded.updated_at,
                    updated_by=excluded.updated_by,reason=excluded.reason
                """,
                (switch_id, module_key, contract_key, mode, rollback_mode, now, actor, reason),
            )
            AuditService(connection).record(
                action="migration_switch.update",
                subject_type="migration_switch",
                subject_id=switch_id,
                actor_id=actor,
                outcome="succeeded",
                details={
                    "module_key": module_key,
                    "contract_key": contract_key,
                    "previous_mode": previous["data_mode"] if previous else None,
                    "data_mode": mode,
                    "rollback_mode": rollback_mode,
                    "reason": reason,
                },
            )
            connection.commit()
    return {
        "ok": True,
        "data": {
            "switch_id": switch_id,
            "module_key": module_key,
            "contract_key": contract_key,
            "data_mode": mode,
            "rollback_mode": rollback_mode,
            "updated_at": now,
        },
    }


@router.post("/comparisons")
def record_contract_comparison(request: Request, payload: dict) -> dict:
    module_key = str(payload.get("module_key") or "").strip()
    contract_key = str(payload.get("contract_key") or "").strip()
    request_fingerprint = str(payload.get("request_fingerprint") or "").strip()
    legacy_hash = payload.get("legacy_hash")
    hub_hash = payload.get("hub_hash")
    if not module_key or not contract_key or not request_fingerprint:
        raise HTTPException(status_code=400, detail="module_key、contract_key、request_fingerprint 不能为空")
    status = "matched" if legacy_hash and legacy_hash == hub_hash else "different"
    comparison_id = generate_ulid_like("cmp")
    now = _utc_now()
    with writer_lock(request.app.state.settings.lock_path):
        with connect(request.app.state.settings, readonly=False) as connection:
            connection.execute(
                """
                INSERT INTO contract_comparisons(
                    comparison_id,module_key,contract_key,request_fingerprint,
                    legacy_hash,hub_hash,status,diff_json,compared_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    comparison_id, module_key, contract_key, request_fingerprint,
                    legacy_hash, hub_hash, status,
                    json.dumps(payload.get("diff") if isinstance(payload.get("diff"), dict) else {}, ensure_ascii=False),
                    now,
                ),
            )
            connection.commit()
    return {"ok": True, "data": {"comparison_id": comparison_id, "status": status, "compared_at": now}}


@router.get("/comparisons")
def list_contract_comparisons(
    request: Request,
    module_key: str | None = None,
    contract_key: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    allowed_statuses = {"matched", "different", "legacy_error", "hub_error"}
    if status and status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="status 不合法")
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("module_key", module_key),
        ("contract_key", contract_key),
        ("status", status),
    ):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if since:
        clauses.append("compared_at>=?")
        params.append(since)
    if until:
        clauses.append("compared_at<=?")
        params.append(until)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    settings = request.app.state.settings
    with connect(settings, readonly=True) as connection:
        total = connection.execute(
            f"SELECT COUNT(*) FROM contract_comparisons{where}", params
        ).fetchone()[0]
        rows = connection.execute(
            f"""
            SELECT comparison_id,module_key,contract_key,request_fingerprint,
                   legacy_hash,hub_hash,status,diff_json,compared_at,
                   diff_count,diffs_truncated
            FROM contract_comparisons{where}
            ORDER BY compared_at DESC, comparison_id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["summary"] = _public_json(item.pop("diff_json"), request)
        item["diffs_truncated"] = bool(item["diffs_truncated"])
        items.append(item)
    return {"ok": True, "data": {"items": items, "total": total, "limit": limit, "offset": offset}}


@router.get("/comparisons/{comparison_id}")
def contract_comparison_detail(request: Request, comparison_id: str) -> dict:
    settings = request.app.state.settings
    with connect(settings, readonly=True) as connection:
        comparison = connection.execute(
            """
            SELECT comparison_id,module_key,contract_key,request_fingerprint,
                   legacy_hash,hub_hash,status,diff_json,compared_at,
                   diff_count,diffs_truncated
            FROM contract_comparisons WHERE comparison_id=?
            """,
            (comparison_id,),
        ).fetchone()
        if comparison is None:
            raise HTTPException(status_code=404, detail="comparison 不存在")
        diffs = connection.execute(
            """
            SELECT diff_id,json_pointer,kind,legacy_value_json,hub_value_json,
                   severity,rule,truncated
            FROM contract_comparison_diffs
            WHERE comparison_id=? ORDER BY diff_id
            """,
            (comparison_id,),
        ).fetchall()
    summary = dict(comparison)
    summary["summary"] = _public_json(summary.pop("diff_json"), request)
    summary["diffs_truncated"] = bool(summary["diffs_truncated"])
    return {
        "ok": True,
        "data": {
            "comparison": summary,
            "diffs": [_public_diff(row, request) for row in diffs],
            "total_diffs": len(diffs),
        },
    }


@router.get("/reconcile")
def reconcile(request: Request) -> dict:
    settings = request.app.state.settings
    allowed_roots = [Path(settings.project_root), Path(settings.asset_store_path)] + [Path(p) for p in settings.allowed_roots]
    with connect(settings, readonly=False) as connection:
        engine = ReconcileEngine(connection, allowed_roots)
        results = engine.run()
        payload = {
            "ok": True,
            "data": {
                "results": [r.to_dict() for r in results],
                "total": len(results),
                "errors": sum(1 for r in results if r.severity == "error"),
                "warnings": sum(1 for r in results if r.severity == "warn"),
            },
        }
    report_root = Path(settings.database_path.parent / "reports" / "reconcile").resolve()
    report_root.mkdir(parents=True, exist_ok=True)
    report_name = f"reconcile_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    report_path = report_root / report_name
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["data"]["report"] = str(report_path.relative_to(settings.database_path.parent))
    return payload
