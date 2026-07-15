from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from content_hub.adapters.mp import MpSourceError
from content_hub.features.mp.service import MpService

router = APIRouter(prefix="/api/v1/mp", tags=["wechat-mp"])


def _service(request: Request) -> MpService:
    return MpService(request.app.state.settings)


def _call(fn: Callable[[], Any]):
    try:
        result = fn()
    except MpSourceError as exc:
        return JSONResponse(status_code=exc.status or 502, content={"ok": False, "error": {"code": "UPSTREAM_ERROR", "message": str(exc), "kind": exc.kind, "status": exc.status, "payload": exc.payload}})
    return {"ok": True, "data": result.payload if hasattr(result, "payload") else result}


@router.get("/bootstrap")
def bootstrap(request: Request): return {"ok": True, "data": _service(request).bootstrap()}


@router.get("/articles")
def articles(request: Request): return {"ok": True, "data": _service(request).articles()}


@router.get("/articles/{content_id}")
def article(content_id: str, request: Request): return {"ok": True, "data": _service(request).article(content_id)}


@router.post("/import")
def import_articles(request: Request, body: dict[str, Any] | None = None):
    payload = body or {}
    limit = payload.get("limit")
    if limit is not None:
        try: limit = max(1, min(100000, int(limit)))
        except (TypeError, ValueError):
            from content_hub.errors import ValidationAppError
            raise ValidationAppError("limit 必须是正整数。")
    return {"ok": True, "data": _service(request).import_history(dry_run=payload.get("dry_run") is True, limit=limit)}


@router.get("/accounts")
def accounts(request: Request): return _call(lambda: _service(request).accounts())


@router.get("/categories")
def categories(request: Request): return _call(lambda: _service(request).categories())


@router.patch("/accounts/{mp_id}")
def update_account(mp_id: str, request: Request, body: dict[str, Any] | None = None):
    payload = body or {}
    return _call(lambda: _service(request).update_flags(mp_id, payload.get("flags") if isinstance(payload.get("flags"), dict) else payload, payload.get("confirm") is True))


@router.get("/jobs")
def jobs(request: Request): return _call(lambda: _service(request).jobs())


@router.post("/jobs")
def create_job(request: Request, body: dict[str, Any] | None = None):
    payload = body or {}
    return _call(
        lambda: _service(request).create_job(
            payload,
            payload.get("confirm") is True,
            idempotency_key=str(
                payload.get("idempotency_key")
                or request.headers.get("X-Idempotency-Key")
                or ""
            ),
        )
    )


@router.get("/jobs/{job_id}")
def job(job_id: str, request: Request): return _call(lambda: _service(request).job(job_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request, body: dict[str, Any] | None = None):
    payload = body or {}
    return _call(lambda: _service(request).cancel_job(job_id, payload.get("confirm") is True))


@router.post("/auth/check")
def auth_check(request: Request): return _call(lambda: _service(request).auth_check())


@router.post("/auth/qrcode")
def auth_qrcode(request: Request): return _call(lambda: _service(request).auth_qrcode())


@router.get("/auth/qrcode/image/{qr_id}")
def auth_qrcode_image(qr_id: str, request: Request):
    try:
        result = _service(request).auth_qrcode_image(qr_id)
    except MpSourceError as exc:
        return JSONResponse(status_code=exc.status or 502, content={"ok": False, "error": {"code": "UPSTREAM_ERROR", "message": str(exc), "kind": exc.kind, "status": exc.status, "payload": exc.payload}})
    return Response(content=result.content, status_code=result.status, media_type=result.content_type, headers={"Content-Type": result.content_type})


@router.post("/auth/qrcode/finish")
def auth_qrcode_finish(request: Request, body: dict[str, Any] | None = None):
    return _call(lambda: _service(request).auth_qrcode_finish(body or {}))
