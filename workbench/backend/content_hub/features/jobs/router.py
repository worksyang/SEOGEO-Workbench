"""Jobs 路由：列出最近任务 / 取消 / 详情。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from content_hub.db.connection import connect
from content_hub.services.jobs import JobsService

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


def _service(request: Request) -> JobsService:
    return JobsService(connect(request.app.state.settings, readonly=False))


@router.get("")
def list_jobs(request: Request, limit: int = Query(30, ge=1, le=200)) -> dict:
    return {"ok": True, "data": {"items": _service(request).list_recent(limit=limit)}}


@router.get("/{job_id}")
def detail(request: Request, job_id: str) -> dict:
    data = _service(request).detail(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True, "data": data}


@router.post("/{job_id}/cancel")
def cancel(request: Request, job_id: str) -> dict:
    svc = _service(request)
    ok = svc.cancel(job_id, reason="user cancelled")
    if not ok:
        raise HTTPException(status_code=409, detail="任务不可被取消")
    return {"ok": True}
