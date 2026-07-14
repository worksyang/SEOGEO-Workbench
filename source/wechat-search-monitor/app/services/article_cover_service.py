from __future__ import annotations

import json
from pathlib import Path
from threading import Lock

import requests

from app.ingest.common import is_placeholder_url
from app.ingest.content.cover_extractor import (
    REQUEST_HEADERS,
    extract_account_headimg_from_html,
    extract_cover_url_from_html,
    fetch_article_html,
)
from app.repositories.article_data_repo import ArticleDataRepository


MAX_BATCH_SIZE = 10

_ACCOUNTS_WRITE_LOCK = Lock()


def _articles_repo(normalized_dir: Path) -> ArticleDataRepository:
    return ArticleDataRepository(Path(normalized_dir) / "articles.json")


def _is_real_http_url(url: str) -> bool:
    text = str(url or "").strip()
    return text.startswith("http://") or text.startswith("https://")


def _update_account_headimg(normalized_dir: Path, account_id: str, headimg_url: str) -> None:
    accounts_path = Path(normalized_dir) / "accounts.json"
    with _ACCOUNTS_WRITE_LOCK:
        data = json.loads(accounts_path.read_text(encoding="utf-8"))
        for acct in data:
            if acct.get("account_id") == account_id:
                if not acct.get("headimg_url"):
                    acct["headimg_url"] = headimg_url
                break
        accounts_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_items(raw_items: object) -> list[dict]:
    if not isinstance(raw_items, list):
        raise ValueError("articles must be a list")
    if not raw_items:
        return []
    if len(raw_items) > MAX_BATCH_SIZE:
        raise ValueError(f"articles batch exceeds limit {MAX_BATCH_SIZE}")

    items: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("article item must be an object")
        article_id = str(raw.get("article_id", "")).strip()
        url = str(raw.get("url", "")).strip()
        if not article_id:
            raise ValueError("article_id is required")
        items.append({
            "article_id": article_id,
            "url": url,
        })
    return items


def resolve_article_covers_payload(normalized_dir: Path, raw_items: object) -> dict:
    repo = _articles_repo(normalized_dir)
    items = _normalize_items(raw_items)
    results: list[dict] = []

    accounts_path = Path(normalized_dir) / "accounts.json"
    try:
        accounts_data = json.loads(accounts_path.read_text(encoding="utf-8"))
        headimg_cache: dict[str, str | None] = {
            a["account_id"]: a.get("headimg_url") for a in accounts_data if a.get("account_id")
        }
    except Exception:
        headimg_cache = {}

    for item in items:
        article_id = item["article_id"]
        requested_url = item["url"]
        article = repo.find_by_id(article_id)
        if article is None:
            results.append({
                "article_id": article_id,
                "cover_url": None,
                "status": "missing_article",
            })
            continue

        cached_cover = str(article.get("cover_url") or "").strip()
        account_id = article.get("account_id", "")
        cached_headimg = headimg_cache.get(account_id)

        if cached_cover and cached_headimg is not None:
            results.append({
                "article_id": article_id,
                "cover_url": cached_cover,
                "status": "cached",
            })
            continue

        url = requested_url or str(article.get("raw_url", "")).strip()
        if not _is_real_http_url(url) or is_placeholder_url(url):
            results.append({
                "article_id": article_id,
                "cover_url": None,
                "status": "no_url",
            })
            continue

        try:
            html = fetch_article_html(url)
        except requests.HTTPError as exc:
            results.append({
                "article_id": article_id,
                "cover_url": None,
                "status": "http_error",
                "error": str(exc),
            })
            continue
        except requests.RequestException as exc:
            results.append({
                "article_id": article_id,
                "cover_url": None,
                "status": "request_error",
                "error": str(exc),
            })
            continue

        cover_url = cached_cover or extract_cover_url_from_html(html)
        if cover_url:
            repo.update_cover_url(article_id, cover_url)

        if cached_headimg is None and account_id:
            headimg_url = extract_account_headimg_from_html(html)
            if headimg_url:
                _update_account_headimg(normalized_dir, account_id, headimg_url)
                headimg_cache[account_id] = headimg_url

        results.append({
            "article_id": article_id,
            "cover_url": cover_url or None,
            "status": "fetched" if cover_url else "not_found",
        })

    return {
        "items": results,
        "count": len(results),
    }


def fetch_cover_image_bytes(url: str, timeout: int = 20) -> tuple[bytes, str]:
    if not _is_real_http_url(url) or is_placeholder_url(url):
        raise ValueError("cover url is invalid")
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
    response.raise_for_status()
    content_type = response.headers.get("content-type") or "image/jpeg"
    return response.content, content_type
