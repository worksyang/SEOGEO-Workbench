"""Wiki / 母文章库 路由。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from content_hub.db.connection import connect
from content_hub.errors import AppError
from content_hub.services.wiki import WikiService

router = APIRouter(prefix="/api/v1/wiki", tags=["wiki"])


def _wiki_source_roots(settings) -> list[Path]:
    roots: list[Path] = [Path(r) for r in settings.wiki_allowed_roots]
    return [r for r in roots if r.exists()]


def _service(request: Request, *, readonly: bool = True) -> WikiService:
    settings = request.app.state.settings
    return WikiService(
        connection=connect(settings, readonly=readonly),
        asset_root=Path(settings.asset_store_path),
        source_roots=_wiki_source_roots(settings),
        lock_path=Path(settings.lock_path),
    )


@router.get("/tree")
def tree(request: Request) -> dict:
    return {"ok": True, "data": _service(request).tree()}


@router.get("/search")
def search(request: Request, query: str = Query(""), limit: int = Query(50, ge=1, le=200)) -> dict:
    items = _service(request).search(query, limit=limit)
    return {"ok": True, "data": {"items": items, "total": len(items)}}


@router.post("/import")
def import_wiki(request: Request, payload: dict | None = None) -> dict:
    payload = payload or {}
    confirm = bool(payload.get("confirm", False))
    try:
        max_files = int(payload.get("max_files", 2000))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="max_files 必须是正整数") from exc
    if max_files < 1:
        raise HTTPException(status_code=400, detail="max_files 必须是正整数")
    result = _service(request, readonly=not confirm).import_wiki(
        confirm=confirm,
        max_files=max_files,
        operator=str(payload.get("operator") or "user"),
    )
    if result.get("status") == "blocked":
        return {"ok": False, "data": result}
    return {"ok": True, "data": result}


@router.get("/{content_id}")
def read(request: Request, content_id: str) -> dict:
    detail = _service(request).read(content_id)
    if not detail:
        raise HTTPException(status_code=404, detail="母文章不存在")
    return {"ok": True, "data": detail}


@router.put("/{content_id}")
def save(request: Request, content_id: str, payload: dict) -> dict:
    body = str(payload.get("body") or "")
    if not body.strip():
        raise HTTPException(status_code=400, detail="正文不能为空")
    operator = str(payload.get("operator") or "user")
    try:
        result = _service(request, readonly=False).save(content_id, body=body, operator=operator)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, ValueError, AppError):
        raise HTTPException(status_code=400, detail="母文章路径不安全或不可读取")
    return {"ok": True, "data": result}
