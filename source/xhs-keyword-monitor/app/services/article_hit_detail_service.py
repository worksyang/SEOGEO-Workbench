"""article_hit_detail_service — 文章命中详情 (XHS 适配)。

复用 normalized 实体数据构造命中详情（包含文章元数据 + 命中关键词 + 时间线）。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import current_app


def _normalized_dir() -> Path:
    return Path(current_app.config["NORMALIZED_DIR"])


def _load_json(filename: str) -> Any:
    path = _normalized_dir() / filename
    if not path.exists():
        raise FileNotFoundError(f"normalized data not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_article_hit_detail(article_id: str | None = None, url: str | None = None) -> dict:
    if not article_id and not url:
        raise ValueError("article_id or url is required")

    articles = _load_json("articles.json")
    accounts = _load_json("accounts.json")
    snapshots = _load_json("snapshots.json")
    keywords = _load_json("keywords.json")
    ranking_hits = _load_json("ranking_hits.json")
    metric_obs = _load_json("note_metric_observations.json")

    art_by_id = {a.get("article_id"): a for a in articles}
    acct_by_id = {a.get("account_id"): a for a in accounts}
    snap_by_id = {s.get("snapshot_id"): s for s in snapshots}
    kw_by_id = {k.get("keyword_id"): k for k in keywords}

    target = None
    if article_id:
        target = art_by_id.get(article_id)
    if target is None and url:
        for a in articles:
            if (a.get("normalized_url") or a.get("raw_url")) == url:
                target = a
                break
    if target is None:
        raise FileNotFoundError("article not found")

    aid = target.get("article_id")
    acct = acct_by_id.get(target.get("account_id"), {})

    # 命中关键词 + 时间线
    kw_groups: dict[str, list[dict]] = defaultdict(list)
    timeline: list[dict] = []
    for h in ranking_hits:
        if h.get("article_id") != aid:
            continue
        snap = snap_by_id.get(h.get("snapshot_id", ""))
        if not snap:
            continue
        kid = snap.get("keyword_id", "")
        kw = kw_by_id.get(kid, {})
        kw_groups[kid].append({
            "snapshot_id": snap.get("snapshot_id"),
            "captured_at": snap.get("captured_at"),
            "rank": h.get("rank"),
            "trigger_type": snap.get("trigger_type"),
            "keyword_text": kw.get("keyword_text", kid),
        })
        timeline.append({
            "captured_at": snap.get("captured_at"),
            "rank": h.get("rank"),
            "keyword_text": kw.get("keyword_text", kid),
            "trigger_type": snap.get("trigger_type"),
        })
    timeline.sort(key=lambda x: x.get("captured_at") or "")
    kw_groups_out = sorted(
        [
            {
                "keyword_id": kid,
                "keyword_text": kw_by_id.get(kid, {}).get("keyword_text", kid),
                "events": sorted(events, key=lambda e: e.get("captured_at") or ""),
            }
            for kid, events in kw_groups.items()
        ],
        key=lambda g: g["keyword_text"],
    )

    # 互动观测时间序列
    obs_points = []
    for obs in metric_obs:
        if obs.get("article_id") != aid:
            continue
        obs_points.append({
            "captured_at": obs.get("captured_at"),
            "liked_count": obs.get("liked_count"),
            "collected_count": obs.get("collected_count"),
            "comment_count": obs.get("comment_count"),
            "shared_count": obs.get("shared_count"),
            "read_count": obs.get("read_count"),
        })
    obs_points.sort(key=lambda x: x.get("captured_at") or "")

    # 文章 url 画像
    urls = sorted({u for u in (target.get("normalized_url"), target.get("raw_url")) if u})

    return {
        "article_id": aid,
        "platform": target.get("platform", "小红书"),
        "title": target.get("title", ""),
        "summary": target.get("summary") or "",
        "markdown": (target.get("summary") or "").strip(),
        "platform": target.get("platform", "小红书"),
        "url": target.get("raw_url") or target.get("normalized_url"),
        "cover_url": target.get("cover_url"),
        "work_type": target.get("work_type"),
        "published_at": target.get("published_at"),
        "account": {  # XHS 博主
            "account_id": target.get("account_id"),
            "name": acct.get("canonical_name") or target.get("account_id"),
            "headimg_url": acct.get("headimg_url"),
            "fans": acct.get("fans"),
            "total_works": acct.get("total_works"),
            "likes_total": acct.get("likes"),
            "collects_total": acct.get("collects"),
            "ip_location": acct.get("ip_location"),
        },
        "metrics": {
            "liked_count": target.get("liked_count"),
            "collected_count": target.get("collected_count"),
            "comment_count": target.get("comment_count"),
            "shared_count": target.get("shared_count"),
            "read_count": target.get("read_count"),
        },
        "metrics_note": "小红书不公开阅读量；收藏+点赞+评论+分享是核心信号",
        "hit_groups": kw_groups_out,
        "timeline": timeline,
        "metric_series": obs_points,
        "is_relevant": target.get("is_relevant", True),
        "relevance_score": target.get("relevance_score", 1.0),
        "urls": urls,
        "first_seen_at": target.get("first_seen_at"),
        "last_seen_at": target.get("last_seen_at"),
    }
