"""TikHub 数据源 provider（默认 XHS_DATA_PROVIDER=tikhub）。

模块：
- client.py        HTTP 客户端（Bearer auth / 退避 / 限速）
- parser.py        业务 JSON → 标准 envelope
- envelope.py      ContentItem / CreatorItem / SnapshotEnvelope
- detail_service.py 详情/博主懒加载 + 缓存

调用方统一通过：
    from app.ingest.tikhub import search_xhs_notes, get_xhs_note_detail, ...
"""
from app.ingest.tikhub.client import (
    TikHubClient,
    TikHubError,
    search_xhs_notes,
    get_user_info,
    get_user_posted_notes,
    search_xhs_users,
    get_image_note_detail,
    get_video_note_detail,
)
from app.ingest.tikhub.envelope import (
    ContentItem,
    CreatorItem,
    SnapshotEnvelope,
    build_content_item_from_search,
    build_creator_item,
)
from app.ingest.tikhub.parser import (
    extract_notes_from_search_response,
    extract_creators_from_search_users_response,
    extract_note_from_detail_response,
    extract_creator_from_user_info_response,
    extract_notes_from_user_posted_response,
)

__all__ = [
    "TikHubClient",
    "TikHubError",
    "search_xhs_notes", "search_xhs_users",
    "get_user_info", "get_user_posted_notes",
    "get_image_note_detail", "get_video_note_detail",
    "ContentItem", "CreatorItem", "SnapshotEnvelope",
    "build_content_item_from_search",
    "build_creator_item",
    "extract_notes_from_search_response",
    "extract_creators_from_search_users_response",
    "extract_note_from_detail_response",
    "extract_creator_from_user_info_response",
    "extract_notes_from_user_posted_response",
]
