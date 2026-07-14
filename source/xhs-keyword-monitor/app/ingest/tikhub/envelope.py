"""TikHub 响应 → 标准 SnapshotEnvelope / ContentItem / CreatorItem。

**契约对齐**：与原 RedFox envelope 同名同义，但字段命名遵循 TikHub 原生：
- workLikedCount / workCollectedCount / workCommentsCount / workSharedCount
- accountUserid / accountNickname / accountImages / accountRedId
- workType: normal / video

供 monitor_keywords / monitor_accounts / entity_builder 复用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from urllib.parse import quote


def _first(*candidates) -> Any:
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, str) and not c.strip():
            continue
        return c
    return None


def _to_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_xhs_url(note_id: str, xsec_token: str = "") -> str:
    """原始小红书链接：explore/<note_id>?xsec_token=... 编码。"""
    if not note_id:
        return ""
    base = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        from urllib.parse import quote as _q
        return f"{base}?xsec_token={_q(xsec_token, safe='')}"
    return base


@dataclass
class ContentItem:
    """TikHub note → 标准 ContentItem。"""

    content_id: str
    rank: int
    title: str
    summary: str | None  # 搜索 desc（截断）
    url: str | None
    published_at: str | None  # ISO from timestamp
    creator_id: str | None
    creator_name: str | None
    work_type: str | None  # normal / video
    cover_url: str | None
    liked_count: int | None
    collected_count: int | None
    comment_count: int | None
    shared_count: int | None
    read_count: int | None  # XHS 不公开，留 None
    desc_full: str | None = None  # 详细接口返回的完整 desc
    xsec_token: str | None = None
    images_list: list = field(default_factory=list)
    platform_payload: dict = field(default_factory=dict)
    # 后续懒加载标记
    detail_loaded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_id": self.content_id,
            "rank": self.rank,
            "title": self.title,
            "summary": self.summary,
            "url": self.url,
            "published_at": self.published_at,
            "creator_id": self.creator_id,
            "creator_name": self.creator_name,
            "work_type": self.work_type,
            "cover_url": self.cover_url,
            "liked_count": self.liked_count,
            "collected_count": self.collected_count,
            "comment_count": self.comment_count,
            "shared_count": self.shared_count,
            "read_count": self.read_count,
            "desc_full": self.desc_full,
            "xsec_token": self.xsec_token,
            "images_list": self.images_list,
            "platform_payload": self.platform_payload,
            "detail_loaded": self.detail_loaded,
        }


@dataclass
class CreatorItem:
    creator_id: str
    name: str
    account_type: str | None
    avatar: str | None
    description: str | None
    fans: int | None
    total_works: int | None
    likes: int | None
    collects: int | None
    follows: int | None
    ip_location: str | None
    note_num_stat: dict = field(default_factory=dict)  # {posted, liked, collected}
    verify_info: str | None = None
    red_official_verify_type: str | None = None
    platform_payload: dict = field(default_factory=dict)
    detail_loaded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "creator_id": self.creator_id,
            "name": self.name,
            "account_type": self.account_type,
            "avatar": self.avatar,
            "description": self.description,
            "fans": self.fans,
            "total_works": self.total_works,
            "likes": self.likes,
            "collects": self.collects,
            "follows": self.follows,
            "ip_location": self.ip_location,
            "note_num_stat": self.note_num_stat,
            "verify_info": self.verify_info,
            "red_official_verify_type": self.red_official_verify_type,
            "platform_payload": self.platform_payload,
            "detail_loaded": self.detail_loaded,
        }


@dataclass
class SnapshotEnvelope:
    platform: str = "小红书"
    keyword: str = ""
    captured_at: str = ""
    status: str = "success"
    has_more: bool = False
    result_count: int = 0
    suggestions: list = field(default_factory=list)
    related_terms: list = field(default_factory=list)
    items: list = field(default_factory=list)  # list[ContentItem]
    raw: dict = field(default_factory=dict)
    error_message: str | None = None
    source_version: str = "tikhub_v1"
    # 原始响应的本地审计路径；只保存相对项目路径，不含鉴权信息。
    raw_file_path: str | None = None
    search_id: str = ""
    search_session_id: str = ""
    next_page: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "keyword": self.keyword,
            "captured_at": self.captured_at,
            "status": self.status,
            "has_more": self.has_more,
            "result_count": self.result_count,
            "items": [it.to_dict() if hasattr(it, "to_dict") else it for it in self.items],
            "error_message": self.error_message,
            "source_version": self.source_version,
            "raw_file_path": self.raw_file_path,
            "search_id": self.search_id,
            "search_session_id": self.search_session_id,
            "next_page": self.next_page,
        }


def _normalize_timestamp(value) -> str | None:
    """TikHub `timestamp` 是毫秒或秒级整数；返回 ISO 字符串。"""
    if value is None or value == "":
        return None
    try:
        s = str(value).strip()
        if not s:
            return None
        n = float(s)
        from datetime import datetime, timezone
        if n > 1e12:  # 毫秒
            n = n / 1000.0
        if n < 0:
            return None
        return datetime.fromtimestamp(n, tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _extract_images_list(notes_obj: dict) -> list:
    """TikHub 图文：images_list[]（每个含 url/url_size_large/original/width/height）。
    视频：用 video_info_v2 fallback.
    """
    out = []
    for img in (notes_obj.get("images_list") or []):
        if not isinstance(img, dict):
            continue
        out.append({
            "url": img.get("url"),
            "url_size_large": img.get("url_size_large") or img.get("url"),
            "original": img.get("original"),
            "width": img.get("width"),
            "height": img.get("height"),
        })
    # 视频 fallback
    if not out and "video_info_v2" in notes_obj:
        vi = notes_obj.get("video_info_v2") or {}
        if isinstance(vi, dict):
            cap = vi.get("video_url") or vi.get("url") or vi.get("master_url")
            if cap:
                out.append({"url": cap, "url_size_large": cap})
    return out


def _extract_cover(images_list: list) -> str | None:
    if not images_list:
        return None
    first = images_list[0]
    return first.get("url_size_large") or first.get("url")


def build_content_item_from_search(note_obj: dict, rank: int) -> ContentItem:
    """从 search_notes 的 item 构造 ContentItem。

    TikHub 业务结构：`items[].note`（model_type='note'）— 也兼容顶层 note。
    """
    note = note_obj.get("note") if isinstance(note_obj, dict) and "note" in note_obj else note_obj
    if not isinstance(note, dict):
        note = {}

    note_id = note.get("id") or note.get("note_id") or ""
    cover_url = None
    images = _extract_images_list(note)
    cover_url = _extract_cover(images)
    work_type_raw = note.get("type") or ""
    work_type = "video" if work_type_raw == "video" else "normal"

    user = note.get("user") or {}
    creator_id = _first(user.get("userid"), user.get("user_id"))
    creator_name = _first(user.get("nickname"))
    creator_images = user.get("images")
    avatar = None
    if isinstance(creator_images, dict):
        avatar = _first(creator_images.get("large"), creator_images.get("medium"),
                       creator_images.get("small"), creator_images.get("url"))
    elif isinstance(creator_images, str):
        avatar = creator_images
    if not avatar:
        avatar = _first(user.get("image"), user.get("imageb"))

    return ContentItem(
        content_id=f"xhs_tk_{note_id}",
        rank=rank,
        title=note.get("title") or "",
        summary=note.get("desc") or "",
        url=build_xhs_url(note_id, note.get("xsec_token") or ""),
        published_at=_normalize_timestamp(
            _first(note.get("timestamp"), note.get("time"), note.get("last_update_time"))
        ),
        creator_id=creator_id,
        creator_name=creator_name,
        work_type=work_type,
        cover_url=cover_url,
        liked_count=_to_int(_first(note.get("liked_count"), note.get("workLikedCount"))),
        collected_count=_to_int(_first(note.get("collected_count"), note.get("workCollectedCount"))),
        comment_count=_to_int(_first(note.get("comments_count"), note.get("workCommentsCount"))),
        shared_count=_to_int(_first(note.get("shared_count"), note.get("workSharedCount"))),
        read_count=None,  # XHS 不公开
        desc_full=None,
        xsec_token=note.get("xsec_token"),
        images_list=images,
        platform_payload={
            "source": "tikhub_search",
            "model_type": note_obj.get("model_type") if isinstance(note_obj, dict) else None,
            "creator_avatar": avatar,
            "creator_red_id": user.get("red_id"),
            "creator_verified": user.get("red_official_verified"),
            "creator_verify_type": user.get("red_official_verify_type"),
            "ip_location": note.get("ip_location"),
        },
        detail_loaded=False,
    )


def _extract_creator_avatar(images) -> str | None:
    if isinstance(images, dict):
        return _first(images.get("large"), images.get("medium"), images.get("small"),
                     images.get("url"))
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            return _first(first.get("url"), first.get("large"))
        if isinstance(first, str):
            return first
    return None


def build_creator_item(user_obj: dict) -> CreatorItem:
    """从 search_users / get_user_info 的 user 构造 CreatorItem。"""
    if not isinstance(user_obj, dict):
        user_obj = {}
    userid = _first(user_obj.get("userid"), user_obj.get("user_id"))
    interact = user_obj.get("interactions") or []
    verify_content = user_obj.get("red_official_verify_content")
    note_num_stat = user_obj.get("note_num_stat") or {}
    if not isinstance(note_num_stat, dict):
        note_num_stat = {}

    return CreatorItem(
        creator_id=str(userid or ""),
        name=user_obj.get("nickname") or "",
        account_type=None,
        avatar=_extract_creator_avatar(user_obj.get("images") or user_obj.get("imageb")),
        description=user_obj.get("desc"),
        fans=_to_int(_first(user_obj.get("fans"))),
        total_works=_to_int(_first(note_num_stat.get("posted"), user_obj.get("notes"))),
        likes=_to_int(_first(note_num_stat.get("liked"), user_obj.get("liked"))),
        collects=_to_int(_first(note_num_stat.get("collected"), user_obj.get("collected"))),
        follows=_to_int(user_obj.get("follows")),
        ip_location=user_obj.get("ip_location"),
        note_num_stat={
            "posted": _to_int(note_num_stat.get("posted")),
            "liked": _to_int(note_num_stat.get("liked")),
            "collected": _to_int(note_num_stat.get("collected")),
        },
        verify_info=str(verify_content) if verify_content else None,
        red_official_verify_type=str(user_obj.get("red_official_verify_type") or ""),
        platform_payload={
            "red_id": user_obj.get("red_id"),
            "red_official_verified": user_obj.get("red_official_verified"),
            "gender": user_obj.get("gender"),
        },
        detail_loaded=False,
    )
