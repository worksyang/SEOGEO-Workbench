from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from flask import current_app

from app.repositories.keyword_registry_repo import KeywordRegistryRepository

from app.ingest.common import normalize_url


def _normalized_dir() -> Path:
    return Path(current_app.config["NORMALIZED_DIR"])


def _load_json(filename: str) -> Any:
    path = _normalized_dir() / filename
    if not path.exists():
        raise FileNotFoundError(f"normalized data not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _coalesce_url(article: dict[str, Any]) -> str:
    return article.get("normalized_url") or article.get("raw_url") or ""


def _fmt_time(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return ""
    date = snapshot.get("snapshot_date") or ""
    time = snapshot.get("snapshot_time") or ""
    return f"{date} {time}".strip()


def _metric_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _keyword_meta_map(monitor_data: dict[str, Any], keywords: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_text = {
        item.get("keyword"): item
        for item in monitor_data.get("keywords", [])
        if item.get("keyword")
    }
    result: dict[str, dict[str, Any]] = {}
    for keyword in keywords:
        text = keyword.get("keyword_text") or ""
        enriched = by_text.get(text) or {}
        result[keyword.get("keyword_id") or ""] = {
            "keyword_id": keyword.get("keyword_id") or "",
            "keyword": text,
            "topic": enriched.get("topic") or text,
            "keyword_bucket": enriched.get("keyword_bucket") or "未分类",
        }
    return result


def _build_timeline_events(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not hits:
        return []

    ordered = sorted(hits, key=lambda item: (item.get("captured_at") or "", item.get("rank") or 99))
    first = ordered[0]
    events = [{
        "type": "first",
        "label": "首次命中",
        "title": f"{first.get('keyword') or '未知关键词'} · 第 {first.get('rank') or '—'} 名",
        "description": f"{first.get('captured_at_label') or ''}，首次进入搜索结果。",
    }]

    first_keyword = first.get("keyword_id")
    first_topic = first.get("topic")
    first_bucket = first.get("keyword_bucket")

    same_topic = next(
        (
            item for item in ordered
            if item.get("keyword_id") != first_keyword and item.get("topic") == first_topic
        ),
        None,
    )
    if same_topic:
        events.append({
            "type": "topic",
            "label": "同主题扩散",
            "title": f"{same_topic.get('keyword') or '未知关键词'} · 第 {same_topic.get('rank') or '—'} 名",
            "description": f"{same_topic.get('captured_at_label') or ''}，同一主题的另一个关键词也命中。",
        })

    cross_bucket = next(
        (
            item for item in ordered
            if item.get("keyword_bucket") and item.get("keyword_bucket") != first_bucket
        ),
        None,
    )
    if cross_bucket:
        events.append({
            "type": "bucket",
            "label": "跨类目命中",
            "title": f"{cross_bucket.get('keyword') or '未知关键词'} · 第 {cross_bucket.get('rank') or '—'} 名",
            "description": f"{cross_bucket.get('captured_at_label') or ''}，扩散到「{cross_bucket.get('keyword_bucket')}」。",
        })

    return events


def resolve_article_hit_detail(article_id: str = "", url: str = "") -> dict[str, Any]:
    articles = _load_json("articles.json")
    accounts = _load_json("accounts.json")
    ranking_hits = _load_json("ranking_hits.json")
    snapshots = _load_json("snapshots.json")
    keywords = KeywordRegistryRepository(
        Path(current_app.config["SQLITE_PATH"])
    ).list_keywords(include_archived=True)
    try:
        monitor_data = _load_json("monitor-data.json")
    except FileNotFoundError:
        monitor_data = {}

    articles_by_id = {item.get("article_id"): item for item in articles if item.get("article_id")}
    accounts_by_id = {item.get("account_id"): item for item in accounts if item.get("account_id")}
    snapshots_by_id = {item.get("snapshot_id"): item for item in snapshots if item.get("snapshot_id")}
    keyword_meta_by_id = _keyword_meta_map(monitor_data, keywords)

    target_article = articles_by_id.get(article_id) if article_id else None
    target_url = normalize_url(url) if url else ""
    if target_article and not target_url:
        target_url = _coalesce_url(target_article)
    if not target_article and target_url:
        target_article = next(
            (
                item for item in articles
                if normalize_url(_coalesce_url(item)) == target_url
            ),
            None,
        )

    if not target_article:
        raise FileNotFoundError("article not found")

    target_url = target_url or _coalesce_url(target_article)
    if target_url:
        related_articles = [
            item for item in articles
            if normalize_url(_coalesce_url(item)) == normalize_url(target_url)
        ]
    else:
        related_articles = [target_article]

    related_article_ids = {item.get("article_id") for item in related_articles if item.get("article_id")}
    account = accounts_by_id.get(target_article.get("account_id"), {})

    hit_records: list[dict[str, Any]] = []
    for hit in ranking_hits:
        if hit.get("article_id") not in related_article_ids:
            continue
        snapshot = snapshots_by_id.get(hit.get("snapshot_id"))
        keyword_meta = keyword_meta_by_id.get(snapshot.get("keyword_id") if snapshot else "", {})
        article = articles_by_id.get(hit.get("article_id"), target_article)
        hit_records.append({
            "hit_id": hit.get("hit_id") or "",
            "article_id": hit.get("article_id") or "",
            "snapshot_id": hit.get("snapshot_id") or "",
            "rank": hit.get("rank"),
            "keyword_id": keyword_meta.get("keyword_id") or "",
            "keyword": keyword_meta.get("keyword") or "",
            "topic": keyword_meta.get("topic") or "",
            "keyword_bucket": keyword_meta.get("keyword_bucket") or "未分类",
            "captured_at": snapshot.get("captured_at") if snapshot else "",
            "captured_at_label": _fmt_time(snapshot),
            "snapshot_date": snapshot.get("snapshot_date") if snapshot else "",
            "snapshot_time": snapshot.get("snapshot_time") if snapshot else "",
            "batch_id": (snapshot.get("source_file_path") or "").split("/批量抓取/")[-1].split("/")[0]
            if snapshot and "/批量抓取/" in (snapshot.get("source_file_path") or "") else "",
            "title": article.get("title") or hit.get("title_raw") or "",
            "account_id": hit.get("account_id") or article.get("account_id") or "",
        })

    hit_records.sort(key=lambda item: (item.get("captured_at") or "", item.get("rank") or 99), reverse=True)

    grouped: dict[str, dict[str, Any]] = {}
    for hit in hit_records:
        key = hit.get("keyword_id") or hit.get("keyword") or "unknown"
        group = grouped.setdefault(key, {
            "keyword_id": hit.get("keyword_id") or "",
            "keyword": hit.get("keyword") or "未知关键词",
            "topic": hit.get("topic") or "",
            "keyword_bucket": hit.get("keyword_bucket") or "未分类",
            "hit_count": 0,
            "best_rank": None,
            "latest_rank": None,
            "latest_seen_at": "",
            "hits": [],
        })
        group["hit_count"] += 1
        rank = hit.get("rank")
        if rank and (group["best_rank"] is None or rank < group["best_rank"]):
            group["best_rank"] = rank
        if not group["latest_seen_at"] or hit.get("captured_at", "") > group["latest_seen_at"]:
            group["latest_seen_at"] = hit.get("captured_at") or ""
            group["latest_rank"] = rank
        group["hits"].append(hit)

    keyword_groups = sorted(
        grouped.values(),
        key=lambda item: (
            -(item.get("hit_count") or 0),
            item.get("best_rank") or 99,
            item.get("keyword") or "",
        ),
    )

    content_files = []
    seen_files: set[str] = set()
    for item in related_articles:
        path = item.get("content_file_path")
        if not path or path in seen_files:
            continue
        seen_files.add(path)
        content_files.append({
            "article_id": item.get("article_id") or "",
            "title": item.get("title") or "",
            "path": path,
            "is_primary": item.get("article_id") == target_article.get("article_id"),
        })

    metric_points = []
    for item in related_articles:
        if not any(item.get(key) is not None for key in ("read_count", "like_count", "friends_follow_count")):
            continue
        metric_points.append({
            "article_id": item.get("article_id") or "",
            "captured_at": item.get("last_seen_at") or item.get("first_seen_at") or "",
            "read_count": _metric_value(item.get("read_count")),
            "like_count": _metric_value(item.get("like_count")),
            "friends_follow_count": _metric_value(item.get("friends_follow_count")),
            "content_file_path": item.get("content_file_path") or "",
        })
    metric_points.sort(key=lambda item: item.get("captured_at") or "")

    return {
        "article": {
            "article_id": target_article.get("article_id") or "",
            "title": target_article.get("title") or "",
            "url": _coalesce_url(target_article),
            "normalized_url": normalize_url(_coalesce_url(target_article)),
            "published_at": target_article.get("published_at") or "",
            "summary": target_article.get("summary") or "",
            "content_path": target_article.get("content_file_path") or "",
            "read_count": _metric_value(target_article.get("read_count")),
            "like_count": _metric_value(target_article.get("like_count")),
            "friends_follow_count": _metric_value(target_article.get("friends_follow_count")),
            "original_article_count": _metric_value(target_article.get("original_article_count")),
            "first_seen_at": target_article.get("first_seen_at") or "",
            "last_seen_at": target_article.get("last_seen_at") or "",
        },
        "account": {
            "account_id": account.get("account_id") or target_article.get("account_id") or "",
            "name": account.get("canonical_name") or "",
            "headimg_url": account.get("headimg_url") or "",
            "wechat_biz": account.get("wechat_biz") or "",
        },
        "url_profile": {
            "url": normalize_url(target_url),
            "article_ids": sorted(related_article_ids),
            "article_record_count": len(related_articles),
            "title_variants": sorted({item.get("title") for item in related_articles if item.get("title")}),
        },
        "keyword_groups": keyword_groups,
        "keyword_cloud": [
            {
                "keyword": item.get("keyword"),
                "hit_count": item.get("hit_count"),
                "best_rank": item.get("best_rank"),
                "topic": item.get("topic"),
                "keyword_bucket": item.get("keyword_bucket"),
            }
            for item in keyword_groups
        ],
        "hit_count": len(hit_records),
        "keyword_count": len(keyword_groups),
        "content_files": content_files,
        "metric_points": metric_points,
        "timeline_events": _build_timeline_events(hit_records),
    }
