"""WritingMoney 路由。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from content_hub.db.connection import connect
from content_hub.db.writer_lock import writer_lock
from content_hub.ingestion.markdown_store import MarkdownStore
from content_hub.services.writing import FakeProvider, WritingService
from content_hub.validation.timestamps import utc_now_iso

router = APIRouter(prefix="/api/v1/writing", tags=["writing"])


def _provider(settings) -> FakeProvider | None:
    provider_kind = settings.writing_provider_kind.strip().lower()
    provider_status = settings.writing_provider_status.strip().lower()
    return (
        FakeProvider()
        if provider_kind in {"fake", "fake_provider", "demo"}
        and provider_status in {"demo_only", "ready", "configured", "enabled"}
        else None
    )


def _service(request: Request, connection: sqlite3.Connection) -> WritingService:
    settings = request.app.state.settings
    return WritingService(
        connection=connection,
        markdown_store=MarkdownStore(Path(settings.asset_store_path)),
        provider=_provider(settings),
        provider_kind=settings.writing_provider_kind,
        provider_status=settings.writing_provider_status,
    )


def _record_connection(connection: sqlite3.Connection, request: Request) -> None:
    """登记写作能力状态；不把 Provider secret 带入数据库。"""
    settings = request.app.state.settings
    kind = settings.writing_provider_kind.strip().lower()
    status = settings.writing_provider_status.strip().lower()
    if kind in {"fake", "fake_provider", "demo"} and status in {
        "demo_only", "ready", "configured", "enabled"
    }:
        connection_status = "degraded"
        reason = "fake_provider_demo_only"
    elif kind == "unconfigured" or status in {"unconfigured", "disabled", "blocked"}:
        connection_status = "blocked"
        reason = "writing_provider_unconfigured"
    else:
        connection_status = "degraded"
        reason = "writing_provider_not_verified"
    connection.execute(
        """
        INSERT INTO system_connections(
            system_key, display_name, base_url, status, last_checked_at,
            capabilities_json, details_json
        ) VALUES ('writing-money', 'WritingMoney', NULL, ?, ?, ?, ?)
        ON CONFLICT(system_key) DO UPDATE SET
            display_name=excluded.display_name,
            base_url=NULL,
            status=excluded.status,
            last_checked_at=excluded.last_checked_at,
            capabilities_json=excluded.capabilities_json,
            details_json=excluded.details_json
        """,
        (
            connection_status,
            utc_now_iso(),
            json.dumps(
                ["project_create", "material_select", "batch_generate"],
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "provider_kind": settings.writing_provider_kind,
                    "provider_status": settings.writing_provider_status,
                    "reason_code": reason,
                    "source_kind": "hub_writing_service",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )


@router.get("/jobs")
def list_jobs(request: Request, limit: int = 30) -> dict:
    settings = request.app.state.settings
    connection = connect(settings, readonly=True)
    try:
        svc = _service(request, connection)
        items = svc.list_jobs(limit=limit)
        return {"ok": True, "data": {"items": items, "total": len(items)}}
    finally:
        connection.close()


@router.post("/jobs")
def create_job(request: Request, payload: dict) -> dict:
    settings = request.app.state.settings
    with writer_lock(Path(settings.lock_path)):
        connection = connect(settings, readonly=False)
        try:
            _record_connection(connection, request)
            svc = _service(request, connection)
            mode = payload.get("mode") or "batch_production"
            if mode == "mother_forge":
                job = svc.create_mother_forge(
                    topic=payload.get("topic") or "未命名母文章",
                    purpose=payload.get("purpose") or "",
                    urls=payload.get("urls") or [],
                    recommended_mothers=payload.get("recommended_mothers") or [],
                )
            else:
                job = svc.create_batch(
                    topic=payload.get("topic") or "未命名批次",
                    source=payload.get("source") or "manual",
                    requirements=payload.get("requirements") or {},
                    keywords=payload.get("keywords") or [],
                    target_article_count=int(payload.get("target_article_count") or 1),
                    matched_articles=payload.get("matched_articles") or [],
                )
            connection.commit()
            return {
                "ok": True,
                "data": {
                    "job_id": job.job_id,
                    "job_type": job.job_type,
                    "status": job.status,
                    "provider_kind": settings.writing_provider_kind,
                    "provider_status": settings.writing_provider_status,
                    "demo": _provider(settings) is not None,
                },
            }
        finally:
            connection.close()


@router.post("/jobs/{job_id}/run")
def run_job(request: Request, job_id: str) -> dict:
    settings = request.app.state.settings
    with writer_lock(Path(settings.lock_path)):
        connection = connect(settings, readonly=False)
        try:
            _record_connection(connection, request)
            svc = _service(request, connection)
            result = svc.run(job_id, operator="user")
            connection.commit()
            return {"ok": result["status"] not in {"failed", "blocked", "demo_only"}, "data": result}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            connection.close()


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: str) -> dict:
    settings = request.app.state.settings
    connection = connect(settings, readonly=True)
    try:
        svc = _service(request, connection)
        detail = svc.detail(job_id)
        if not detail:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"ok": True, "data": detail}
    finally:
        connection.close()


@router.post("/jobs/{job_id}/mutate")
def mutate_job(request: Request, job_id: str, payload: dict) -> dict:
    """旧 WritingMoney 岛屿的持久化写入口。

    页面仍保持原版 DOM 和交互，但任何会改变项目、素材、模板、方案或批次队列
    的操作都通过这里进入 production_jobs + job_events + audit_log。
    """
    settings = request.app.state.settings
    action = str(payload.get("action") or "").strip()
    value = payload.get("value")
    if not action or not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="缺少合法的 action/value")
    operator = str(payload.get("operator") or "user")[:120]
    try:
        with writer_lock(Path(settings.lock_path)):
            connection = connect(settings, readonly=False)
            try:
                _record_connection(connection, request)
                result = _service(request, connection).mutate(
                    job_id,
                    action=action,
                    value=value,
                    operator=operator,
                )
                connection.commit()
                return {"ok": True, "data": result}
            finally:
                connection.close()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"任务不存在：{exc.args[0]}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
