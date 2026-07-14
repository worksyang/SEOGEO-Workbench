from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from flask import current_app

from app.repositories.keyword_config_repo import KeywordConfigRepository
from app.repositories.keyword_settings_repo import KeywordSettingsRepository
from app.repositories.monitor_data_repo import MonitorDataRepository


DEFAULT_KEYWORDS_CONFIG_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "config" / "keywords.json"


def _config_path() -> Path:
    return Path(current_app.config.get("KEYWORDS_CONFIG_FILE", DEFAULT_KEYWORDS_CONFIG_FILE))


def _keyword_repo() -> KeywordConfigRepository:
    return KeywordConfigRepository(_config_path())


def _monitor_repo() -> MonitorDataRepository:
    return MonitorDataRepository(current_app.config["MONITOR_DATA_FILE"])


def _settings_repo() -> KeywordSettingsRepository:
    return KeywordSettingsRepository(current_app.config["SQLITE_PATH"])


def _build_keyword_stats() -> dict[str, dict[str, Any]]:
    """从 FastStore 内存索引获取 keyword 摘要，避免重复读 148MB 文件。"""
    try:
        from app.services.monitor_fast_service import get_fast_store
        store = get_fast_store()
        store.ensure_loaded()
        kw_by_id = store._keyword_by_id
        if kw_by_id is not None:
            return kw_by_id
    except (RuntimeError, Exception):
        pass
    # Fallback: 直接读文件
    try:
        data = _monitor_repo().load()
    except FileNotFoundError:
        return {}
    return {
        item.get("keyword_id"): item
        for item in data.get("keywords", [])
        if item.get("keyword_id")
    }


def load_keyword_manage_payload() -> dict[str, Any]:
    """返回包含 enabled/disabled 全集 manage payload；is_enabled_total / disabled_total 字段用于前端展示。"""
    payload = _keyword_repo().load()
    stats = _build_keyword_stats()
    groups = []
    total = 0
    ranked_total = 0
    enabled_total = 0
    disabled_total = 0

    for group in payload.get("groups", []):
        keywords = []
        for item in group.get("keywords", []):
            keyword_id = item.get("keyword_id")
            stat = stats.get(keyword_id, {})
            today_best = stat.get("today_best")
            ranked = bool(today_best)
            enabled = bool(item.get("enabled", True))
            total += 1
            if enabled:
                enabled_total += 1
                if ranked:
                    ranked_total += 1
            else:
                disabled_total += 1
            keywords.append({
                "keyword_id": keyword_id,
                "keyword_text": item.get("keyword_text", ""),
                "note": item.get("note") or "",
                "batch_default_selected": enabled,
                "enabled": enabled,
                "today_best": today_best if enabled else None,
                "coverage_days": stat.get("coverage_days", 0),
                "tracked_accounts": stat.get("tracked_accounts", 0),
                "article_count": stat.get("article_count", 0),
                "seo_status": ("ranked" if ranked else "not_ranked") if enabled else "disabled",
            })
        groups.append({
            "group_id": group.get("group_id"),
            "label": group.get("label"),
            "order": group.get("order", 0),
            "keywords": keywords,
            "total": len(keywords),
            "ranked_count": sum(1 for item in keywords if item.get("today_best")),
            "not_ranked_count": sum(1 for item in keywords if item.get("enabled") and not item.get("today_best")),
            "disabled_count": sum(1 for item in keywords if not item.get("enabled")),
        })

    return {
        "groups": groups,
        "total": total,
        "enabled_total": enabled_total,
        "disabled_total": disabled_total,
        "ranked_total": ranked_total,
        "not_ranked_total": enabled_total - ranked_total,
        "updated_at": payload.get("updated_at"),
    }


def list_keyword_groups() -> dict[str, Any]:
    return load_keyword_manage_payload()


def create_keyword_group(label: str) -> dict[str, Any]:
    return _keyword_repo().create_group(label=label)


def update_keyword_group(group_id: str, label: str | None = None, order: int | None = None) -> dict[str, Any]:
    return _keyword_repo().update_group(group_id=group_id, label=label, order=order)


def delete_keyword_group(group_id: str) -> dict[str, Any]:
    return _keyword_repo().delete_group(group_id=group_id)


def create_managed_keyword(group_id: str, keyword_text: str, note: str = "") -> dict[str, Any]:
    return _keyword_repo().create_keyword(
        group_id=group_id,
        keyword_text=keyword_text,
        note=note,
    )


def update_managed_keyword(
    keyword_id: str,
    keyword_text: str | None = None,
    note: str | None = None,
    group_id: str | None = None,
) -> dict[str, Any]:
    return _keyword_repo().update_keyword(
        keyword_id=keyword_id,
        keyword_text=keyword_text,
        note=note,
        group_id=group_id,
    )


def delete_managed_keyword(keyword_id: str) -> dict[str, Any]:
    return _keyword_repo().delete_keyword(keyword_id=keyword_id)


def list_batch_refresh_keywords() -> list[dict[str, Any]]:
    payload = load_keyword_manage_payload()
    items: list[dict[str, Any]] = []
    for group in payload.get("groups", []):
        for keyword in group.get("keywords", []):
            items.append({
                "group_id": group.get("group_id"),
                "group_label": group.get("label"),
                "group_order": group.get("order", 0),
                "keyword_id": keyword.get("keyword_id"),
                "keyword_text": keyword.get("keyword_text", ""),
                "batch_default_selected": bool(keyword.get("batch_default_selected", True)),
            })
    return items


def save_batch_default_selection(items: list[dict[str, object]]) -> None:
    _settings_repo().save_batch_default_selection(items)


def keyword_group_options() -> list[dict[str, str]]:
    payload = _keyword_repo().load()
    return [
        {"group_id": group.get("group_id"), "label": group.get("label")}
        for group in payload.get("groups", [])
    ]


def keyword_text_counts() -> Counter:
    payload = _keyword_repo().load()
    counter: Counter = Counter()
    for group in payload.get("groups", []):
        for item in group.get("keywords", []):
            counter[item.get("keyword_text", "")] += 1
    return counter
