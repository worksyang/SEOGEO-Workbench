"""RedFox 响应 → 标准 SnapshotEnvelope / ContentItem / CreatorItem。

源项目是微信 Markdown 解析；本项目是 RedFox JSON 直读。
按规范第廿五章：
  - 通用事实句：平台 P / 时间 T / 关键词 Q / 内容 C / 创作者 A / 排名 R / 标题 / 链接
  - 平台特有字段（小红书收藏 / 点赞 / 评论 / 分享 / 笔记类型 / 封面）放进 platform_payload
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _first(*candidates: Any) -> Any:
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, str) and not c.strip():
            continue
        return c
    return None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


@dataclass
class ContentItem:
    """单条笔记的标准事实结构。

    字段命名沿用规范第六章「article 实体」语义；XHS 平台字段放在 platform_payload。
    """

    content_id: str
    rank: int
    title: str
    summary: str | None
    url: str | None
    published_at: str | None
    creator_id: str | None
    creator_name: str | None
    work_type: str | None
    cover_url: str | None
    liked_count: int | None
    collected_count: int | None
    comment_count: int | None
    shared_count: int | None
    read_count: int | None  # XHS 不一定公开，保留
    platform_payload: dict[str, Any] = field(default_factory=dict)

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
            "platform_payload": self.platform_payload,
        }


@dataclass
class CreatorItem:
    """单个博主的标准事实结构。"""

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
    province: str | None
    city: str | None
    verify_info: str | None
    last_create_time: str | None
    platform_payload: dict[str, Any] = field(default_factory=dict)

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
            "province": self.province,
            "city": self.city,
            "verify_info": self.verify_info,
            "last_create_time": self.last_create_time,
            "platform_payload": self.platform_payload,
        }


@dataclass
class SnapshotEnvelope:
    """一次小红书搜索结果的标准快照信封。"""

    platform: str
    keyword: str
    captured_at: str
    status: str  # success / partial / failed
    has_more: bool
    result_count: int
    suggestions: list[str]
    related_terms: list[str]
    items: list[ContentItem]
    raw: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    source_version: str = "xhs_redfox_v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "keyword": self.keyword,
            "captured_at": self.captured_at,
            "status": self.status,
            "has_more": self.has_more,
            "result_count": self.result_count,
            "suggestions": list(self.suggestions),
            "related_terms": list(self.related_terms),
            "items": [item.to_dict() for item in self.items],
            "error_message": self.error_message,
            "source_version": self.source_version,
            # raw 不暴露给前端，仅供审计/重建
        }


# ── 解析函数 ────────────────────────────────────────────────
def build_content_item(raw: dict[str, Any], rank: int) -> ContentItem:
    """RedFox /searchArticle 单条结果 → ContentItem。"""
    work_id = _first(raw.get("workId"), raw.get("workIdStr"), raw.get("noteId")) or ""
    if not work_id:
        # 退化：用 URL 兜底
        work_id = (raw.get("workUrl") or raw.get("url") or f"xhs_unknown_{rank}")

    return ContentItem(
        content_id=f"xhs_work_{work_id}",
        rank=rank,
        title=str(raw.get("workTitle") or raw.get("title") or "").strip(),
        summary=raw.get("workDesc") or raw.get("desc"),
        url=raw.get("workUrl") or raw.get("url"),
        published_at=raw.get("workPublishTime") or raw.get("publishTime"),
        creator_id=raw.get("accountUserid") or raw.get("userId"),
        creator_name=raw.get("accountNickname") or raw.get("nickname"),
        work_type=raw.get("workType") or raw.get("noteType"),
        cover_url=_first(raw.get("workCoverUrl"), raw.get("coverUrl"), (raw.get("workCover") or {}).get("url")),
        liked_count=_to_int(_first(raw.get("workLikedCount"), raw.get("likedCount"))),
        collected_count=_to_int(_first(raw.get("workCollectedCount"), raw.get("collectedCount"))),
        comment_count=_to_int(_first(raw.get("workCommentsCount"), raw.get("commentsCount"))),
        shared_count=_to_int(_first(raw.get("workSharedCount"), raw.get("sharedCount"))),
        read_count=_to_int(_first(raw.get("workReadCount"), raw.get("viewCount"), raw.get("readCount"))),
        platform_payload={k: v for k, v in raw.items() if k not in {
            "workId", "workTitle", "workDesc", "workUrl", "workPublishTime",
            "accountUserid", "accountNickname", "workType", "workCover",
            "workLikedCount", "workCollectedCount", "workCommentsCount",
            "workSharedCount", "workReadCount",
        }},
    )


def build_creator_item(raw: dict[str, Any]) -> CreatorItem:
    user_id = _first(raw.get("userId"), raw.get("accountUserid"))
    account_id = _first(raw.get("accountId"), user_id)
    creator_id = user_id or account_id or raw.get("accountNickname") or ""
    return CreatorItem(
        creator_id=str(creator_id),
        name=str(raw.get("accountNickname") or raw.get("nickname") or "").strip(),
        account_type=raw.get("accountType") or raw.get("type"),
        avatar=_first(raw.get("accountHeadImg"), raw.get("avatar"), (raw.get("accountImage") or {}).get("url")),
        description=raw.get("accountDesc") or raw.get("description"),
        fans=_to_int(_first(raw.get("accountFans"), raw.get("fans"))),
        total_works=_to_int(_first(raw.get("accountTotalWorks"), raw.get("noteCount"), raw.get("worksCount"))),
        likes=_to_int(_first(raw.get("accountLikes"), raw.get("likes"))),
        collects=_to_int(_first(raw.get("accountCollectes"), raw.get("collectes"), raw.get("collectedCount"))),
        follows=_to_int(_first(raw.get("accountFollows"), raw.get("follows"))),
        ip_location=raw.get("ipLocation"),
        province=raw.get("province"),
        city=raw.get("city"),
        verify_info=raw.get("verifyInfo"),
        last_create_time=raw.get("lastCreateTime") or raw.get("accountUpdateTime"),
        platform_payload={k: v for k, v in raw.items() if k not in {
            "userId", "accountId", "accountNickname", "accountType",
            "accountHeadImg", "accountDesc", "accountFans", "accountTotalWorks",
            "accountLikes", "accountCollectes", "accountFollows",
            "ipLocation", "province", "city", "verifyInfo", "lastCreateTime",
        }},
    )
