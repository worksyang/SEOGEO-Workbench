"""Wiki / 母文章库 路由。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from content_hub.db.connection import connect
from content_hub.db.writer_lock import writer_lock
from content_hub.errors import AppError
from content_hub.validation.timestamps import utc_now_iso
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


@router.get("/info")
def wiki_info(request: Request) -> dict:
    """返回 Wiki 当前根目录、健康状态与文件总数。

    工作台首页与 Wiki 侧栏从这里读根路径；侧栏不再依赖任何本机绝对路径硬编码。
    """
    settings = request.app.state.settings
    service = _service(request)
    root = service.import_root()
    if root is None:
        payload = {
            "ok": False,
            "data": {
                "status": "blocked",
                "reason": "没有可用的 Wiki 允许根",
                "root_path": "",
                "file_count": 0,
            },
        }
        return payload
    entries = service.collect()
    file_count = len(entries)
    with connect(settings, readonly=False) as connection:
        connection.execute(
            """
            INSERT INTO system_connections(
                system_key, display_name, base_url, status, last_checked_at,
                capabilities_json, details_json
            ) VALUES (?, ?, NULL, 'healthy', ?, ?, ?)
            ON CONFLICT(system_key) DO UPDATE SET
                display_name=excluded.display_name,
                base_url=NULL,
                status='healthy',
                last_checked_at=excluded.last_checked_at,
                capabilities_json=excluded.capabilities_json,
                details_json=excluded.details_json
            """,
            (
                "wiki",
                "Wiki / 母文章库",
                utc_now_iso(),
                json.dumps(
                    [
                        "read",
                        "search",
                        "edit",
                        "delete_markdown",
                        "bulk_delete_image",
                        "history_import",
                    ],
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "source_kind": "configured_markdown",
                        "root_path": str(root),
                        "file_count": file_count,
                        "direct_write": True,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        connection.commit()
    return {
        "ok": True,
        "data": {
            "status": "healthy",
            "root_path": str(root),
            "file_count": file_count,
            "direct_write": True,
        },
    }


@router.get("/search")
def search(request: Request, query: str = Query(""), limit: int = Query(50, ge=1, le=200)) -> dict:
    items = _service(request).search(query, limit=limit)
    return {"ok": True, "data": {"items": items, "total": len(items)}}


@router.get("/source")
def read_source(request: Request, source_ref: str = Query(..., min_length=1)) -> dict:
    detail = _service(request).read_source_ref(source_ref)
    if not detail:
        raise HTTPException(status_code=404, detail="母文章不存在")
    return {"ok": True, "data": detail}


@router.put("/source")
def save_source(request: Request, payload: dict) -> dict:
    source_ref = payload.get("source_ref")
    body = payload.get("body")
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise HTTPException(status_code=400, detail="source_ref 不能为空")
    if not isinstance(body, str) or not body.strip():
        raise HTTPException(status_code=400, detail="正文不能为空")
    base_version_id = payload.get("base_version_id")
    if base_version_id is not None and not isinstance(base_version_id, str):
        raise HTTPException(status_code=400, detail="base_version_id 必须是字符串")
    try:
        settings = request.app.state.settings
        with writer_lock(settings.lock_path):
            result = _service(request, readonly=False).save_source_ref(
                source_ref,
                body=body,
                operator=str(payload.get("operator") or "legacy-wiki-ui"),
                base_version_id=base_version_id,
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AppError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="母文章路径不安全或不可读取")
    return {"ok": True, "data": result}


@router.delete("/source")
def delete_source(request: Request, payload: dict) -> dict:
    source_ref = payload.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise HTTPException(status_code=400, detail="source_ref 不能为空")
    if payload.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="删除 Markdown 需要 confirm=true")
    base_version_id = payload.get("base_version_id")
    if base_version_id is not None and not isinstance(base_version_id, str):
        raise HTTPException(status_code=400, detail="base_version_id 必须是字符串")
    settings = request.app.state.settings
    try:
        with writer_lock(settings.lock_path):
            result = _service(request, readonly=False).delete_source_ref(
                source_ref,
                operator=str(payload.get("operator") or "legacy-wiki-ui"),
                base_version_id=base_version_id,
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AppError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="母文章路径不安全或不可读取")
    return {"ok": True, "data": result}


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
    base_version_id = payload.get("base_version_id")
    if base_version_id is not None and not isinstance(base_version_id, str):
        raise HTTPException(status_code=400, detail="base_version_id 必须是字符串")
    try:
        result = _service(request, readonly=False).save(
            content_id,
            body=body,
            operator=operator,
            base_version_id=base_version_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AppError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="母文章路径不安全或不可读取")
    return {"ok": True, "data": result}
