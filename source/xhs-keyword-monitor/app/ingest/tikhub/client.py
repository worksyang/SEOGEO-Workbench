"""TikHub HTTP 客户端。

按主控实测的成功契约：
- base URL: https://api.tikhub.io
- all endpoints under /api/v1/xiaohongshu/app_v2/...
- Headers: Authorization: Bearer <token>, User-Agent
- 业务状态: top.code (200) AND data.data.code (0) AND data.data.success (true)

只暴露 envelope dict（不暴露 token）。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from app.config import Config

LOG = logging.getLogger("tikhub.client")


class TikHubError(RuntimeError):
    """TikHub 调用错误（封装 HTTP / 业务 / JSON / token 缺失）。"""


@dataclass
class _BaseResponse:
    code: int | None
    inner_code: int | None
    success: bool
    msg: str
    raw: dict[str, Any]


class TikHubClient:
    """轻量 TikHub 客户端：Bearer auth + 指数退避 + 限速。

    - 401/403：token 无效 → 直接抛错，不重试
    - 429/5xx：指数退避，最多重试 max_retries 次
    - 业务层：top.code != 200 或 inner.code != 0 或 inner.success != true → 抛错
    """

    def __init__(self, base_url: str | None = None, api_token: str | None = None,
                 timeout: int | None = None, max_retries: int | None = None,
                 inter_request_delay: float | None = None) -> None:
        self.base_url = (base_url or Config.TIKHUB_BASE_URL).rstrip("/")
        self.api_token = (api_token or Config.TIKHUB_API_TOKEN or "").strip()
        if not self.api_token:
            raise TikHubError(
                "TIKHUB_API_TOKEN 未设置；请通过环境变量或 .env 提供。"
            )
        self.timeout = int(timeout or Config.TIKHUB_TIMEOUT)
        self.max_retries = int(max_retries or Config.TIKHUB_MAX_RETRIES)
        self.inter_request_delay = float(inter_request_delay or Config.TIKHUB_INTER_REQUEST_DELAY)
        self._last_call_ts = 0.0

    # ── 速率限制 ──
    def _throttle(self) -> None:
        elapsed = time.time() - self._last_call_ts
        if elapsed < self.inter_request_delay:
            time.sleep(self.inter_request_delay - elapsed)
        self._last_call_ts = time.time()

    # ── 核心 GET ──
    def get(self, path: str, params: dict[str, Any] | None = None,
            headers_extra: dict[str, str] | None = None) -> _BaseResponse:
        url = self.base_url + path
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "User-Agent": "xhs-keyword-monitor/2.0 (TikHub)",
            "Accept": "application/json",
        }
        if headers_extra:
            headers.update(headers_extra)

        last_exc: Exception | None = None
        backoff = 1.0
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = requests.get(url, headers=headers, params=params or {},
                                     timeout=self.timeout)
            except requests.RequestException as e:
                last_exc = e
                if attempt < self.max_retries:
                    LOG.warning("TikHub %s attempt %d failed: %s; retry in %.1fs",
                                path, attempt + 1, e, backoff)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise TikHubError(f"TikHub 网络错误 {path}: {e}") from e

            status = resp.status_code
            if status in (401, 403):
                raise TikHubError(
                    f"TikHub 鉴权失败 ({status}); check TIKHUB_API_TOKEN 是否有效。"
                )
            if status == 429 or status >= 500:
                last_exc = TikHubError(f"upstream {status}")
                if attempt < self.max_retries:
                    LOG.warning("TikHub %s status %d; retry in %.1fs",
                                path, status, backoff)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise TikHubError(f"TikHub upstream failure ({status}) after retries") from None
            # 非 2xx/5xx 之外 → 走业务层判断
            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                raise TikHubError(
                    f"TikHub 返回非 JSON (HTTP {status}): {resp.text[:200]}"
                ) from e

            return self._parse_business(path, data, status)

        # 走到这里说明重试次数耗尽
        raise TikHubError(f"TikHub {path} 重试耗尽 ({last_exc})")

    def _parse_business(self, path: str, data: dict[str, Any], status: int) -> _BaseResponse:
        # top-level code（HTTP/网关层）
        top_code = data.get("code")
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        inner_code = inner.get("code") if isinstance(inner, dict) else None
        inner_success = inner.get("success") if isinstance(inner, dict) else None
        msg = (
            (data.get("message_zh") if isinstance(data, dict) else None)
            or (inner.get("msg") if isinstance(inner, dict) else None)
            or ""
        )

        if status != 200 or top_code != 200 or inner_code != 0 or inner_success is False:
            raise TikHubError(
                f"TikHub 业务错误: http={status} top_code={top_code} "
                f"inner_code={inner_code} success={inner_success} msg={msg[:100]}"
            )

        return _BaseResponse(
            code=top_code, inner_code=inner_code, success=True,
            msg=msg, raw=data,
        )


# ── 进程级辅助（与 RedFox 客户端对齐） ────────────────────────
_default_client: TikHubClient | None = None


def _client() -> TikHubClient:
    global _default_client
    if _default_client is None:
        _default_client = TikHubClient()
    return _default_client


def _call(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return _client().get(path, params=params).raw


# ── 6 个主控实测可用的端点 ──────────────────────────────────────────────
def search_xhs_notes(
    keyword: str,
    page: int = 1,
    sort_type: str = "general",
    note_type: str = "",
    time_filter: str = "",
    search_id: str = "",
    search_session_id: str = "",
    source: str = "explore_feed",
    ai_mode: int = 0,
) -> dict[str, Any]:
    """搜索笔记。sort_type ∈ {general, time_descending, popularity, comment, collect}"""
    params = {
        "keyword": keyword,
        "page": int(page),
        "sort_type": sort_type,
        "source": source,
    }
    if note_type:
        params["note_type"] = note_type
    if time_filter:
        params["time_filter"] = time_filter
    if search_id:
        params["search_id"] = search_id
    if search_session_id:
        params["search_session_id"] = search_session_id
    if ai_mode:
        params["ai_mode"] = int(ai_mode)
    return _call("/api/v1/xiaohongshu/app_v2/search_notes", params)


def search_xhs_users(
    keyword: str,
    page: int = 1,
    search_id: str = "",
    source: str = "explore_feed",
) -> dict[str, Any]:
    params = {
        "keyword": keyword,
        "page": int(page),
        "source": source,
    }
    if search_id:
        params["search_id"] = search_id
    return _call("/api/v1/xiaohongshu/app_v2/search_users", params)


def get_image_note_detail(note_id: str, share_text: str = "") -> dict[str, Any]:
    return _call(
        "/api/v1/xiaohongshu/app_v2/get_image_note_detail",
        {"note_id": note_id, "share_text": share_text},
    )


def get_video_note_detail(note_id: str, share_text: str = "") -> dict[str, Any]:
    return _call(
        "/api/v1/xiaohongshu/app_v2/get_video_note_detail",
        {"note_id": note_id, "share_text": share_text},
    )


def get_user_info(user_id: str, share_text: str = "") -> dict[str, Any]:
    return _call(
        "/api/v1/xiaohongshu/app_v2/get_user_info",
        {"user_id": user_id, "share_text": share_text},
    )


def get_user_posted_notes(user_id: str, share_text: str = "", cursor: str = "") -> dict[str, Any]:
    params = {"user_id": user_id, "share_text": share_text}
    if cursor:
        params["cursor"] = cursor
    return _call(
        "/api/v1/xiaohongshu/app_v2/get_user_posted_notes",
        params,
    )
