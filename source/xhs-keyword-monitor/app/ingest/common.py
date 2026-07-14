"""实体构建共享工具 — ID 生成、时间归一、URL 规范化。"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# 与源项目保持一致：东八区；事实层只关心「snapshot 时间戳是什么时刻」
TZ = timezone(timedelta(hours=8))


def _short_hash(text: str, length: int = 8) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:length]


def kw_id(keyword_text: str) -> str:
    return f"kw_{_short_hash(str(keyword_text).strip())}"


def art_id(identity_key: str) -> str:
    return f"xhs_{_short_hash(str(identity_key).strip())}"


def snap_id(keyword_id: str, captured_at: datetime) -> str:
    return f"snap_{keyword_id}_{int(captured_at.timestamp() * 1000)}"


def acct_id(creator_key: str) -> str:
    return f"xhs_acct_{_short_hash(str(creator_key).strip())}"


def normalize_url(url: str | None) -> str | None:
    """小红书 workUrl 一般已是规范链接，仅做基础 trim。"""
    if not url:
        return None
    s = str(url).strip()
    return s or None


def is_placeholder_url(url: str | None) -> bool:
    if not url:
        return True
    s = str(url).strip().lower()
    return s in {"", "about:blank", "javascript:void(0)", "#"} or s.startswith("javascript:")


def article_identity_key(creator_name: str | None, title: str | None) -> str:
    return f"{(creator_name or '').strip()}::{(title or '').strip()}"


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.isoformat(timespec="seconds")


def parse_captured_at_iso(value: str | None) -> datetime:
    """把 ISO 字符串安全解析为带 TZ 的 datetime。"""
    if not value:
        return datetime.now(TZ)
    s = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            return dt
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt
    except ValueError:
        return datetime.now(TZ)


def parse_published_at(value: str | None) -> datetime | None:
    """RedFox workPublishTime 是 13 位毫秒或 ISO；统一解析。"""
    if value is None or value == "":
        return None
    s = str(value).strip()
    # 纯数字 → 毫秒
    if s.isdigit():
        try:
            ms = int(s)
            if len(s) > 10:
                ms = ms / 1000.0  # 毫秒 → 秒
            return datetime.fromtimestamp(ms, tz=TZ)
        except (ValueError, OSError):
            return None
    dt = parse_captured_at_iso(s)
    # 如果解析结果「看起来」是未来时间，直接丢弃
    if dt.year > 2099:
        return None
    return dt


def project_display_path(path: str | Path | None) -> str | None:
    """绝对路径 → 相对项目根，方便前端/审计。"""
    if path is None:
        return None
    p = Path(path)
    try:
        proj = Path(__file__).resolve().parent.parent.parent
        return str(p.relative_to(proj))
    except ValueError:
        return str(p)
