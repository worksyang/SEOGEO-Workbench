"""TikHub 详情/用户懒加载 + 缓存。

接口：
- get_note_detail(note_id, force=False) → ContentItem（合并 search 已有 + detail 新增字段）
- get_creator_detail(user_id, force=False) → CreatorItem

缓存路径：
- 笔记：data/raw/tikhub/xhs/details/<note_id>.json
- 用户：data/raw/tikhub/xhs/users/<user_id>.json

文件 schema：原始抓取响应 + captured_at；envelope 由 detail parser 提取。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import current_app
from app.config import Config, require_provider_token
from app.ingest.tikhub import (
    TikHubError,
    extract_creator_from_user_info_response,
    extract_note_from_detail_response,
    get_image_note_detail,
    get_user_info,
    get_video_note_detail,
    build_content_item_from_search,
    build_creator_item,
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _details_root() -> Path:
    """data/raw/<provider>/xhs/details/"""
    return Config.RAW_DIR / "details"


def _users_root() -> Path:
    return Config.RAW_DIR / "users"


def _safe_read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _safe_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _note_path(note_id: str) -> Path:
    return _details_root() / f"{note_id}.json"


def _user_path(user_id: str) -> Path:
    return _users_root() / f"{user_id}.json"


# ── 笔记详情 ─────────────────────────────────────────────
def get_note_detail(
    note_id: str,
    note_type_hint: str = "",
    search_summary: dict | None = None,
    force: bool = False,
) -> dict | None:
    """懒加载笔记详情。

    1. 缓存命中 → 走 parser 返 dict（含 merged fields）
    2. 缓存 miss → 按 normal/video 调 TikHub，写缓存，parser 返 dict
    3. 失败 → 静默 None（前端可用 search_summary 兜底）
    """
    if not note_id:
        return None

    cache_path = _note_path(note_id)
    if cache_path.exists() and not force:
        raw = _safe_read(cache_path)
        if raw:
            item = extract_note_from_detail_response(raw, target_note_id=note_id)
            if item:
                return item.to_dict()
            # cache 文件异常 → 删除并重抓
            cache_path.unlink(missing_ok=True)

    # 缓存 miss → 远程
    try:
        require_provider_token("tikhub")
        if (note_type_hint or "").lower() == "video":
            raw = get_video_note_detail(note_id=note_id)
        else:
            # 默认走图文（image note），失败再走视频
            try:
                raw = get_image_note_detail(note_id=note_id)
            except TikHubError as e:
                # 一些视频笔记也会被图文端点名 → 失败时自动 fallback 到视频
                raw = get_video_note_detail(note_id=note_id)
                note_type_hint = "video"
    except TikHubError:
        return None

    # 落缓存
    try:
        _safe_write(cache_path, {**raw, "_cached_at": _now()})
    except OSError:
        pass

    item = extract_note_from_detail_response(raw, target_note_id=note_id)
    if item is None:
        return None
    return item.to_dict()


# ── 用户详情 ─────────────────────────────────────────────
def get_creator_detail(user_id: str, force: bool = False) -> dict | None:
    if not user_id:
        return None
    cache_path = _user_path(user_id)
    if cache_path.exists() and not force:
        raw = _safe_read(cache_path)
        if raw:
            item = extract_creator_from_user_info_response(raw)
            if item:
                return item.to_dict()
            cache_path.unlink(missing_ok=True)

    try:
        require_provider_token("tikhub")
        raw = get_user_info(user_id=user_id)
    except TikHubError:
        return None

    try:
        _safe_write(cache_path, {**raw, "_cached_at": _now()})
    except OSError:
        pass

    item = extract_creator_from_user_info_response(raw)
    if item is None:
        return None
    return item.to_dict()


# ── 批量（enrich）─────────────────────────────────────────
def enrich_creators_bulk(
    user_ids: list[str],
    inter_delay: float = 0.3,
) -> tuple[int, int, list[str]]:
    """按 user_id 列表批量补全博主详情。

    返回 (success, fail, failures)
    - 写入缓存：data/raw/tikhub/xhs/users/<user_id>.json
    - 失败记录：failures 列表
    """
    success = 0
    fail = 0
    failures: list[str] = []
    for uid in user_ids:
        try:
            existing = _user_path(uid)
            if existing.exists():
                continue  # 断点续跑：跳过已缓存
            get_creator_detail(uid, force=True)
            success += 1
            time.sleep(inter_delay)
        except Exception as e:
            fail += 1
            failures.append(f"{uid}: {type(e).__name__}: {e}")
    return success, fail, failures
