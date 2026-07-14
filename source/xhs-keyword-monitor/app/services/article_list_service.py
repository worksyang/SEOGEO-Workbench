"""article_list_service — 文章列表 join + 分页/排序/筛选。

数据源：
  normalized/articles.json
  normalized/accounts.json
  normalized/ranking_hits.json
  normalized/snapshots.json
  normalized/keywords.json
  normalized/monitor-data.json (账号三榜 + 六边形)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from flask import current_app


_cache_lock = Lock()
_cached_articles: list[dict[str, Any]] | None = None
_cached_account_list: list[dict[str, Any]] | None = None
_cache_articles_mtime: float | None = None


def _normalized_dir() -> Path:
    return Path(current_app.config["NORMALIZED_DIR"])


def _load_json(filename: str) -> Any:
    path = _normalized_dir() / filename
    if not path.exists():
        raise FileNotFoundError(f"normalized data not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _build_articles() -> list[dict[str, Any]]:
    articles = _load_json("articles.json")
    accounts = _load_json("accounts.json")
    ranking_hits = _load_json("ranking_hits.json")
    snapshots = _load_json("snapshots.json")
    keywords = _load_json("keywords.json")
    monitor_data = _load_json("monitor-data.json")

    acct_by_id = {a["account_id"]: a for a in accounts}

    snap_to_kw_id = {s["snapshot_id"]: s["keyword_id"] for s in snapshots}
    snap_to_date = {s["snapshot_id"]: s.get("snapshot_date", "") for s in snapshots}
    kw_id_to_text = {k["keyword_id"]: k["keyword_text"] for k in keywords}

    md_accounts = monitor_data.get("accounts", [])
    md_acct_score = {a.get("account_id"): a.get("score") or 0 for a in md_accounts}
    md_acct_name = {a.get("account_id"): a.get("name", "") for a in md_accounts}
    md_acct_headimg = {a.get("account_id"): a.get("headimg_url") or "" for a in md_accounts}

    art_hits: dict[str, set[str]] = {}
    art_snapshot_days: dict[str, set[str]] = {}
    for h in ranking_hits:
        aid = h.get("article_id", "")
        if not aid:
            continue
        snap_id = h.get("snapshot_id", "")
        kw_id = snap_to_kw_id.get(snap_id, "")
        kw_text = kw_id_to_text.get(kw_id, "")
        if kw_text:
            art_hits.setdefault(aid, set()).add(kw_text)
        snapshot_date = snap_to_date.get(snap_id, "")
        if snapshot_date:
            art_snapshot_days.setdefault(aid, set()).add(snapshot_date)

    result: list[dict[str, Any]] = []
    for art in articles:
        aid = art.get("article_id", "")
        if aid not in art_hits:
            continue
        account_id = art.get("account_id", "")
        acct = acct_by_id.get(account_id, {})
        account_name = acct.get("canonical_name") or md_acct_name.get(account_id, "")
        account_headimg = acct.get("headimg_url") or md_acct_headimg.get(account_id, "")

        hit_keywords = sorted(art_hits[aid])
        on_rank_days = len(art_snapshot_days.get(aid, set()))

        result.append({
            "article_id": aid,
            "title": art.get("title", ""),
            "url": art.get("normalized_url") or art.get("raw_url", ""),
            "account_id": account_id,
            "account_name": account_name,
            "account_headimg": account_headimg,
            # XHS 主信号
            "liked_count": art.get("liked_count"),
            "collected_count": art.get("collected_count"),
            "comment_count": art.get("comment_count"),
            "shared_count": art.get("shared_count"),
            # 兼容字段：原 read_count 映射到 read_count
            "read_count": art.get("read_count"),
            "work_type": art.get("work_type"),
            "cover_url": art.get("cover_url"),
            "is_relevant": art.get("is_relevant"),
            "relevance_score": art.get("relevance_score"),
            # 排序/筛选字段
            "hit_count": len(hit_keywords),
            "hit_keywords": hit_keywords,
            "on_rank_days": on_rank_days,
            "account_score": md_acct_score.get(account_id, 0),
            "published_at": art.get("published_at"),
            "content_file_path": None,
        })

    return result


def _build_account_list(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    acct_map: dict[str, dict[str, Any]] = {}
    for a in articles:
        aid = a["account_id"]
        if aid not in acct_map:
            acct_map[aid] = {
                "account_id": aid,
                "name": a["account_name"],
                "headimg_url": a["account_headimg"],
                "article_count": 0,
            }
        acct_map[aid]["article_count"] += 1
    return sorted(acct_map.values(), key=lambda x: (-x["article_count"], x["name"]))


def _get_cached_articles() -> list[dict[str, Any]]:
    global _cached_articles, _cached_account_list, _cache_articles_mtime
    with _cache_lock:
        monitor_path = Path(current_app.config["MONITOR_DATA_FILE"])
        try:
            current_mtime = monitor_path.stat().st_mtime
        except OSError:
            current_mtime = None
        if _cached_articles is None or _cache_articles_mtime != current_mtime:
            _cached_articles = _build_articles()
            _cached_account_list = _build_account_list(_cached_articles)
            _cache_articles_mtime = current_mtime
        return _cached_articles


def invalidate_cache() -> None:
    global _cached_articles, _cached_account_list, _cache_articles_mtime
    with _cache_lock:
        _cached_articles = None
        _cached_account_list = None
        _cache_articles_mtime = None


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    # Try ISO 8601 with timezone (2026-06-07T17:42:51+08:00 or 2026-06-07T17:42:51)
    try:
        dt = datetime.fromisoformat(str(s))
        # 统一转为 naive（保留原 UTC 偏移），让 cutoff 比较不出错
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        pass
    # Fallback to plain formats
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s), fmt)
        except ValueError:
            continue
    return None


_SORT_KEYS = {
    "reads", "hitCount", "publishTime", "likes", "collects", "comments",
    "shared", "accountScore", "todayTop3", "onRankDays",
}


def _signal_total(a: dict) -> int:
    """小红书互动信号总和（缺失不算）。"""
    total = 0
    for k in ("collected_count", "liked_count", "comment_count", "shared_count"):
        v = a.get(k)
        if isinstance(v, (int, float)) and v is not None:
            total += int(v)
    return total


def list_articles(
    *,
    page: int = 1,
    page_size: int = 50,
    sort: str = "reads",
    time_range: int = 15,
    min_hits: int = 0,
    account: str = "",
    search: str = "",
) -> dict[str, Any]:
    all_articles = _get_cached_articles()

    cutoff: datetime | None = None
    if time_range > 0:
        cutoff = datetime.now() - timedelta(days=time_range)

    search_lower = search.strip().lower() if search else ""

    filtered: list[dict[str, Any]] = []
    for a in all_articles:
        if cutoff is not None:
            pub = _parse_date(a.get("published_at"))
            if pub is None or pub < cutoff:
                continue
        if a["hit_count"] < min_hits:
            continue
        if account and a["account_id"] != account:
            continue
        if search_lower and search_lower not in a["title"].lower():
            continue
        filtered.append(a)

    sort_key = sort if sort in _SORT_KEYS else "reads"

    if sort_key == "reads":
        filtered.sort(key=lambda a: (a["read_count"] is None, -(a["read_count"] or 0)))
    elif sort_key == "onRankDays":
        filtered.sort(key=lambda a: (-(a.get("on_rank_days") or 0), a["collected_count"] is None, -(a["collected_count"] or 0)))
    elif sort_key == "hitCount":
        filtered.sort(key=lambda a: -a["hit_count"])
    elif sort_key == "publishTime":
        filtered.sort(key=lambda a: (_parse_date(a.get("published_at")) or datetime.min), reverse=True)
    elif sort_key == "likes":
        filtered.sort(key=lambda a: (a["liked_count"] is None, -(a["liked_count"] or 0)))
    elif sort_key == "collects":
        filtered.sort(key=lambda a: (a["collected_count"] is None, -(a["collected_count"] or 0)))
    elif sort_key == "comments":
        filtered.sort(key=lambda a: (a["comment_count"] is None, -(a["comment_count"] or 0)))
    elif sort_key == "shared":
        filtered.sort(key=lambda a: (a["shared_count"] is None, -(a["shared_count"] or 0)))
    elif sort_key == "accountScore":
        _MAX_PER_ACCT = 3
        by_acct: dict[str, list[dict[str, Any]]] = {}
        for a in filtered:
            by_acct.setdefault(a["account_id"], []).append(a)
        for arts in by_acct.values():
            arts.sort(key=lambda a: (a["collected_count"] is None, -(a["collected_count"] or 0)))
        filtered = []
        for acct_id in sorted(by_acct, key=lambda k: -by_acct[k][0]["account_score"]):
            filtered.extend(by_acct[acct_id][:_MAX_PER_ACCT])
    elif sort_key == "todayTop3":
        # XHS 暂无当天分时间序列，按发布时间倒序近似
        filtered.sort(key=lambda a: (_parse_date(a.get("published_at")) or datetime.min), reverse=True)

    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    page_articles = filtered[start:end]

    return {
        "articles": page_articles,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def list_accounts_with_article_count() -> list[dict[str, Any]]:
    _get_cached_articles()
    global _cached_account_list
    with _cache_lock:
        if _cached_account_list is None:
            _cached_account_list = _build_account_list(_get_cached_articles())
        return _cached_account_list
