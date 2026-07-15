from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import Body, Request
from fastapi.responses import FileResponse, JSONResponse, Response


# 只代理两个原版业务岛屿已经使用的接口；禁止把工作台变成任意 URL 代理。
_ALLOWED_PREFIXES = (
    "monitor-data",
    "monitor-data/bootstrap",
    "monitor-data/keyword/",
    "monitor-data/account/",
    "creator-detail",
    "keyword-manage",
    "article-content",
    "article-covers",
    "article-hit-detail",
    "article-cover-image",
    "note-detail",
    "articles",
    "account-aliases",
    "penalty-signals",
    "scheduler/",
    "keywords/",
    "refresh-all",
    "refresh-status/",
    # 公众号控制台的原始 API 契约；只允许已审计的资源族。
    "settings",
    "accounts",
    "accounts/",
    "categories",
    "categories/",
    "jobs",
    "jobs/",
    "runtime/",
    "auth/",
    "ai/",
)


def _allowed(path: str) -> bool:
    normalized = path.lstrip("/")
    return any(
        normalized == prefix or normalized.startswith(prefix)
        for prefix in _ALLOWED_PREFIXES
    )


def _upstream_url(base_url: str, path: str, query: str) -> str:
    encoded_path = urllib.parse.quote(
        path.lstrip("/"),
        safe="/:%@-._~!$&'()*+,;=",
    )
    url = f"{base_url.rstrip('/')}/api/{encoded_path}"
    return f"{url}?{query}" if query else url


async def proxy_legacy_wechat_api(
    path: str,
    request: Request,
    body: bytes = Body(default=b""),
) -> Response:
    """把原微信关键词页面的 API 原样接到旧服务，页面本身仍由工作台托管。"""
    if not _allowed(path):
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_ENDPOINT_NOT_ALLOWED",
                    "message": "该旧系统接口未登记到工作台代理白名单。",
                },
            },
        )

    settings: Any = request.app.state.settings
    referer = request.headers.get("referer", "")
    is_xhs = "/legacy/xhs/" in referer
    is_mp = "/legacy/mp/" in referer
    if is_xhs:
        source_url = settings.xhs_source_url
        timeout_seconds = settings.xhs_source_timeout_seconds
    elif is_mp:
        source_url = settings.mp_source_url
        timeout_seconds = settings.mp_source_timeout_seconds
    else:
        source_url = settings.wechat_source_url
        timeout_seconds = settings.wechat_source_timeout_seconds
    target = _upstream_url(
        str(source_url),
        path,
        str(request.url.query),
    )
    headers = {"Accept": request.headers.get("accept", "application/json")}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type
    upstream_request = urllib.request.Request(
        target,
        data=body if body else None,
        headers=headers,
        method=request.method,
    )

    try:
        with urllib.request.urlopen(
            upstream_request,
            timeout=float(timeout_seconds),
        ) as upstream:
            payload = upstream.read()
            status = int(upstream.status)
            response_type = upstream.headers.get_content_type() or "application/json"
            return Response(
                content=payload,
                status_code=status,
                media_type=response_type,
            )
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        response_type = exc.headers.get_content_type() if exc.headers else "application/json"
        return Response(
            content=payload,
            status_code=int(exc.code),
            media_type=response_type or "application/json",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_UPSTREAM_UNAVAILABLE",
                    "message": f"原系统服务暂时不可用：{exc}",
                },
            },
        )


async def proxy_legacy_static(path: str, request: Request) -> Response:
    """代理公众号旧控制台返回的有限静态资源，不提供通用文件代理。"""
    if path != "logo.svg":
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_STATIC_NOT_ALLOWED",
                    "message": "该旧系统静态资源未登记到工作台代理白名单。",
                },
            },
        )

    settings: Any = request.app.state.settings
    local_logo = settings.workbench_root / "frontend/public/legacy/mp/static/logo.svg"
    if local_logo.is_file():
        return FileResponse(local_logo, media_type="image/svg+xml")

    target = f"{str(settings.mp_source_url).rstrip('/')}/static/logo.svg"
    upstream_request = urllib.request.Request(
        target,
        headers={"Accept": request.headers.get("accept", "image/svg+xml")},
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            upstream_request,
            timeout=float(settings.mp_source_timeout_seconds),
        ) as upstream:
            payload = upstream.read()
            return Response(
                content=payload,
                status_code=int(upstream.status),
                media_type=upstream.headers.get_content_type() or "image/svg+xml",
            )
    except urllib.error.HTTPError as exc:
        return Response(
            content=exc.read(),
            status_code=int(exc.code),
            media_type=exc.headers.get_content_type() if exc.headers else "text/plain",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_UPSTREAM_UNAVAILABLE",
                    "message": f"原系统静态资源暂时不可用：{exc}",
                },
            },
        )
