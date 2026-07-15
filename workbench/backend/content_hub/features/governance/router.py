"""Governance 路由：身份合并候选、对账报告、信号消费、矫正任务。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from content_hub.db.connection import connect
from content_hub.ingestion.reconcile import ReconcileEngine
from content_hub.services.audit import AuditService
from content_hub.services.backup import BackupService
from content_hub.services.safety import scrub_public_payload

router = APIRouter(prefix="/api/v1/governance", tags=["governance"])


def _backup_service(request: Request) -> BackupService:
    return BackupService.from_settings(request.app.state.settings)


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


@router.get("/states")
def batch_states(request: Request, limit: int = Query(60, ge=1, le=200)) -> dict:
    """成稿状态机视图：按 production_jobs.status 分组列出最近批次。"""
    with connect(request.app.state.settings, readonly=True) as connection:
        rows = connection.execute(
            "SELECT job_id, job_type, status, input_signal_ids_json, created_at, updated_at "
            "FROM production_jobs ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        columns: dict[str, list] = {}
        for row in rows:
            item = dict(row)
            try:
                item["input_signal_ids"] = json.loads(item.pop("input_signal_ids_json") or "[]")
            except Exception:
                item["input_signal_ids"] = []
            columns.setdefault(item["status"], []).append(item)
    return {"ok": True, "data": {"columns": columns, "total": len(rows)}}


@router.get("/lineage")
def lineage(request: Request) -> dict:
    with connect(request.app.state.settings, readonly=True) as connection:
        signals = connection.execute(
            "SELECT signal_id, signal_type, subject_id, detected_at FROM signals ORDER BY detected_at DESC LIMIT 30"
        ).fetchall()
        production = connection.execute(
            "SELECT job_id, job_type, status, output_content_id, created_at FROM production_jobs ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
        attempts = connection.execute(
            "SELECT attempt_id, account_key, mode, status, attempted_at FROM publish_attempts ORDER BY attempted_at DESC LIMIT 30"
        ).fetchall()
    nodes = []
    for row in signals:
        nodes.append({"kind": "signal", "id": row["signal_id"], "label": row["signal_type"], "subject_id": row["subject_id"], "ts": row["detected_at"]})
    for row in production:
        nodes.append({"kind": "production", "id": row["job_id"], "label": row["job_type"], "status": row["status"], "subject_id": row["output_content_id"], "ts": row["created_at"]})
    for row in attempts:
        nodes.append({"kind": "publish", "id": row["attempt_id"], "label": row["mode"], "subject_id": row["account_key"], "status": row["status"], "ts": row["attempted_at"]})
    return {"ok": True, "data": {"nodes": nodes, "total": len(nodes)}}


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
