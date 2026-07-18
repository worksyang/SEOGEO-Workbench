"""小红书搜索级影子刷新 Provider 契约。

只允许关键词搜索；不包含 TikHub、笔记详情、博主详情、封面、评论或媒体接口。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import urllib.error
import urllib.request
import urllib.parse
from typing import Any, Protocol


class XhsSearchProvider(Protocol):
    kind: str

    def search(self, *, keyword_id: str, keyword: str) -> dict[str, Any]:
        """执行一次关键词搜索并返回原始搜索响应。"""


class XhsSearchProviderError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str, status: int | None = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.status = status


@dataclass(frozen=True, slots=True)
class DryRunXhsSearchProvider:
    kind: str = "dry-run"

    def search(self, *, keyword_id: str, keyword: str) -> dict[str, Any]:
        return {
            "status": "dry-run",
            "keyword_id": keyword_id,
            "keyword": keyword,
            "results": [],
            "upstream_called": False,
            "scope": {
                "search_only": True,
                "note_detail": False,
                "account_detail": False,
                "cover": False,
                "comments": False,
                "media": False,
            },
        }


def _canonical_note_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in {
        "www.xiaohongshu.com", "xhslink.com",
    }:
        return None
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                if k.lower() not in {"xsec_token", "xsec_source", "token", "access_token"}),
        doseq=True,
    )
    return urlunsplit(("https", (parsed.hostname or "").lower(), parsed.path or "/", query, ""))


def normalize_search_notes_response(payload: dict[str, Any]) -> dict[str, Any]:
    """转换 TikHub ``search_notes`` 原始 envelope，只读取 ``data.data.items``。

    转换结果不包含详情/博主/封面/评论/媒体字段，也不修改原始 envelope。
    """
    outer = payload.get("data")
    inner = outer.get("data") if isinstance(outer, dict) else None
    items = inner.get("items") if isinstance(inner, dict) else None
    if not isinstance(items, list):
        return {"hits": [], "raw_sha256": hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(), "envelope_valid": False}
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        # TikHub search_notes 返回的 item 常见形态是
        # {model_type: "note", note: {id, title, user, ...}}。
        # 这里只展开搜索响应已有字段，不触发任何详情请求。
        note = item.get("note") if isinstance(item.get("note"), dict) else item
        user = note.get("user") if isinstance(note.get("user"), dict) else {}
        note_id = str(note.get("id") or note.get("note_id") or note.get("noteId") or item.get("id") or "").strip()
        url = _canonical_note_url(
            note.get("url") or note.get("url_raw") or note.get("link")
            or (f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else None)
        )
        identity = f"id:{note_id}" if note_id else f"url:{url}" if url else ""
        if not identity or identity in seen:
            continue
        seen.add(identity)
        hits.append({
            "rank": int(item.get("rank") or note.get("rank") or index),
            "note_id": note_id or None,
            "title_raw": str(note.get("title") or note.get("title_raw") or "").strip() or None,
            "url_raw": url,
            "canonical_url": url,
            "creator_id": str(
                user.get("userid")
                or user.get("user_id")
                or user.get("userId")
                or note.get("user_id")
                or note.get("userid")
                or ""
            ).strip() or None,
            "creator_name_raw": str(user.get("nickname") or note.get("author") or note.get("creator_name_raw") or "").strip() or None,
            "published_at": note.get("timestamp"),
            "liked_count": note.get("liked_count"),
            "collected_count": note.get("collected_count"),
            "comment_count": note.get("comments_count"),
            "shared_count": note.get("shared_count"),
            "images": note.get("images"),
            "cover": note.get("cover"),
            "payload": note,
        })
    return {
        "hits": hits,
        "raw_sha256": hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "envelope_valid": True,
        "raw_count": len(items),
        "deduplicated_count": len(items) - len(hits),
    }


class TikHubSearchProvider:
    """显式启用时才调用 TikHub 的 search_notes Provider。

    该类只发起 ``search_notes`` 请求；token 从环境读取且永不进入日志或返回值。
    """

    kind = "tikhub-search_notes"
    is_live = True

    def __init__(self, *, token: str, endpoint: str, timeout_seconds: float = 20.0) -> None:
        if not token.strip():
            raise ValueError("TikHub token 未配置。")
        self._token = token.strip()
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "TikHubSearchProvider":
        token = os.getenv("HUB_XHS_TIKHUB_TOKEN", "").strip()
        endpoint = os.getenv("HUB_XHS_TIKHUB_SEARCH_NOTES_URL", "").strip()
        if not token or not endpoint:
            raise ValueError("未配置 HUB_XHS_TIKHUB_TOKEN 或 HUB_XHS_TIKHUB_SEARCH_NOTES_URL。")
        parsed = urllib.parse.urlsplit(endpoint)
        if not parsed.path.rstrip("/").endswith("/api/v1/xiaohongshu/app_v2/search_notes"):
            endpoint = endpoint.rstrip("/") + "/api/v1/xiaohongshu/app_v2/search_notes"
        return cls(token=token, endpoint=endpoint)

    def search(self, *, keyword_id: str, keyword: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({
            "keyword": keyword,
            "page": 1,
            "sort_type": "general",
            "source": "explore_feed",
        })
        request = urllib.request.Request(
            f"{self.endpoint}?{query}" if "?" not in self.endpoint else f"{self.endpoint}&{query}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "xhs-keyword-monitor/2.0 (TikHub)",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            raise XhsSearchProviderError(
                f"TikHub search_notes HTTP {exc.code}",
                reason_code="tikhub_http_error", status=int(exc.code),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise XhsSearchProviderError(
                "TikHub search_notes 网络请求失败",
                reason_code="tikhub_network_error",
            ) from exc
        if not 200 <= status < 300:
            raise XhsSearchProviderError(
                f"TikHub search_notes HTTP {status}",
                reason_code="tikhub_http_error", status=status,
            )
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise XhsSearchProviderError(
                "TikHub search_notes 返回无效 JSON",
                reason_code="tikhub_invalid_json", status=status,
            ) from exc
        if not isinstance(value, dict):
            raise XhsSearchProviderError(
                "TikHub search_notes 返回不是 JSON object",
                reason_code="tikhub_invalid_payload", status=status,
            )
        if value.get("code") not in (None, 0, 200, "0", "200"):
            raise XhsSearchProviderError(
                "TikHub search_notes 业务返回失败",
                reason_code="tikhub_business_error", status=status,
            )
        return value
