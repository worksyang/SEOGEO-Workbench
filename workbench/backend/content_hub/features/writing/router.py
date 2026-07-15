"""WritingMoney 路由。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from content_hub.db.connection import connect
from content_hub.db.writer_lock import writer_lock
from content_hub.ingestion.markdown_store import MarkdownStore
from content_hub.services.writing import WritingService

router = APIRouter(prefix="/api/v1/writing", tags=["writing"])


def _service(request: Request) -> WritingService:
    settings = request.app.state.settings
    return WritingService(
        connection=connect(settings, readonly=False),
        markdown_store=MarkdownStore(Path(settings.asset_store_path)),
        provider_kind=settings.writing_provider_kind,
        provider_status=settings.writing_provider_status,
    )


@router.get("/jobs")
def list_jobs(request: Request, limit: int = 30) -> dict:
    svc = _service(request)
    return {"ok": True, "data": {"items": svc.list_jobs(limit=limit), "total": len(svc.list_jobs(limit=limit))}}


@router.post("/jobs")
def create_job(request: Request, payload: dict) -> dict:
    svc = _service(request)
    settings = request.app.state.settings
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
    return {
        "ok": True,
        "data": {
            "job_id": job.job_id,
            "job_type": job.job_type,
            "status": job.status,
            "provider_kind": settings.writing_provider_kind,
            "provider_status": settings.writing_provider_status,
            "demo": False,
        },
    }


@router.post("/jobs/{job_id}/run")
def run_job(request: Request, job_id: str) -> dict:
    svc = _service(request)
    settings = request.app.state.settings
    try:
        with writer_lock(Path(settings.lock_path)):
            result = svc.run(job_id, operator="user")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": result["status"] not in {"failed", "blocked", "demo_only"}, "data": result}


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: str) -> dict:
    svc = _service(request)
    detail = svc.detail(job_id)
    if not detail:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True, "data": detail}
