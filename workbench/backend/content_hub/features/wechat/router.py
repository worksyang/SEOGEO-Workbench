from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from content_hub.errors import AppError, ValidationAppError
from content_hub.features.wechat.service import WechatService
from content_hub.repositories.wechat_state import (
    LEGACY_UNHANDLED_INTERNAL_ERROR,
    LEGACY_UNHANDLED_NOT_FOUND,
)
from content_hub.services.wechat_refresh import WechatRefreshService
from content_hub.services.wechat_state import StateCommandService

router = APIRouter(prefix="/api/v1/wechat", tags=["wechat"])
legacy_state_router = APIRouter(prefix="/api", tags=["wechat-legacy-state"])
_LEGACY_INTERNAL_ERROR_HTML = """<!doctype html>
<html lang=en>
<title>500 Internal Server Error</title>
<h1>Internal Server Error</h1>
<p>The server encountered an internal error and was unable to complete your request. Either the server is overloaded or there is an error in the application.</p>
"""


def _service(request: Request) -> WechatService:
    return WechatService(request.app.state.settings)


@router.get("/bootstrap", response_model=None)
def bootstrap(request: Request) -> Any:
    service = _service(request)
    cached = service.bootstrap_http_response()
    if cached is not None:
        return Response(cached, media_type="application/json")
    return {"ok": True, "data": service.bootstrap()}


@router.get("/keywords/{keyword_id}")
def keyword(keyword_id: str, request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).keyword(keyword_id)}


@router.get("/accounts/{account_id}")
def account(account_id: str, request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).account_activity(account_id)}


@router.get("/articles/{article_id}")
def article(article_id: str, request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).article(article_id)}


@router.get("/articles/{article_id}/content")
def article_content(article_id: str, request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).article_content(article_id)}


