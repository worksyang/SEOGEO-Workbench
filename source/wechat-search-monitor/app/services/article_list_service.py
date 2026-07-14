"""文章列表 service — 多数据源 join + 服务端分页/排序/筛选。

数据流:
  articles.json + accounts.json + ranking_hits.json
  + snapshots.json + SQLite keyword_registry + monitor-data.json(账号分)
  → 扁平文章列表

缓存: 首次调用全量 join 并缓存, rebuild 后清除。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from flask import current_app

from app.repositories.keyword_registry_repo import KeywordRegistryRepository

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
    """全量 join, 返回扁平文章列表。只包含被关键词命中过的文章。"""

    articles = _load_json("articles.json")
    accounts = _load_json("accounts.json")
    ranking_hits = _load_json("ranking_hits.json")
    snapshots = _load_json("snapshots.json")
    keywords = KeywordRegistryRepository(
        Path(current_app.config["SQLITE_PATH"])
    ).list_keywords(include_archived=True)
    monitor_data = _load_json("monitor-data.json")

    # --- 索引 ---
    acct_by_id: dict[str, dict] = {a["account_id"]: a for a in accounts}

    snap_to_kw_id: dict[str, str] = {
        s["snapshot_id"]: s["keyword_id"] for s in snapshots
    }
    snap_to_date: dict[str, str] = {
        s["snapshot_id"]: s.get("snapshot_date", "") for s in snapshots
    }
    kw_id_to_text: dict[str, str] = {
        k["keyword_id"]: k["keyword_text"] for k in keywords
    }

    # monitor-data 账号分
    md_accounts = monitor_data.get("accounts", [])
    md_acct_score: dict[str, float] = {}
    md_acct_name: dict[str, str] = {}
    md_acct_headimg: dict[str, str] = {}
    for a in md_accounts:
        aid = a.get("account_id", "")
        md_acct_score[aid] = a.get("score") or 0
        md_acct_name[aid] = a.get("name", "")
        md_acct_headimg[aid] = a.get("headimg_url", "")

    # --- ranking_hits 按 article_id 分组, 收集命中关键词 / 在榜日期 ---
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

    # --- 组装扁平文章 ---
    result: list[dict[str, Any]] = []
    for art in articles:
        aid = art.get("article_id", "")
        if aid not in art_hits:
            continue

        account_id = art.get("account_id", "")
        acct = acct_by_id.get(account_id, {})

        # 优先用 normalized accounts.json 的 canonical_name,
        # fallback 到 monitor-data 的 name
        account_name = (
            acct.get("canonical_name")
            or md_acct_name.get(account_id, "")
        )
        account_headimg = (
            acct.get("headimg_url")
            or md_acct_headimg.get(account_id, "")
        )

        hit_keywords = sorted(art_hits[aid])
        on_rank_days = len(art_snapshot_days.get(aid, set()))

        result.append({
            "article_id": aid,
            "title": art.get("title", ""),
            "url": art.get("normalized_url") or art.get("raw_url", ""),
            "account_id": account_id,
            "account_name": account_name,
            "account_headimg": account_headimg,
            "read_count": art.get("read_count"),
            "like_count": art.get("like_count"),
            "hit_count": len(hit_keywords),
            "hit_keywords": hit_keywords,
            "on_rank_days": on_rank_days,
            "account_score": md_acct_score.get(account_id, 0),
            "published_at": art.get("published_at"),
            "content_file_path": art.get("content_file_path"),
            "cover_url": art.get("cover_url"),
        })

    return result


def _build_account_list(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从已 join 的文章列表聚合账号维度。"""
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
    """rebuild 后调用, 清除内存缓存。"""
    global _cached_articles, _cached_account_list, _cache_articles_mtime
    with _cache_lock:
        _cached_articles = None
        _cached_account_list = None
        _cache_articles_mtime = None


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


_SORT_KEYS = {"reads", "hitCount", "publishTime", "likes", "accountScore", "todayReads", "onRankDays"}


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
    """筛选 + 排序 + 分页, 返回 {articles, total, page, page_size}。"""

    all_articles = _get_cached_articles()

    # --- 筛选 ---
    cutoff: datetime | None = None
    if time_range > 0:
        cutoff = datetime.now() - timedelta(days=time_range)

    search_lower = search.strip().lower() if search else ""

    filtered: list[dict[str, Any]] = []
    for a in all_articles:
        # 时间范围
        if cutoff is not None:
            pub = _parse_date(a.get("published_at"))
            if pub is None or pub < cutoff:
                continue
        # 命中词数
        if a["hit_count"] < min_hits:
            continue
        # 账号
        if account and a["account_id"] != account:
            continue
        # 标题搜索
        if search_lower and search_lower not in a["title"].lower():
            continue
        filtered.append(a)

    # --- 排序 ---
    sort_key = sort if sort in _SORT_KEYS else "reads"

    if sort_key == "reads":
        filtered.sort(key=lambda a: (a["read_count"] is None, -(a["read_count"] or 0)))
    elif sort_key == "onRankDays":
        filtered.sort(key=lambda a: (-(a.get("on_rank_days") or 0), a["read_count"] is None, -(a["read_count"] or 0)))
    elif sort_key == "hitCount":
        filtered.sort(key=lambda a: -a["hit_count"])
    elif sort_key == "publishTime":
        filtered.sort(key=lambda a: (_parse_date(a.get("published_at")) or datetime.min), reverse=True)
    elif sort_key == "likes":
        filtered.sort(key=lambda a: (a["like_count"] is None, -(a["like_count"] or 0)))
    elif sort_key == "accountScore":
        # 每账号最多展示 3 篇，账号间按账号分降序排列
        _MAX_PER_ACCT = 3
        by_acct: dict[str, list[dict[str, Any]]] = {}
        for a in filtered:
            by_acct.setdefault(a["account_id"], []).append(a)
        for arts in by_acct.values():
            arts.sort(key=lambda a: (a["read_count"] is None, -(a["read_count"] or 0)))
        filtered = []
        for acct_id in sorted(by_acct, key=lambda k: -by_acct[k][0]["account_score"]):
            filtered.extend(by_acct[acct_id][:_MAX_PER_ACCT])
    elif sort_key == "todayReads":
        # 今日热文：从今天起逐天往前补，每天内部按阅读量降序，最多100篇
        today = datetime.now()
        hard_cap = 100
        by_day: dict[str, list[dict[str, Any]]] = {}
        for a in filtered:
            d = (a.get("published_at") or "")[:10]
            if not d:
                continue
            by_day.setdefault(d, []).append(a)
        result: list[dict[str, Any]] = []
        for days_back in range(0, 30):
            check_date = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
            day_articles = by_day.get(check_date, [])
            day_articles.sort(key=lambda a: (a["read_count"] is None, -(a["read_count"] or 0)))
            result.extend(day_articles)
            if len(result) >= hard_cap:
                result = result[:hard_cap]
                break
        filtered = result

    # --- 分页 ---
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
    """返回有文章的账号列表, 按篇数降序。"""
    _get_cached_articles()
    global _cached_account_list
    with _cache_lock:
        if _cached_account_list is None:
            _cached_account_list = _build_account_list(_get_cached_articles())
        return _cached_account_list
