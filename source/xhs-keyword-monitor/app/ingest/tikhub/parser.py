"""TikHub 原始 JSON envelope → 业务数据提取。

主控实测结构：
- top: {code, request_id, message_zh, cache_url, router, data}
- data: {code=0, success=true, msg, search_id, search_session_id, page, next_page, data}
- data.data.items[] (search_notes) → {model_type:'note', note:{...}}
- data.data.{notes,tags,has_more} (get_user_posted_notes)
- data.data.user (or top.data.data) (get_user_info)
- data.data.users[], data.data.filters (search_users)

详情：
- get_image_note_detail top.data.data 是 list，通常 [0].note_list[0] 是主笔记
- get_video_note_detail top.data.data 是 list；从中找 id==note_id
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.ingest.tikhub.envelope import (
    ContentItem,
    CreatorItem,
    SnapshotEnvelope,
    build_content_item_from_search,
    build_creator_item,
    _normalize_timestamp,
    _to_int,
    _first,
)


def _top_data(raw: dict) -> dict | None:
    """提取最里面的 data.data 业务对象。"""
    if not isinstance(raw, dict):
        return None
    inner = raw.get("data")
    return inner if isinstance(inner, dict) else None


def _is_business_ok(raw: dict) -> bool:
    if not isinstance(raw, dict):
        return False
    inner = _top_data(raw)
    if not isinstance(inner, dict):
        return False
    return (
        raw.get("code") in (200, "200")
        and inner.get("code") == 0
        and inner.get("success") is True
    )


def _envelope_meta(raw: dict) -> dict:
    """返回顶层 meta: search_id, search_session_id, page, next_page."""
    if not isinstance(raw, dict):
        return {}
    inner = _top_data(raw) or {}
    return {
        "search_id": inner.get("search_id"),
        "search_session_id": inner.get("search_session_id"),
        "page": inner.get("page"),
        "next_page": inner.get("next_page"),
    }


# ── 搜索笔记 ────────────────────────────────────────────────────────
def extract_notes_from_search_response(
    raw: dict,
    keyword: str,
    captured_at: str,
) -> SnapshotEnvelope:
    """search_notes → SnapshotEnvelope（含 ContentItem 列表）。"""
    items_inner: list = []
    inner = _top_data(raw) or {}
    items = ((inner.get("data") or {}).get("items") or [])
    for idx, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            continue
        model_type = it.get("model_type")
        # 仅取 note 类；其它（ad/user/related_query 等）一律丢弃
        if model_type and model_type != "note":
            continue
        items_inner.append(build_content_item_from_search(it, rank=len(items_inner) + 1))

    meta = _envelope_meta(raw)
    return SnapshotEnvelope(
        keyword=keyword,
        captured_at=captured_at,
        status="success" if items_inner else "empty",
        has_more=bool(meta.get("next_page")),
        result_count=len(items_inner),
        items=items_inner,
        raw={k: v for k, v in raw.items() if k != "data"},  # 不内嵌 whole data
        error_message=None,
        source_version="tikhub_app_v2",
        search_id=meta.get("search_id") or "",
        search_session_id=meta.get("search_session_id") or "",
        next_page=str(meta.get("next_page") or ""),
    )


# ── 搜索用户 ────────────────────────────────────────────────────────
def extract_creators_from_search_users_response(
    raw: dict,
    captured_at: str,
) -> list[CreatorItem]:
    out: list[CreatorItem] = []
    inner = _top_data(raw) or {}
    users = ((inner.get("data") or {}).get("users") or [])
    for u in users:
        if not isinstance(u, dict):
            continue
        out.append(build_creator_item(u))
    return out


# ── get_user_info ────────────────────────────────────────────────────
def extract_creator_from_user_info_response(raw: dict) -> CreatorItem | None:
    inner = _top_data(raw) or {}
    user = inner.get("data") if isinstance(inner.get("data"), dict) else None
    if not user:
        return None
    return build_creator_item(user)


# ── get_user_posted_notes ─────────────────────────────────────────────
def extract_notes_from_user_posted_response(
    raw: dict,
    captured_at: str,
) -> list[ContentItem]:
    inner = _top_data(raw) or {}
    inner_data = inner.get("data") if isinstance(inner.get("data"), dict) else {}
    notes = inner_data.get("notes") or []
    out: list[ContentItem] = []
    for idx, n in enumerate(notes, start=1):
        if not isinstance(n, dict):
            continue
        out.append(build_content_item_from_search(n, rank=idx))
    return out


# ── get_image_note_detail / get_video_note_detail ────────────────────
def extract_note_from_detail_response(
    raw: dict,
    target_note_id: str | None = None,
) -> ContentItem | None:
    """从详情接口返回的 list-of-notes 找出主笔记。

    主控实测结构：
    - top.data.data 是 list，常见 [0].note_list[0] 是笔记对象
    - 也可能有 top.data.data[0].note_list 或 top.data.data 直接是一个 dict
    """
    inner = _top_data(raw) or {}
    items = inner.get("data") if isinstance(inner.get("data"), list) else None
    if items is None and isinstance(inner.get("data"), dict):
        items = [inner["data"]]
    if items is None:
        items = []

    candidates: list[dict] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        # 形式 1：entry.note_list = [{...主笔记...}, ...]
        if "note_list" in entry and isinstance(entry["note_list"], list):
            for n in entry["note_list"]:
                if isinstance(n, dict):
                    candidates.append(n)
        # 形式 2：entry 本身就是笔记 dict
        elif entry.get("id") or entry.get("title") is not None:
            candidates.append(entry)

    # 找目标 id 优先；否则取第一个
    target = None
    if target_note_id:
        for n in candidates:
            if n.get("id") == target_note_id:
                target = n
                break
    if target is None and candidates:
        target = candidates[0]
    if not target:
        return None

    item = build_content_item_from_search(target, rank=1)
    # 详情接口补全 desc_full（保留完整原始 desc）
    item.desc_full = target.get("desc") or item.summary
    item.detail_loaded = True
    return item
