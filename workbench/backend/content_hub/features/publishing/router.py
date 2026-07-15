"""发布中心路由：账号 / 预览 / 草稿 / dry-run / 真发布。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from content_hub.db.connection import connect
from content_hub.db.writer_lock import writer_lock
from content_hub.services.publishing import PublishAccount, PublishingService
from content_hub.services.safety import scrub_public_payload
from content_hub.features.publishing.source_reader import read_legacy_accounts

router = APIRouter(prefix="/api/v1/publishing", tags=["publishing"])


def _accounts_from_settings(request: Request) -> list[PublishAccount]:
    settings = request.app.state.settings
    accounts_raw = getattr(settings, "publish_accounts", []) or []
    out: list[PublishAccount] = []
    for raw in accounts_raw:
        if not isinstance(raw, dict):
            continue
        out.append(
            PublishAccount(
                account_id=raw.get("account_id") or raw.get("id") or "",
                display_name=raw.get("display_name") or raw.get("name") or "",
                profile_dir=raw.get("profile_dir") or "",
                cookie_file=raw.get("cookie_file") or "",
                token_file=raw.get("token_file") or "",
                enabled=False,
                publishable=False,
                bridge_kind=getattr(settings, "publish_bridge_kind", "disabled"),
                bridge_status=getattr(settings, "publish_bridge_status", "unconfigured"),
            )
        )
    if not out:
        out.append(
            PublishAccount(
                account_id="demo",
                display_name="演示公众号（无 Cookie）",
                profile_dir="",
                cookie_file="",
                token_file="",
                enabled=False,
                publishable=False,
                bridge_kind="disabled",
                bridge_status="unconfigured",
            )
        )
    return out


def _load_sensitive_words(request: Request) -> list[str]:
    settings = request.app.state.settings
    candidates = [
        Path(settings.project_root) / "source" / "wechat-publish-system" / "Write" / "sensitive_words.txt",
    ]
    for path in candidates:
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                return [line.strip() for line in text.splitlines() if line.strip()]
            except OSError:
                continue
    return []


def _service(request: Request, connection) -> PublishingService:
    settings = request.app.state.settings
    legacy_config = read_legacy_accounts(getattr(settings, "publish_accounts_source", Path("")))
    return PublishingService(
        connection=connection,
        publish_root=Path(settings.asset_store_path) / "publish",
        sensitive_words=_load_sensitive_words(request),
        accounts=_accounts_from_settings(request),
        bridge_kind=getattr(settings, "publish_bridge_kind", "disabled"),
        bridge_status=getattr(settings, "publish_bridge_status", "unconfigured"),
        legacy_config=legacy_config,
    )


@router.get("/accounts")
def accounts(request: Request) -> dict:
    settings = request.app.state.settings
    with writer_lock(Path(settings.lock_path)):
        with connect(settings, readonly=False) as connection:
            service = _service(request, connection)
            service.sync_runtime_accounts()
            connection.commit()
            return {"ok": True, "data": {"items": service.list_accounts()}}


@router.get("/accounts/{account_id}")
def account_status(request: Request, account_id: str) -> dict:
    settings = request.app.state.settings
    with connect(settings, readonly=True) as connection:
        return {"ok": True, "data": _service(request, connection).status(account_id)}


@router.post("/preview")
def preview(request: Request, payload: dict) -> dict:
    settings = request.app.state.settings
    body = payload.get("body") or ""
    content_id = payload.get("content_id") or "preview"
    extra = payload.get("extra_sensitive_words") or []
    with connect(settings, readonly=True) as connection:
        result = _service(request, connection).preview(
            content_id=content_id, body=body, extra_sensitive_words=extra
        )
    return {
        "ok": True,
        "data": {
            "content_id": content_id,
            "html": result.html,
            "sensitive_matches": result.sensitive_matches,
            "warnings": result.warnings,
        },
    }


@router.post("/draft")
def draft(request: Request, payload: dict) -> dict:
    settings = request.app.state.settings
    account_id = payload.get("account_id")
    body = payload.get("body") or ""
    content_id = payload.get("content_id") or "draft"
    operator = payload.get("operator") or "user"
    if not account_id:
        raise HTTPException(status_code=400, detail="缺少 account_id")
    try:
        with writer_lock(Path(settings.lock_path)):
            with connect(settings, readonly=False) as connection:
                service = _service(request, connection)
                service.sync_runtime_accounts()
                result = service.save_draft(
                    account_id=account_id, content_id=content_id, body=body, operator=operator
                )
                connection.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "data": result}


@router.post("/dry-run")
def dry_run(request: Request, payload: dict) -> dict:
    settings = request.app.state.settings
    account_id = payload.get("account_id")
    body = payload.get("body") or ""
    content_id = payload.get("content_id") or "dry-run"
    if not account_id:
        raise HTTPException(status_code=400, detail="缺少 account_id")
    try:
        with writer_lock(Path(settings.lock_path)):
            with connect(settings, readonly=False) as connection:
                service = _service(request, connection)
                service.sync_runtime_accounts()
                result = service.dry_run(
                    account_id=account_id, content_id=content_id, body=body
                )
                connection.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "data": result}


@router.post("/queue")
def enqueue(request: Request, payload: dict) -> dict:
    settings = request.app.state.settings
    account_id = payload.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="缺少 account_id")
    try:
        with writer_lock(Path(settings.lock_path)):
            with connect(settings, readonly=False) as connection:
                service = _service(request, connection)
                service.sync_runtime_accounts()
                result = service.enqueue(
                    account_id=account_id,
                    content_id=payload.get("content_id") or "queue",
                    body=payload.get("body") or "",
                    operator=payload.get("operator") or "user",
                    queue_name=payload.get("queue_name") or "publishing-safe-queue",
                )
                connection.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "data": result}


@router.get("/queue")
def queue(request: Request, limit: int = 100) -> dict:
    settings = request.app.state.settings
    with connect(settings, readonly=True) as connection:
        return {"ok": True, "data": {"items": _service(request, connection).list_queue(limit=limit)}}


@router.post("/queue/{queue_item_id}/cancel")
def cancel_queue(request: Request, queue_item_id: str, payload: dict | None = None) -> dict:
    settings = request.app.state.settings
    try:
        with writer_lock(Path(settings.lock_path)):
            with connect(settings, readonly=False) as connection:
                result = _service(request, connection).set_queue_item_state(
                    queue_item_id=queue_item_id,
                    state="cancelled",
                    operator=(payload or {}).get("operator") or "user",
                )
                connection.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "data": result}


@router.post("/queue/{queue_item_id}/resume")
def resume_queue(request: Request, queue_item_id: str, payload: dict | None = None) -> dict:
    settings = request.app.state.settings
    try:
        with writer_lock(Path(settings.lock_path)):
            with connect(settings, readonly=False) as connection:
                result = _service(request, connection).set_queue_item_state(
                    queue_item_id=queue_item_id,
                    state="queued",
                    operator=(payload or {}).get("operator") or "user",
                )
                connection.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "data": result}


@router.post("/publish")
def publish(request: Request, payload: dict) -> dict:
    settings = request.app.state.settings
    account_id = payload.get("account_id")
    body = payload.get("body") or ""
    content_id = payload.get("content_id") or "publish"
    confirm = bool(payload.get("confirm", False))
    operator = payload.get("operator") or "user"
    if not account_id:
        raise HTTPException(status_code=400, detail="缺少 account_id")
    try:
        with writer_lock(Path(settings.lock_path)):
            with connect(settings, readonly=False) as connection:
                service = _service(request, connection)
                service.sync_runtime_accounts()
                result = service.publish(
                    account_id=account_id,
                    content_id=content_id,
                    body=body,
                    confirm=confirm,
                    operator=operator,
                )
                connection.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": result.get("ok", result["status"] not in {"blocked"}), "data": result}


@router.get("/attempts")
def attempts(request: Request, account_id: str | None = None, limit: int = 50) -> dict:
    settings = request.app.state.settings
    with connect(settings, readonly=True) as connection:
        params = [account_id, account_id, limit]
        cursor = connection.execute(
            "SELECT attempt_id, job_id, account_key, idempotency_key, mode, status, attempted_at, remote_receipt, error, payload_json "
            "FROM publish_attempts WHERE (? IS NULL OR account_key=?) ORDER BY attempted_at DESC LIMIT ?",
            params,
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
            except Exception:
                item["payload"] = {}
            # 历史记录也不把路径、profile 或任何潜在 secret 字段回显到 API。
            item["payload"] = scrub_public_payload(
                item["payload"],
                asset_root=Path(settings.asset_store_path),
            )
            item["error"] = scrub_public_payload(
                item.get("error"),
                asset_root=Path(settings.asset_store_path),
            )
            item["remote_receipt"] = scrub_public_payload(
                item.get("remote_receipt"),
                asset_root=Path(settings.asset_store_path),
            )
            rows.append(item)
    return {"ok": True, "data": {"items": rows}}
