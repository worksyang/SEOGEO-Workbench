"""RedFox HTTP 客户端。

只封装三个端点（searchArticle / searchUser / queryAccountDetail），
对所有响应保留 raw payload，方便事实层审计。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests


LOG = logging.getLogger(__name__)


class RedFoxError(RuntimeError):
    """RedFox 调用错误（含 4xx/5xx/网络/JSON 解析）。"""


@dataclass
class _BaseResponse:
    code: int | None
    msg: str
    raw: dict[str, Any]


def _post(base_url: str, api_key: str, path: str, payload: dict[str, Any], timeout: int = 60) -> _BaseResponse:
    if not api_key:
        raise RedFoxError("REDFOX_API_KEY 未设置；请在 .env 或环境变量里配置。")
    url = base_url.rstrip("/") + path
    headers = {
        "Content-Type": "application/json",
        "REDFOX_API_KEY": api_key,
        "User-Agent": "xhs-keyword-monitor/1.0",
    }
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            raw_text = resp.text
            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                raise RedFoxError(
                    f"RedFox 返回非 JSON (HTTP {resp.status_code}): {raw_text[:200]}"
                ) from e
            return _BaseResponse(code=data.get("code"), msg=data.get("msg", ""), raw=data)
        except requests.RequestException as e:
            last_exc = e
            if attempt == 2:
                raise RedFoxError(f"RedFox 网络错误 {path}: {e}") from e
            time.sleep(0.5 + attempt * 0.5)
    raise RedFoxError(f"RedFox 重试耗尽: {last_exc}")


class RedFoxClient:
    """轻量 RedFox 客户端，便于在 services / scripts 复用。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> None:
        from app.config import Config

        self.base_url = (base_url or Config.REDFOX_BASE_URL).rstrip("/")
        self.api_key = (api_key or Config.REDFOX_API_KEY or "").strip()
        self.timeout = int(timeout or Config.REDFOX_TIMEOUT)

    # ── 三个核心端点 ────────────────────────────────────────
    def search_article(
        self,
        keyword: str,
        offset: int = 0,
        sort_type: str = "default",
    ) -> _BaseResponse:
        return _post(
            self.base_url,
            self.api_key,
            "/story/api/xhsUser/searchArticle",
            {"keyword": keyword, "offset": offset, "sortType": sort_type},
            timeout=self.timeout,
        )

    def search_user(
        self,
        keyword: str,
        offset: int = 0,
        sort_type: str = "default",
    ) -> _BaseResponse:
        return _post(
            self.base_url,
            self.api_key,
            "/story/api/xhsUser/searchUser",
            {"keyword": keyword, "offset": offset, "sortType": sort_type},
            timeout=self.timeout,
        )

    def query_account_detail(
        self,
        account_id: str | None = None,
        user_id: str | None = None,
    ) -> _BaseResponse:
        payload: dict[str, Any] = {}
        if account_id:
            payload["accountId"] = account_id
        if user_id:
            payload["userId"] = user_id
        if not payload:
            raise RedFoxError("queryAccountDetail 至少需要 accountId 或 userId 之一")
        return _post(
            self.base_url,
            self.api_key,
            "/story/api/xhsUser/queryAccountDetail",
            payload,
            timeout=self.timeout,
        )


# ── 进程级辅助函数（避免到处 import 客户端） ─────────────────
_default_client: RedFoxClient | None = None


def _client() -> RedFoxClient:
    global _default_client
    if _default_client is None:
        _default_client = RedFoxClient()
    return _default_client


def search_xhs_article(keyword: str, offset: int = 0, sort_type: str = "default") -> dict[str, Any]:
    """直接返回原始响应体；失败抛 RedFoxError。"""
    return _client().search_article(keyword=keyword, offset=offset, sort_type=sort_type).raw


def search_xhs_user(keyword: str, offset: int = 0, sort_type: str = "default") -> dict[str, Any]:
    return _client().search_user(keyword=keyword, offset=offset, sort_type=sort_type).raw


def query_xhs_account_detail(account_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    return _client().query_account_detail(account_id=account_id, user_id=user_id).raw
