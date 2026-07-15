from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Request

from content_hub.features.geo.service import GeoService
from content_hub.errors import ValidationAppError

router = APIRouter(prefix="/api/v1/geo", tags=["geopromax"])


def svc(request: Request) -> GeoService:
    return GeoService(request.app.state.settings)


def _body_limit(body: dict[str, Any]) -> int | None:
    if "limit" not in body or body["limit"] is None:
        return None
    value = body["limit"]
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000:
        raise ValidationAppError("limit 必须是 1–10000 的整数。")
    return value


@router.get("/bootstrap")
def bootstrap(request: Request): return {"ok": True, "data": svc(request).bootstrap()}

@router.get("/status")
def status(request: Request): return {"ok": True, "data": svc(request).status()}

@router.get("/reconciliation")
def reconciliation(request: Request, limit: int = 100, offset: int = 0):
    return {"ok": True, "data": svc(request).reconciliation(limit=limit, offset=offset)}

@router.get("/questions")
def questions(request: Request, limit: int = 100, offset: int = 0):
    return {"ok": True, "data": svc(request).questions(limit=max(1, min(limit, 10000)), offset=max(0, offset))}

@router.get("/questions/{question_id}")
def question(question_id: str, request: Request):
    return {"ok": True, "data": svc(request).question_detail(question_id)}

@router.get("/source-overview")
def source_overview(request: Request, platform: str | None = None, q: str | None = None, limit: int = 100, offset: int = 0):
    return {"ok": True, "data": svc(request).source_overview(platform=platform, q=q, limit=limit, offset=offset)}

@router.get("/redfox/read-only")
def redfox_read_only(request: Request): return {"ok": True, "data": svc(request).redfox_read_only()}

def _list(name: str):
    def endpoint(request: Request, limit: int = 100, offset: int = 0):
        return {"ok": True, "data": svc(request).query(name, limit=max(1, min(limit, 10000)), offset=max(0, offset))}
    return endpoint

for _name in ("batches", "answers", "sources", "tools", "keywords", "metrics"):
    router.add_api_route(f"/{_name}", _list(_name), methods=["GET"], name=f"geo_{_name}")

@router.get("/batches/{item_id}")
def batch(item_id: int, request: Request): return {"ok": True, "data": svc(request).detail("batch", item_id)}

@router.get("/answers/{item_id}")
def answer(item_id: int, request: Request): return {"ok": True, "data": svc(request).detail("answer", item_id)}

@router.post("/import")
def import_history(request: Request, body: dict[str, Any] | None = None):
    body = body or {}
    return {"ok": True, "data": svc(request).import_history(confirm=body.get("confirm") is True, limit=_body_limit(body))}

@router.post("/dry-run")
def dry_run(request: Request, body: dict[str, Any] | None = None):
    body = body or {}
    return {"ok": True, "data": svc(request).preview(limit=_body_limit(body))}

@router.post("/answers/{item_id}/refresh/preview")
def refresh_preview(item_id: int, request: Request): return {"ok": True, "data": svc(request).refresh_preview(item_id)}

@router.post("/answers/{item_id}/refresh/confirm")
def refresh_confirm(item_id: int, request: Request, body: dict[str, Any] | None = None):
    return {"ok": True, "data": svc(request).refresh_confirm(item_id, (body or {}).get("confirm") is True)}
