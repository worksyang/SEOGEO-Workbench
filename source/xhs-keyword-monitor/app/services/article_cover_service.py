"""article_cover_service — XHS 封面代理（小红书 CDN 公开可访问）。

XHS workCoverUrl 一般是 sns-img-hw.xhscdn.net 的公开图片，浏览器 fetch 会触发防盗链；
服务端代理可避免防盗链问题。代理仅做透传 + 5MB 限制。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import requests
from flask import current_app


XHS_IMAGE_HOSTS = (
    "xhscdn.net",
    "xhscdn.com",
    "xhs-img.com",
    "xiaohongshu.com",
    "rednotecdn.com",
    "sns-img",
)


def resolve_article_covers_payload(normalized_dir: Path, raw_items: list[dict]) -> dict[str, Any]:
    return {"items": raw_items or [], "method": "xhs_proxy_workCoverUrl"}


def fetch_cover_image_bytes(url: str) -> tuple[bytes, str]:
    """代理 XHS 公开图片（避免浏览器侧防盗链）。"""
    if not url:
        raise ValueError("url is required")
    if not any(host in url for host in XHS_IMAGE_HOSTS):
        raise ValueError(f"refused: not an XHS image host: {url[:60]}")
    # 5MB 限制
    resp = requests.get(
        url,
        timeout=8,
        headers={"User-Agent": "Mozilla/5.0 (XHS-Monitor)"},
        stream=True,
    )
    resp.raise_for_status()
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=8192):
        if chunk:
            buf.extend(chunk)
        if len(buf) > 5 * 1024 * 1024:
            break
    content_type = resp.headers.get("content-type") or "image/webp"
    return bytes(buf), content_type
