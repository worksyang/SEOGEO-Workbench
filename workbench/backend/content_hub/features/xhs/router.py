from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from content_hub.features.xhs.policy import frozen_payload
from content_hub.features.xhs.service import XhsService

router = APIRouter(prefix="/api/v1/xhs", tags=["xiaohongshu"])


def _service(request: Request) -> XhsService:
    return XhsService(
        request.app.state.settings,
        provider=getattr(request.app.state, "xhs_shadow_provider", None),
    )


@router.get("/bootstrap")
def bootstrap(request: Request, summary: bool = Query(default=False)) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).bootstrap(summary=summary)}


@router.get("/keywords/{keyword_id}")
def keyword(keyword_id: str, request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).keyword(keyword_id)}


@router.get("/accounts/{account_id}")
def account(account_id: str, request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).account(account_id)}


@router.get("/articles")
def articles(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).articles(limit)}


@router.get("/articles/{article_id}")
def article(article_id: str, request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).article(article_id)}


@router.post("/keywords/{keyword_id}/refresh", response_model=None)
def refresh(keyword_id: str, request: Request, body: dict[str, Any] | None = None) -> Any:
    del request, body
    return JSONResponse(
        status_code=409,
        content=frozen_payload(f"keyword_refresh:{keyword_id}"),
    )


@router.post("/keywords/{keyword_id}/shadow-refresh", response_model=None)
def shadow_refresh(keyword_id: str, request: Request, body: dict[str, Any] | None = None) -> Any:
    payload = body or {}
    result = _service(request).shadow_refresh(
        keyword_id,
        dry_run=payload.get("dry_run", True) is True,
        confirm=payload.get("confirm") is True,
        idempotency_key=str(payload.get("idempotency_key") or request.headers.get("X-Idempotency-Key") or ""),
        provider=getattr(request.app.state, "xhs_shadow_provider", None),
    )
    status = int(result.pop("http_status", 200))
    response = {"ok": status < 400, "data": result}
    return JSONResponse(status_code=status, content=response) if status != 200 else response


@router.get("/refresh-status/{job_id}", response_model=None)
def refresh_status(job_id: str, request: Request) -> Any:
    result = _service(request).refresh_status(job_id)
    status = int(result.pop("http_status", 200))
    payload = {"ok": status < 400, "data": result}
    return JSONResponse(status_code=status, content=payload) if status != 200 else payload


@router.get("/freeze-state")
def freeze_state(request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).frozen_state()}


@router.post("/import")
def import_history(request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
    del request, body
    return JSONResponse(
        status_code=409,
        content=frozen_payload("history_import"),
    )
