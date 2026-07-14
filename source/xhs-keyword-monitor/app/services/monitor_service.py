"""monitor_service — 加载 monitor-data.json 并叠加控制层状态。"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import current_app
from app.ingest.builders.monitor_keyword_heat import interaction_weights_payload



def _load_json(filename: str) -> Any:
    p = Path(current_app.config["NORMALIZED_DIR"]) / filename
    if not p.exists():
        raise FileNotFoundError(f"{filename} not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _settings_repo():
    from app.repositories.keyword_settings_repo import KeywordSettingsRepository
    return KeywordSettingsRepository(current_app.config["SQLITE_PATH"])


def _monitor_data_repo():
    from app.repositories.monitor_data_repo import MonitorDataRepository
    return MonitorDataRepository(current_app.config["MONITOR_DATA_FILE"])


def _keyword_config_repo():
    from app.repositories.keyword_config_repo import KeywordConfigRepository
    return KeywordConfigRepository(current_app.config["KEYWORDS_CONFIG_FILE"])


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _empty_keyword_payload(item: dict, group: dict, window_days: int) -> dict[str, Any]:
    return {
        "keyword": item.get("keyword_text", ""),
        "keyword_id": item.get("keyword_id", ""),
        "topic": item.get("keyword_text", ""),
        "keyword_bucket": group.get("label", "未分类"),
        "today_best": None,
        "today_count": 0,
        "coverage_days": 0,
        "tracked_accounts": 0,
        "article_count": 0,
        "latest_run": None,
        "runs": [],
        "history_best": [0] * window_days,
        "history_hits": [0] * window_days,
        "accounts": [],
        "heat_summary": {},
        "kw_score": {"total": 0, "heat": 0, "breadth": 0, "richness": 0, "has_heat": False},
        "keyword_heat_metric": {
            "status": "no_data",
            "method": "xhs_visible_interaction_heat_v1",
            "version": "1.0.0",
            "window_days": window_days,
            "effective_days": 0,
            "steady_heat": 0.0,
            "peak_heat": 0.0,
            "heat_delta_15d": 0.0,
            "peak_date": None,
            "trend_signal": 0.0,
            "trend_ratio": 0.0,
            "trend_label": "观察中",
            "confidence_score": 0,
            "confidence_level": "insufficient",
            "interaction_weights": interaction_weights_payload(),
            "value_signal": {"score": 0.0, "label": "观察中",
                             "components": {"heat_trend_ratio": 0.0, "note_supply_trend_ratio": 0.0,
                                            "creator_breadth_trend_ratio": 0.0}},
            "current_interactions": {"likes": 0, "collects": 0, "comments": 0, "shares": 0,
                                      "equivalent": 0.0, "note_count": 0, "creator_count": 0,
                                      "interaction_structure": {"likes_pct": 0.0, "collects_pct": 0.0,
                                                                 "comments_pct": 0.0, "shares_pct": 0.0}},
            "daily_heat_points": [],
        },
        "keyword_read_delta": {
            "status": "insufficient_data",
            "reason": "xhs_no_reading_proxy",
            "trend_label": "观察中",
        },
    }


def _apply_keyword_config_scope(data: dict) -> dict:
    payload = _keyword_config_repo().load()
    monitor_by_id = {item.get("keyword_id"): item for item in data.get("keywords", []) if item.get("keyword_id")}
    window_days = int(data.get("window_days") or 15)
    keywords = []
    for group in payload.get("groups", []):
        for item in group.get("keywords", []):
            kid = item.get("keyword_id")
            merged = dict(monitor_by_id.get(kid) or _empty_keyword_payload(item, group, window_days))
            merged["keyword_id"] = kid
            merged["keyword"] = item.get("keyword_text", merged.get("keyword", ""))
            merged["keyword_bucket"] = merged.get("keyword_bucket") or group.get("label") or "未分类"
            keywords.append(merged)
    return {**data, "keywords": keywords}


def _merge_keyword_states(data: dict, settings: dict[str, dict]) -> dict:
    keywords = []
    for item in data.get("keywords", []):
        merged = dict(item)
        st = settings.get(item.get("keyword_id", ""), {})
        merged["is_pinned"] = bool(st.get("is_pinned", False))
        merged["pin_order"] = st.get("pin_order")
        merged["topic"] = st.get("topic") or merged.get("topic") or merged.get("keyword")
        merged["keyword_bucket"] = st.get("keyword_bucket") or merged.get("keyword_bucket") or "未分类"
        keywords.append(merged)
    return {**data, "keywords": keywords}


def _sort_keywords(keywords: list[dict]) -> list[dict]:
    def metric_number(value: Any) -> float:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return number if math.isfinite(number) else 0.0

    def sort_key(item: dict):
        km = item.get("keyword_heat_metric") or {}
        if item.get("is_pinned"):
            segment = 0
        elif km.get("effective_days", 0) > 0:
            segment = 1
        else:
            segment = 2
        return (
            segment,
            -metric_number(km.get("steady_heat")),
            -metric_number(km.get("peak_heat")),
            str(item.get("keyword") or ""),
        )

    keywords.sort(key=sort_key)
    return keywords


def load_monitor_payload() -> dict:
    data = _monitor_data_repo().load()
    data = _apply_keyword_config_scope(data)
    settings = _settings_repo().list_all()
    data = _merge_keyword_states(data, settings)
    keywords = _sort_keywords(data["keywords"])
    data["keywords"] = keywords
    data["pinned_keyword_count"] = sum(1 for k in keywords if k.get("is_pinned"))
    from app.keyword_bucket_resolver import bucket_options
    data["keyword_bucket_options"] = bucket_options()
    data["keyword_scope"] = "configured"
    data["keyword_source_total"] = len(keywords)
    return data


def set_keyword_pin(keyword_id: str, keyword_text: str, is_pinned: bool) -> dict:
    return _settings_repo().set_pin_state(keyword_id=keyword_id, keyword_text=keyword_text, is_pinned=is_pinned)


def set_keyword_topic(keyword_id: str, keyword_text: str, topic: str | None) -> dict:
    state = _settings_repo().set_topic(keyword_id=keyword_id, keyword_text=keyword_text, topic=topic)
    from app.ingest.rebuild import rebuild_all
    rebuild_all(verbose=False)
    return state


def set_keyword_bucket(keyword_id: str, keyword_text: str, keyword_bucket: str | None) -> dict:
    state = _settings_repo().set_bucket(
        keyword_id=keyword_id, keyword_text=keyword_text, keyword_bucket=keyword_bucket,
    )
    from app.ingest.rebuild import rebuild_all
    rebuild_all(verbose=False)
    return state


def set_keyword_note(keyword_id: str, keyword_text: str, note: str) -> dict:
    repo = _settings_repo()
    now = _now()
    with repo._connect() as conn:
        row = conn.execute("SELECT keyword_id FROM keyword_settings WHERE keyword_id = ?", (keyword_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE keyword_settings SET keyword_text = ?, note = ?, updated_at = ? WHERE keyword_id = ?",
                (keyword_text, note, now, keyword_id),
            )
        else:
            conn.execute(
                "INSERT INTO keyword_settings (keyword_id, keyword_text, is_pinned, pin_order, is_active, topic, keyword_bucket, note, updated_at) VALUES (?, ?, 0, NULL, 1, NULL, NULL, ?, ?)",
                (keyword_id, keyword_text, note, now),
            )
    state = repo.get(keyword_id)
    if state is None:
        raise RuntimeError("failed to persist keyword note")
    return state


def resolve_article_markdown(content_path: str) -> dict:
    """XHS 抽屉详情 — 返回 workDesc 纯文本/Markdown + 互动数据 + 封面 + 原始链接。

    不需要图片 OCR；content_path 在 XHS 场景是占位字段（保留兼容）。
    """
    if not content_path or not content_path.strip():
        raise ValueError("content_path is required")
    # XHS 不下载正文，content_path 仅作为占位
    normalized_dir = Path(current_app.config["NORMALIZED_DIR"])
    articles_path = normalized_dir / "articles.json"
    if not articles_path.exists():
        raise FileNotFoundError(f"articles.json not found: {articles_path}")

    article_id = content_path.strip()
    articles = json.loads(articles_path.read_text(encoding="utf-8"))
    target = None
    for art in articles:
        if art.get("article_id") == article_id:
            target = art
            break
    if target is None:
        raise FileNotFoundError(f"article not found: {article_id}")

    acct = next((a for a in _load_json("accounts.json") if a.get("account_id") == target.get("account_id")), None) or {}
    metrics = {
        "liked_count": target.get("liked_count"),
        "collected_count": target.get("collected_count"),
        "comment_count": target.get("comment_count"),
        "shared_count": target.get("shared_count"),
        "read_count": target.get("read_count"),
    }
    visible_count = sum(1 for v in metrics.values() if isinstance(v, (int, float)) and v is not None)
    return {
        "article_id": target.get("article_id"),
        "title": target.get("title", ""),
        "summary": target.get("summary") or "",
        "markdown": (target.get("summary") or "").strip(),  # XHS 笔记正文 = workDesc
        "url": target.get("raw_url") or target.get("normalized_url"),
        "cover_url": target.get("cover_url"),
        "work_type": target.get("work_type"),
        "published_at": target.get("published_at"),
        "first_seen_at": target.get("first_seen_at"),
        "last_seen_at": target.get("last_seen_at"),
        "metrics": metrics,
        "metrics_visibility": f"{visible_count}/5",
        "account": {
            "account_id": target.get("account_id"),
            "name": acct.get("canonical_name") or target.get("account_id"),
            "headimg_url": acct.get("headimg_url"),
            "description": acct.get("description"),
            "fans": acct.get("fans"),
            "total_works": acct.get("total_works"),
            "likes_total": acct.get("likes"),
            "collects_total": acct.get("collects"),
            "follows_total": acct.get("follows"),
            "ip_location": acct.get("ip_location"),
            "verify_info": acct.get("verify_info"),
            "platform": acct.get("platform", "小红书"),
        },
        "platform_payload": target.get("platform_payload", {}),
        "is_relevant": target.get("is_relevant", True),
        "relevance_score": target.get("relevance_score", 1.0),
        "content_status": target.get("content_status", "available" if target.get("summary") else "missing"),
        "source": target.get("source") or "tikhub_xhs_search_notes",
    }
