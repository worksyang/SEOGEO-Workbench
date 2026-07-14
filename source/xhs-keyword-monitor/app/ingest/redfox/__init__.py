"""RedFox 小红书事实层适配器。

按规范文档第二十五章「平台适配器」建议：
  - searchArticle / searchUser / queryAccountDetail
  - 返回统一 SnapshotEnvelope（含平台、状态、原始 payload、解析版本）
"""
from __future__ import annotations

from app.ingest.redfox.client import (
    RedFoxClient,
    RedFoxError,
    search_xhs_article,
    search_xhs_user,
    query_xhs_account_detail,
)
from app.ingest.redfox.envelope import (
    SnapshotEnvelope,
    ContentItem,
    CreatorItem,
    build_content_item,
    build_creator_item,
)

__all__ = [
    "RedFoxClient",
    "RedFoxError",
    "search_xhs_article",
    "search_xhs_user",
    "query_xhs_account_detail",
    "SnapshotEnvelope",
    "ContentItem",
    "CreatorItem",
    "build_content_item",
    "build_creator_item",
]
