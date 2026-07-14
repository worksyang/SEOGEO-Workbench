"""Contents 路由：内容列表 / 详情 / context / metrics / markdown。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from content_hub.db.connection import connect
from content_hub.ingestion.markdown_store import MarkdownStore
from content_hub.services.content import ContentService

router = APIRouter(prefix="/api/v1/contents", tags=["contents"])


def _service(request: Request, *, readonly: bool) -> ContentService:
    settings = request.app.state.settings
    return ContentService(
        connection=connect(settings, readonly=readonly),
        markdown_store=MarkdownStore(Path(settings.asset_store_path)),
    )


@router.get("")
def list_contents(
    request: Request,
    content_type: str | None = None,
    query: str | None = None,
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    svc = _service(request, readonly=True)
    data = svc.list(content_type=content_type, query=query, limit=limit, offset=offset)
    return {"ok": True, "data": data}


@router.get("/{content_id}")
def detail(request: Request, content_id: str) -> dict:
    svc = _service(request, readonly=True)
    record = svc.detail(content_id)
    if not record:
        raise HTTPException(status_code=404, detail="content 不存在")
    return {"ok": True, "data": record}


@router.get("/{content_id}/context")
def context(request: Request, content_id: str) -> dict:
    svc = _service(request, readonly=True)
    data = svc.context(content_id)
    if not data:
        raise HTTPException(status_code=404, detail="content 不存在")
    return {"ok": True, "data": data}


@router.get("/{content_id}/discoveries")
def discoveries(request: Request, content_id: str) -> dict:
    svc = _service(request, readonly=True)
    data = svc.context(content_id)
    if not data:
        raise HTTPException(status_code=404, detail="content 不存在")
    return {"ok": True, "data": {"discoveries": data["discoveries"], "identifiers": data["identifiers"]}}


@router.get("/{content_id}/metrics")
def metrics(request: Request, content_id: str) -> dict:
    svc = _service(request, readonly=True)
    return {"ok": True, "data": svc.metrics(content_id)}


@router.get("/{content_id}/markdown")
def markdown(request: Request, content_id: str) -> dict:
    svc = _service(request, readonly=True)
    data = svc.markdown(content_id)
    if not data:
        raise HTTPException(status_code=404, detail="content 不存在")
    return {"ok": True, "data": data}
