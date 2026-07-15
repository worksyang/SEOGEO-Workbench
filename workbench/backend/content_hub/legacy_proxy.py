from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import Body, Request
from fastapi.responses import JSONResponse, Response


# 只代理微信搜一搜原页面已经使用的接口；禁止把工作台变成任意 URL 代理。
_ALLOWED_PREFIXES = (
    "monitor-data/bootstrap",
    "monitor-data/keyword/",
    "monitor-data/account/",
    "keyword-manage",
    "article-content",
    "article-covers",
    "article-hit-detail",
    "articles",
    "keywords/",
    "refresh-all",
    "refresh-status/",
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
    target = _upstream_url(
        str(settings.wechat_source_url),
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
            timeout=float(settings.wechat_source_timeout_seconds),
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
                    "message": f"微信搜一搜旧服务暂时不可用：{exc}",
                },
            },
        )
