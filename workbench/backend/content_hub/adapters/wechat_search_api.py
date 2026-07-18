"""远端微信搜索服务 Provider 与响应转换器。"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable


class WechatSearchRemoteError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str = "remote_failed", payload: Any = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.payload = payload if isinstance(payload, dict) else {}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonicalize_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = hostname
    if parsed.username or parsed.password:
        return None
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
    )


def content_id_for_url(url: str) -> str:
    return "wechat_article_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def _first(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = item.get(name)
        if value not in (None, ""):
            return value
    return None


def _looks_like_hit(item: Any) -> bool:
    return isinstance(item, dict) and any(key in item for key in ("url", "url_raw", "link", "article_url", "title", "title_raw"))


def _extract_hits(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if _looks_like_hit(item)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "articles", "hits", "items", "data", "search_results"):
        value = payload.get(key)
        if isinstance(value, list):
            hits = [item for item in value if _looks_like_hit(item)]
            if hits:
                return hits
        if isinstance(value, dict):
            hits = _extract_hits(value)
            if hits:
                return hits
    for value in payload.values():
        if isinstance(value, (dict, list)):
            hits = _extract_hits(value)
            if hits:
                return hits
    return []


def _extract_result_count(payload: dict[str, Any], hits: list[dict[str, Any]]) -> int:
    for key in ("result_count", "total", "count", "total_count"):
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return len(hits)


def _remote_result_container(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _count_from_remote_data(container: dict[str, Any]) -> int | None:
    data = container.get("data")
    if not isinstance(data, list):
        return None
    counts = [item.get("article_count") for item in data if isinstance(item, dict)]
    counts = [int(value) for value in counts if isinstance(value, int) and value >= 0]
    return sum(counts) if counts else None


def _iter_markdown_documents(value: Any) -> list[str]:
    """从远端完成态递归收集内存中的 Markdown 文档。

    2.1.0 的不同部署版本把全文结果放在 ``result.markdown``、
    ``result.data[*].markdown`` 或其它嵌套字段；不能只读取一个固定层级。
    只收集带有搜索/文章结构标记的字符串，避免把普通错误消息当正文。
    """
    documents: list[str] = []
    if isinstance(value, str):
        if any(marker in value for marker in ("#### 文章列表", "#### 文章内容", "StartFragment")):
            documents.append(value)
        return documents
    if isinstance(value, dict):
        for nested in value.values():
            documents.extend(_iter_markdown_documents(nested))
    elif isinstance(value, list):
        for nested in value:
            documents.extend(_iter_markdown_documents(nested))
    return documents


def _titles_from_remote_markdown(container: dict[str, Any]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for markdown in _iter_markdown_documents(container):
        for hit in parse_search_markdown(markdown):
            key = (
                canonicalize_url(hit.get("url_raw")),
                int(hit.get("rank") or 0),
                hit.get("title_raw"),
            )
            if key in seen:
                continue
            seen.add(key)
            hits.append(hit)
    return hits


_RANK_RE = re.compile(r"(?m)^(\d+)\.\s+(.+?)\s*$")
_URL_RE = re.compile(r"https?://(?:mp\.)?weixin\.qq\.com/s/[^\s)>\u3001，。]+", re.I)


def parse_search_markdown(markdown: str) -> list[dict[str, Any]]:
    """解析全文模式搜索结果的文章列表，不保存搜索结果 Markdown。

    只在 ``#### 文章列表`` 之后解析排名块，避免把下拉词/相关搜索的编号误当文章。
    """
    text = str(markdown or "").replace("\x00", "")
    section = text.split("#### 文章列表", 1)[-1] if "#### 文章列表" in text else ""
    matches = list(_RANK_RE.finditer(section))
    hits: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        block = section[match.start(): matches[index + 1].start() if index + 1 < len(matches) else len(section)]
        title = match.group(2).strip()
        url_match = _URL_RE.search(block)
        creator_match = re.search(r"(?m)^\s*公众号[：:]\s*(.+?)\s*$", block)
        published_match = re.search(r"(?m)^\s*(?:时间|发布时间|发表时间)[：:]\s*(.+?)\s*$", block)
        intro_match = re.search(r"(?m)^\s*文章简介：(.+?)\s*$", block)
        hits.append({
            "rank": int(match.group(1)),
            "title_raw": title,
            "url_raw": url_match.group(0).rstrip(".,;!?，。；！？") if url_match else None,
            "creator_name_raw": creator_match.group(1).strip() if creator_match else None,
            "published_at": published_match.group(1).strip() if published_match else None,
            "summary_raw": intro_match.group(1).strip() if intro_match else None,
        })
    return hits


def parse_article_markdown(markdown: str) -> dict[str, str]:
    """从全文搜索 Markdown 的 ``文章内容`` 分段提取正文，结果只留在内存。

    搜索服务把文章列表和正文拼在同一个 Markdown 中；正文段落以
    ``##### 01. 标题`` 开始，随后有 canonical URL 和 ``StartFragment``。
    这里不写文件，也不把整份搜索 Markdown 作为结果保存。
    """
    text = str(markdown or "").replace("\x00", "")
    section = text.split("#### 文章内容", 1)[-1] if "#### 文章内容" in text else ""
    headings = list(re.finditer(r"(?m)^#####\s+\d+\.\s+(.+?)\s*$", section))
    articles: dict[str, str] = {}
    for index, heading in enumerate(headings):
        block = section[
            heading.end(): headings[index + 1].start() if index + 1 < len(headings) else len(section)
        ]
        url_match = _URL_RE.search(block)
        if not url_match:
            continue
        url = canonicalize_url(url_match.group(0))
        if not url:
            continue
        body = block[url_match.end():]
        if "StartFragment" in body:
            body = body.split("StartFragment", 1)[1]
        body = body.lstrip("\r\n ")
        body = body.strip()
        if body.endswith("EndFragment"):
            body = body[: -len("EndFragment")].rstrip()
        if body:
            articles[url] = body
    return articles


def _article_payloads(container: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("articles", "article_contents", "article_results", "contents"):
        value = container.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _attach_article_payloads(hits: list[dict[str, Any]], container: dict[str, Any]) -> None:
    payloads = _article_payloads(container)
    by_url = {
        canonicalize_url(_first(item, "url", "url_raw", "link", "article_url")): item
        for item in payloads
        if canonicalize_url(_first(item, "url", "url_raw", "link", "article_url"))
    }
    for hit in hits:
        article = by_url.get(hit.get("url_raw"))
        if not article:
            continue
        body = _first(article, "markdown", "content", "body", "text", "article_markdown")
        if isinstance(body, str) and body.strip():
            hit["markdown_body"] = body
    article_bodies: dict[str, str] = {}
    for markdown in _iter_markdown_documents(container):
        article_bodies.update(parse_article_markdown(markdown))
    if article_bodies:
        for hit in hits:
            url = canonicalize_url(hit.get("url_raw"))
            if url and url in article_bodies:
                hit["markdown_body"] = article_bodies[url]


def normalize_search_response(
    payload: dict[str, Any],
    *,
    keyword: str,
    request_id: str | None = None,
    source_ref: str,
) -> dict[str, Any]:
    """将远端完成态转换为 Hub 搜索刷新结果。

    URL 去重在 Provider 边界完成，后续服务层还会通过 contents 唯一索引做跨快照兜底。
    """
    container = _remote_result_container(payload)
    raw_hits = _extract_hits(container)
    if not raw_hits:
        raw_hits = _titles_from_remote_markdown(container)
    _attach_article_payloads(raw_hits, container)
    hits: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    invalid_hit_count = 0
    for ordinal, raw in enumerate(raw_hits, 1):
        url = canonicalize_url(_first(raw, "url", "url_raw", "link", "article_url"))
        if not url:
            invalid_hit_count += 1
            continue
        if url in seen:
            continue
        seen[url] = ordinal
        title = _first(raw, "title", "title_raw", "name")
        creator = _first(raw, "account", "creator", "author", "creator_name_raw")
        item = dict(raw)
        item.update({
            "rank": int(raw.get("rank") or raw.get("position") or ordinal),
            "title_raw": str(title) if title is not None else None,
            "url_raw": url,
            "creator_name_raw": str(creator) if creator is not None else None,
            "canonical_url": url,
            "content_id": content_id_for_url(url),
        })
        hits.append(item)
    hits.sort(key=lambda item: (int(item["rank"]), item["url_raw"]))
    explicit_count = _count_from_remote_data(container)
    return {
        "captured_at": str(payload.get("completed_at") or container.get("completed_at") or payload.get("timestamp") or _now()),
        "result_count": explicit_count if explicit_count is not None else _extract_result_count(container, raw_hits),
        "features": {
            "remote_status": payload.get("status"),
            "request_id": request_id,
            "invalid_hit_count": invalid_hit_count,
            "remote_markdown_discarded": bool(_iter_markdown_documents(container)),
            "remote_markdown_length": sum(len(markdown) for markdown in _iter_markdown_documents(container)),
            "raw_result_keys": sorted(payload.keys()),
        },
        "hits": hits,
        "metrics": [],
        "source_ref": source_ref,
        "remote_request_id": request_id,
        "markdown_count": 0,
        "invalid_hit_count": invalid_hit_count,
        # 不把远端全文搜索 Markdown 带入 Hub；正文在命中级别短暂存在，落盘后
        # 由刷新服务移除，搜索快照只保存结构化命中与正文资产引用。
        "raw_payload": {
            key: value for key, value in payload.items()
            if key not in {"markdown", "result"}
        },
    }


@dataclass(slots=True)
class RemoteWechatSearchProvider:
    base_url: str
    timeout_seconds: float = 20.0
    poll_interval_seconds: float = 2.0
    max_wait_seconds: float = 360.0
    top_k: int = 10
    kind: str = "wechat-search-api"
    opener: Callable[..., Any] = urllib.request.urlopen
    sleep: Callable[[float], None] = time.sleep

    def _request(self, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url.rstrip("/") + path, data=body, headers=headers, method=method)
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise WechatSearchRemoteError(
                f"远端微信搜索 HTTP {exc.code}",
                reason_code="remote_http",
                payload=_json_object(raw),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WechatSearchRemoteError(f"远端微信搜索网络失败：{exc}", reason_code="remote_unavailable") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatSearchRemoteError("远端微信搜索返回非法 JSON", reason_code="invalid_remote_payload") from exc
        if status >= 400 or not isinstance(value, dict):
            raise WechatSearchRemoteError("远端微信搜索返回结构无效", reason_code="invalid_remote_payload", payload=value)
        return value

    def _article(self, url: str) -> dict[str, Any]:
        return self._request("/article?" + urllib.parse.urlencode({"url": url}))

    def _complete_result(self, payload: dict[str, Any], *, request_id: str | None, keyword: str) -> dict[str, Any]:
        result = normalize_search_response(
            payload,
            keyword=keyword,
            request_id=request_id,
            source_ref=f"remote:wechat-search-api:{request_id}" if request_id else "remote:wechat-search-api",
        )
        # 某些服务版本只在搜索结果 Markdown 中返回 URL，正文由正式 /article 链路提供。
        for hit in result["hits"][: self.top_k]:
            if not str(hit.get("url_raw") or "").lower().startswith(
                ("https://mp.weixin.qq.com/s/", "http://mp.weixin.qq.com/s/")
            ):
                continue
            if hit.get("markdown_body"):
                continue
            try:
                article = self._article(hit["url_raw"])
            except WechatSearchRemoteError:
                continue
            body = _first(article, "content", "markdown", "body", "text")
            if isinstance(body, str) and body.strip():
                hit["markdown_body"] = body
        result["search_result_markdown_count"] = 0
        result["article_markdown_count"] = sum(1 for hit in result["hits"] if hit.get("markdown_body"))
        # 兼容旧刷新回执：markdown_count 表示搜索结果 Markdown 文件数，不能把
        # 正文资产计入该字段。
        result["markdown_count"] = 0
        result["article_fetch_count"] = result["article_markdown_count"]
        return result

    def _sync_search(self, *, keyword: str) -> dict[str, Any]:
        return self._request(
            "/search",
            method="POST",
            payload={
                "keywords": [keyword],
                "top_k": self.top_k,
                "async_mode": False,
                "fetch_depth": 1,
                "fetch_max_count": self.top_k,
            },
        )

    def fetch(self, *, keyword_id: str, keyword: str, incremental: bool = False, refresh_round: Any = None) -> dict[str, Any]:
        queued = self._request(
            "/search",
            method="POST",
            payload={
                "keywords": [keyword],
                "top_k": self.top_k,
                "async_mode": True,
                "fetch_depth": 1,
                "fetch_max_count": self.top_k,
            },
        )
        request_id = str(queued.get("request_id") or "").strip()
        if not request_id:
            if str(queued.get("status") or "").lower() in {"completed", "success", "succeeded"}:
                return self._complete_result(queued, request_id=None, keyword=keyword)
            raise WechatSearchRemoteError("远端搜索未返回 request_id", reason_code="invalid_remote_payload", payload=queued)
        deadline = time.monotonic() + self.max_wait_seconds
        latest = queued
        while time.monotonic() <= deadline:
            latest = self._request(f"/search/result/{urllib.parse.quote(request_id, safe='')}")
            status = str(latest.get("status") or "").lower()
            if status in {"completed", "success", "succeeded", "done"}:
                result = self._complete_result(latest, request_id=request_id, keyword=keyword)
                if result["hits"]:
                    return result
                # 某些远端版本异步完成态只给统计/摘要，正式同步模式会给
                # 同一请求的全文结果；只有在确实没有 URL 时才发起同步兜底。
                sync = self._sync_search(keyword=keyword)
                sync_id = str(sync.get("request_id") or "").strip()
                if sync_id:
                    sync_latest = sync
                    sync_deadline = time.monotonic() + self.max_wait_seconds
                    while time.monotonic() <= sync_deadline:
                        sync_latest = self._request(f"/search/result/{urllib.parse.quote(sync_id, safe='')}")
                        sync_status = str(sync_latest.get("status") or "").lower()
                        if sync_status in {"completed", "success", "succeeded", "done"}:
                            return self._complete_result(sync_latest, request_id=sync_id, keyword=keyword)
                        if sync_status in {"failed", "error", "cancelled", "canceled"}:
                            break
                        self.sleep(min(self.poll_interval_seconds, max(0.05, sync_deadline - time.monotonic())))
                return self._complete_result(sync, request_id=sync_id or None, keyword=keyword)
            if status in {"failed", "error"}:
                raise WechatSearchRemoteError(
                    str(latest.get("message") or latest.get("error") or "远端搜索失败"),
                    reason_code="remote_failed",
                    payload=latest,
                )
            if status in {"cancelled", "canceled"}:
                raise WechatSearchRemoteError("远端搜索已取消", reason_code="remote_cancelled", payload=latest)
            self.sleep(min(self.poll_interval_seconds, max(0.05, deadline - time.monotonic())))
        try:
            self._request(f"/cancel/{urllib.parse.quote(request_id, safe='')}", method="POST")
        except WechatSearchRemoteError:
            pass
        raise WechatSearchRemoteError(
            f"远端搜索超时：{request_id}",
            reason_code="remote_timeout",
            payload={"request_id": request_id, "last_status": latest.get("status")},
        )


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