@router.post("/keywords/{keyword_id}/refresh")
def refresh(keyword_id: str, request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = body or {}
    if payload.get("confirm") is not True:
        raise ValidationAppError("刷新必须明确传入 confirm=true。")
    idempotency_key = str(
        payload.get("idempotency_key")
        or payload.get("idempotencyKey")
        or request.headers.get("Idempotency-Key")
        or request.headers.get("X-Idempotency-Key")
        or ""
    ).strip()
    if not idempotency_key:
        raise ValidationAppError("刷新写请求必须提供非空 Idempotency-Key。")
    result = WechatRefreshService(
        request.app.state.settings,
        provider=getattr(request.app.state, "wechat_refresh_provider", None),
        actor_id=request.headers.get("X-Actor-ID", "user"),
    ).refresh_one(
        keyword_id=keyword_id,
        request_keyword=str(payload.get("keyword") or ""),
        key=idempotency_key,
        request_id=request.headers.get("X-Request-ID"),
        confirm=True,
        semantic=True,
    )
    semantic_status = str(result.get("status") or "")
    if result.get("blocked") is True or semantic_status == "blocked":
        status = 409
    elif semantic_status == "failed":
        status = 500
    elif semantic_status in {"queued", "running"}:
        status = 202
    else:
        status = 200
    response = {"ok": status < 400, "data": result}
    return JSONResponse(status_code=status, content=response) if status != 200 else response


@router.post("/import")
def import_history(request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = body or {}
    limit = payload.get("limit")
    if limit is not None:
        try: limit = max(1, min(100000, int(limit)))
        except (TypeError, ValueError) as exc: raise ValidationAppError("limit 必须是正整数。") from exc
    dry_run = payload.get("dry_run") is True
    key = str(
        payload.get("idempotency_key")
        or payload.get("idempotencyKey")
        or request.headers.get("Idempotency-Key")
        or request.headers.get("X-Idempotency-Key")
        or ""
    ).strip()
    if not dry_run and limit is None:
        if payload.get("confirm") is not True:
            raise ValidationAppError("正式全量导入必须明确传入 confirm=true。")
        if not key:
            raise ValidationAppError("正式全量导入必须提供非空幂等键。")
    result = _service(request).import_history(
        dry_run=dry_run,
        limit=limit,
        confirm=payload.get("confirm") is True,
        idempotency_key=key,
    )
    if result.get("semantic_status") in {"partial_failed", "failed"}:
        return JSONResponse(
            status_code=409,
            content={"ok": False, "data": result},
        )
    return {"ok": True, "data": result}


@router.get("/refresh-status/{job_id}")
def refresh_status(job_id: str, request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _service(request).refresh_status(job_id)}


def _state_service(request: Request) -> StateCommandService:
    return StateCommandService(request.app.state.settings)


def _payload(request: Request, body: dict[str, Any] | None) -> dict[str, Any]:
    value = body or {}
    if not isinstance(value, dict):
        return {}
    return value


def _explicit_idempotency_key(request: Request, body: dict[str, Any]) -> str:
    for value in (
        body.get("idempotency_key"),
        body.get("idempotencyKey"),
        request.headers.get("Idempotency-Key"),
        request.headers.get("X-Idempotency-Key"),
    ):
        key = str(value or "").strip()
        if key:
            return key
    return ""


def _legacy_write(request: Request, body: dict[str, Any] | None, command_type: str, payload: dict[str, Any], operation):
    body = _payload(request, body)
    key = _explicit_idempotency_key(request, body)
    if not key:
        return JSONResponse(
            status_code=400,
            content={"error": "状态命令必须提供非空 idempotency_key。"},
        )
    try:
        result = _state_service(request).execute(
            command_type, {"command": payload}, operation, idempotency_key=str(key or ""),
            actor_id=request.headers.get("X-Actor-ID", "user"),
            request_id=request.headers.get("X-Request-ID"),
        )
        return result
    except AppError as exc:
        if exc.code == LEGACY_UNHANDLED_NOT_FOUND:
            return JSONResponse(status_code=404, content={"error": exc.message})
        if exc.code == LEGACY_UNHANDLED_INTERNAL_ERROR:
            return JSONResponse(status_code=500, content={"error": exc.message})
        status = 400 if exc.code == "VALIDATION_ERROR" else exc.status_code
        return JSONResponse(status_code=status, content={"error": exc.message})


def _validation(message: str):
    raise ValidationAppError(message)


def _legacy_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@legacy_state_router.post("/keywords/{keyword_id}/pin")
def pin_keyword(keyword_id: str, request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    keyword = str(body.get("keyword", "")).strip()
    return _legacy_write(request, body, "keyword.pin", {"keyword_id": keyword_id, "keyword": keyword, "pinned": True},
                         lambda repo: _validation("keyword is required") if not keyword else repo.set_flag(keyword_id, keyword, "pinned", True))


@legacy_state_router.post("/keywords/{keyword_id}/unpin")
def unpin_keyword(keyword_id: str, request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    keyword = str(body.get("keyword", "")).strip()
    return _legacy_write(request, body, "keyword.unpin", {"keyword_id": keyword_id, "keyword": keyword, "pinned": False},
                         lambda repo: _validation("keyword is required") if not keyword else repo.set_flag(keyword_id, keyword, "pinned", False))


@legacy_state_router.post("/keywords/{keyword_id}/topic")
def update_topic(keyword_id: str, request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    keyword = str(body.get("keyword", "")).strip()
    value = body.get("topic")
    value = str(value).strip() if value is not None else None
    return _legacy_write(request, body, "keyword.topic", {"keyword_id": keyword_id, "keyword": keyword, "topic": value},
                         lambda repo: _validation("keyword is required") if not keyword else repo.set_flag(keyword_id, keyword, "topic", value))


@legacy_state_router.post("/keywords/{keyword_id}/note")
def update_note(keyword_id: str, request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    keyword = str(body.get("keyword", "")).strip()
    value = str(body.get("note", "")).strip()
    return _legacy_write(request, body, "keyword.note", {"keyword_id": keyword_id, "keyword": keyword, "note": value},
                         lambda repo: _validation("keyword is required") if not keyword else repo.set_flag(keyword_id, keyword, "note", value))


@legacy_state_router.post("/keywords/{keyword_id}/bucket")
def update_bucket(keyword_id: str, request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    keyword = str(body.get("keyword", "")).strip()
    value = body.get("keyword_bucket")
    value = str(value).strip() if value is not None else None
    return _legacy_write(request, body, "keyword.bucket", {"keyword_id": keyword_id, "keyword": keyword, "keyword_bucket": value},
                         lambda repo: _validation("keyword is required") if not keyword else repo.set_flag(keyword_id, keyword, "keyword_bucket", value))


@legacy_state_router.post("/keyword-manage/groups")
def create_group(request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    return _legacy_write(request, body, "group.create", {"label": str(body.get("label", "")).strip()},
                         lambda repo: repo.create_group(str(body.get("label", "")).strip()))


@legacy_state_router.patch("/keyword-manage/groups/{group_id}")
def update_group(group_id: str, request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    order = body.get("order")
    try:
        order = int(order) if order is not None else None
    except (TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return _legacy_write(request, body, "group.update", {"group_id": group_id, "label": body.get("label"), "order": order},
                         lambda repo: repo.update_group(group_id, str(body["label"]).strip() if body.get("label") is not None else None, order))


@legacy_state_router.delete("/keyword-manage/groups/{group_id}")
def delete_group(group_id: str, request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    return _legacy_write(request, body, "group.delete", {"group_id": group_id},
                         lambda repo: repo.delete_group(group_id))


@legacy_state_router.post("/keyword-manage/keywords")
def create_keyword(request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    payload = {"group_id": str(body.get("group_id", "")).strip(), "keyword_text": str(body.get("keyword_text", "")).strip(), "note": str(body.get("note", "")).strip()}
    return _legacy_write(request, body, "keyword.create", payload,
                         lambda repo: _validation("group_id is required") if not payload["group_id"] else (
                             _validation("keyword_text is required") if not payload["keyword_text"] else
                             repo.create_keyword(payload["group_id"], payload["keyword_text"], payload["note"])
                         ))


@legacy_state_router.patch("/keyword-manage/keywords/{keyword_id}")
def update_keyword(keyword_id: str, request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    note = str(body["note"]) if body.get("note") is not None else None
    payload = {"keyword_id": keyword_id, "keyword_text": body.get("keyword_text"), "note": note, "group_id": body.get("group_id")}
    return _legacy_write(request, body, "keyword.update", payload,
                         lambda repo: repo.update_keyword(keyword_id, str(body["keyword_text"]).strip() if body.get("keyword_text") is not None else None, note, str(body["group_id"]).strip() if body.get("group_id") else None))


@legacy_state_router.delete("/keyword-manage/keywords/{keyword_id}")
def delete_keyword(keyword_id: str, request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    return _legacy_write(request, body, "keyword.archive", {"keyword_id": keyword_id},
                         lambda repo: repo.archive_keyword(keyword_id))


@legacy_state_router.patch("/keyword-manage/keywords/{keyword_id}/refresh-policy")
def refresh_policy(keyword_id: str, request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    days = body.get("refresh_frequency_days")
    try:
        days = int(days) if days is not None else None
    except (TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    payload = {"keyword_id": keyword_id, "refresh_frequency_days": days, "source": str(body.get("source") or "manual").strip().lower()}
    return _legacy_write(request, body, "keyword.refresh_policy", payload,
                         lambda repo: repo.set_policy(keyword_id, days, payload["source"]))


@legacy_state_router.patch("/keyword-manage/keywords/{keyword_id}/commercial-value")
def commercial_value(keyword_id: str, request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    try:
        score = int(body.get("score"))
    except (TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    payload = {"keyword_id": keyword_id, "score": score, "reason": str(body.get("reason") or "")}
    return _legacy_write(request, body, "keyword.commercial_value", payload,
                         lambda repo: repo.set_commercial(keyword_id, score, payload["reason"]))


@legacy_state_router.patch("/keyword-manage/keywords/{keyword_id}/auto-archive-lock")
def archive_lock(keyword_id: str, request: Request, body: dict[str, Any] | None = None):
    body = _payload(request, body)
    locked = _legacy_bool(body.get("locked"), False)
    payload = {"keyword_id": keyword_id, "locked": locked}
    return _legacy_write(request, body, "keyword.auto_archive_lock", payload,
                         lambda repo: repo.set_archive_lock(keyword_id, locked))
