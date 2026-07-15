from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from content_hub.errors import ValidationAppError
from content_hub.features.wechat.service import WechatService

router = APIRouter(prefix="/api/v1/wechat", tags=["wechat"])


def _service(request: Request) -> WechatService:
    return WechatService(request.app.state.settings)


@router.get("/bootstrap")
def bootstrap(request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).bootstrap()}


@router.get("/keywords/{keyword_id}")
def keyword(keyword_id: str, request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).keyword(keyword_id)}


@router.get("/articles/{article_id}")
def article(article_id: str, request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).article(article_id)}


@router.get("/articles/{article_id}/content")
def article_content(article_id: str, request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).article_content(article_id)}


@router.post("/keywords/{keyword_id}/refresh")
def refresh(keyword_id: str, request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = body or {}
    result = _service(request).refresh(
        keyword_id,
        payload.get("confirm") is True,
        idempotency_key=str(
            payload.get("idempotency_key")
            or request.headers.get("X-Idempotency-Key")
            or ""
        ),
    )
    status = int(result.pop("http_status", 200))
    response = {"ok": status < 400, "data": result}
    return JSONResponse(status_code=status, content=response) if status != 200 else response


@router.post("/import")
def import_history(request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = body or {}
    limit = payload.get("limit")
    if limit is not None:
        try: limit = max(1, min(100000, int(limit)))
        except (TypeError, ValueError) as exc: raise ValidationAppError("limit 必须是正整数。") from exc
    return {"ok": True, "data": _service(request).import_history(dry_run=payload.get("dry_run") is True, limit=limit)}


@router.get("/refresh-status/{job_id}")
def refresh_status(job_id: str, request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).refresh_status(job_id)}
