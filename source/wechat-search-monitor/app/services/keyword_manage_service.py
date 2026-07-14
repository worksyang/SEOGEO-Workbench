from __future__ import annotations

from collections import Counter
from typing import Any

from flask import current_app

from app.repositories.keyword_registry_repo import KeywordRegistryRepository
from app.repositories.monitor_data_repo import MonitorDataRepository


def _keyword_repo() -> KeywordRegistryRepository:
    return KeywordRegistryRepository(current_app.config["SQLITE_PATH"])


def _monitor_repo() -> MonitorDataRepository:
    return MonitorDataRepository(current_app.config["MONITOR_DATA_FILE"])


def _build_keyword_stats() -> dict[str, dict[str, Any]]:
    from app.services.monitor_fast_service import try_get_fast_store

    fast_store = try_get_fast_store()
    if fast_store is not None:
        return fast_store.get_keyword_stats()

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
    payload = _keyword_repo().load()
    stats = _build_keyword_stats()
    groups = []
    total = 0
    ranked_total = 0

    for group in payload.get("groups", []):
        keywords = []
        for item in group.get("keywords", []):
            keyword_id = item.get("keyword_id")
            stat = stats.get(keyword_id, {})
            today_best = stat.get("today_best")
            ranked = bool(today_best)
            total += 1
            ranked_total += 1 if ranked else 0
            keywords.append({
                "keyword_id": keyword_id,
                "keyword_text": item.get("keyword_text", ""),
                "note": item.get("note") or "",
                "batch_default_selected": bool(item.get("batch_default_selected", True)),
                "refresh_frequency_days": int(item.get("refresh_frequency_days") or 1),
                "effective_refresh_interval_hours": item.get(
                    "effective_refresh_interval_hours",
                    int(item.get("refresh_frequency_days") or 1) * 24,
                ),
                "refresh_frequency_source": item.get("refresh_frequency_source") or "auto",
                "refresh_policy_reason": item.get("refresh_policy_reason") or "",
                "last_refresh_at": item.get("last_refresh_at"),
                "last_refresh_attempt_at": item.get("last_refresh_attempt_at"),
                "last_refresh_status": item.get("last_refresh_status"),
                "next_refresh_at": item.get("next_refresh_at"),
                "refresh_age_days": item.get("refresh_age_days"),
                "is_refresh_due": bool(item.get("is_refresh_due", True)),
                "commercial_value_score": int(item.get("commercial_value_score") or 5),
                "commercial_value_source": item.get("commercial_value_source") or "auto",
                "commercial_value_reason": item.get("commercial_value_reason") or "",
                "lifecycle_stage": item.get("lifecycle_stage") or "established",
                "observation_started_at": item.get("observation_started_at"),
                "observation_deadline_at": item.get("observation_deadline_at"),
                "discovery_candidate_id": item.get("discovery_candidate_id"),
                "auto_archive_locked": bool(item.get("auto_archive_locked")),
                "archive_reason_code": item.get("archive_reason_code"),
                "archive_reason_detail": item.get("archive_reason_detail"),
                "today_best": today_best,
                "coverage_days": stat.get("coverage_days", 0),
                "tracked_accounts": stat.get("tracked_accounts", 0),
                "article_count": stat.get("article_count", 0),
                "seo_status": "ranked" if ranked else "not_ranked",
            })
        groups.append({
            "group_id": group.get("group_id"),
            "label": group.get("label"),
            "order": group.get("order", 0),
            "keywords": keywords,
            "total": len(keywords),
            "ranked_count": sum(1 for item in keywords if item["today_best"]),
            "not_ranked_count": sum(1 for item in keywords if not item["today_best"]),
        })

    return {
        "groups": groups,
        "total": total,
        "ranked_total": ranked_total,
        "not_ranked_total": total - ranked_total,
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
    return _keyword_repo().archive_keyword(keyword_id=keyword_id)


def set_managed_keyword_refresh_policy(
    keyword_id: str,
    *,
    refresh_frequency_days: int | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    return _keyword_repo().set_refresh_policy(
        keyword_id,
        refresh_frequency_days=refresh_frequency_days,
        source=source,
    )


def set_managed_keyword_commercial_value(
    keyword_id: str,
    *,
    score: int,
    reason: str = "",
) -> dict[str, Any]:
    return _keyword_repo().set_commercial_value(
        keyword_id,
        score=score,
        reason=reason or "人工设定商业价值",
        source="manual",
    )


def set_managed_keyword_auto_archive_lock(
    keyword_id: str,
    *,
    locked: bool,
) -> dict[str, Any]:
    return _keyword_repo().set_auto_archive_lock(keyword_id, locked)


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
                "refresh_frequency_days": int(keyword.get("refresh_frequency_days") or 1),
                "last_refresh_at": keyword.get("last_refresh_at"),
                "next_refresh_at": keyword.get("next_refresh_at"),
            })
    return items


def save_batch_default_selection(items: list[dict[str, object]]) -> None:
    _keyword_repo().save_batch_default_selection(items)


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
