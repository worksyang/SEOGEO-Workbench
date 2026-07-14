from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import current_app

from app.ingest.rebuild import rebuild_all
from app.keyword_bucket_resolver import DEFAULT_BUCKET, bucket_options
from app.repositories.keyword_registry_repo import KeywordRegistryRepository
from app.repositories.monitor_data_repo import MonitorDataRepository
from app.services.article_content_service import resolve_article_markdown_payload


def _keyword_registry_repo() -> KeywordRegistryRepository:
    return KeywordRegistryRepository(current_app.config["SQLITE_PATH"])


def _monitor_data_repo() -> MonitorDataRepository:
    return MonitorDataRepository(current_app.config["MONITOR_DATA_FILE"])


def _load_keyword_read_delta_rows() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    path = Path(current_app.config["NORMALIZED_DIR"]) / "keyword_read_deltas.json"
    if not path.exists():
        return {}, {
            "available": False,
            "row_count": 0,
            "source": str(path),
        }

    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("items") or raw.get("keywords") or []
    by_id: dict[str, dict[str, Any]] = {}
    status_counts: dict[str, int] = {}
    first_row = rows[0] if rows else {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        keyword_id = str(row.get("keyword_id") or "").strip()
        if keyword_id:
            by_id[keyword_id] = dict(row)
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    meta_path = Path(current_app.config["NORMALIZED_DIR"]) / "article_metric_observations_meta.json"
    generated_at = None
    if meta_path.exists():
        try:
            generated_at = json.loads(meta_path.read_text(encoding="utf-8")).get("generated_at")
        except (OSError, json.JSONDecodeError):
            generated_at = None

    return by_id, {
        "available": True,
        "row_count": len(by_id),
        "source": str(path),
        "generated_at": generated_at,
        "window_start": first_row.get("window_start"),
        "window_end": first_row.get("window_end"),
        "window_days": first_row.get("window_days"),
        "method": first_row.get("method"),
        "status_counts": status_counts,
    }


def _insufficient_keyword_read_delta(item: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "keyword_id": item.get("keyword_id") or "",
        "keyword": item.get("keyword") or "",
        "window_start": meta.get("window_start"),
        "window_end": meta.get("window_end"),
        "window_days": meta.get("window_days"),
        "method": meta.get("method") or "schedule_adjusted_read_rate_v3",
        "status": "insufficient_data",
        "read_delta_estimated": None,
        "read_delta_raw": None,
        "steady_read_median": None,
        "legacy_steady_read_median": None,
        "confidence_score": 0,
        "confidence_level": "insufficient",
        "trend_signal": 0,
        "trend_label": "观察中",
        "insufficient_reason": "not_in_keyword_read_deltas",
    }


def _empty_keyword_payload(item: dict[str, Any], group: dict[str, Any], window_days: int) -> dict[str, Any]:
    keyword_text = str(item.get("keyword_text") or "").strip()
    keyword_id = str(item.get("keyword_id") or "").strip()
    return {
        "keyword": keyword_text,
        "keyword_id": keyword_id,
        "topic": keyword_text,
        "keyword_bucket": group.get("label") or DEFAULT_BUCKET,
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
        "kw_score": {
            "total": 0,
            "heat": 0,
            "breadth": 0,
            "richness": 0,
            "has_heat": False,
        },
    }


def _apply_keyword_config_scope(data: dict) -> dict:
    payload = _keyword_registry_repo().load_payload()
    monitor_by_id = {
        item.get("keyword_id"): item
        for item in data.get("keywords", [])
        if item.get("keyword_id")
    }
    window_days = int(data.get("window_days") or 15)
    keywords = []
    for group in payload.get("groups", []):
        for item in group.get("keywords", []):
            keyword_id = item.get("keyword_id")
            merged = dict(monitor_by_id.get(keyword_id) or _empty_keyword_payload(item, group, window_days))
            merged["keyword_id"] = keyword_id
            merged["keyword"] = item.get("keyword_text", merged.get("keyword", ""))
            merged["keyword_bucket"] = merged.get("keyword_bucket") or group.get("label") or DEFAULT_BUCKET
            keywords.append(merged)
    return {**data, "keywords": keywords}


def _merge_keyword_states(data: dict, settings: dict[str, dict]) -> dict:
    keywords = []
    for item in data.get("keywords", []):
        merged = dict(item)
        state = settings.get(item["keyword_id"], {})
        merged["is_pinned"] = bool(state.get("is_pinned", False))
        merged["pin_order"] = state.get("pin_order")
        merged["topic"] = state.get("topic") or merged.get("topic") or merged.get("keyword")
        merged["keyword_bucket"] = (
            state.get("keyword_bucket")
            or merged.get("keyword_bucket")
            or DEFAULT_BUCKET
        )
        keywords.append(merged)

    return {
        **data,
        "keywords": keywords,
    }


def _merge_keyword_read_deltas(data: dict) -> dict:
    deltas_by_id, meta = _load_keyword_read_delta_rows()
    keywords = []
    for item in data.get("keywords", []):
        merged = dict(item)
        if meta.get("available"):
            row = deltas_by_id.get(str(item.get("keyword_id") or ""))
            merged["keyword_read_delta"] = row or _insufficient_keyword_read_delta(merged, meta)
        keywords.append(merged)
    return {
        **data,
        "keywords": keywords,
        "keyword_read_delta_meta": meta,
    }


def _sort_keywords(keywords: list[dict]) -> list[dict]:
    # 排序规则：置顶 > 有热度（按 heat 倒排）> 无热度（按 richness 倒排）。
    # 有热度指 WSO 或 DSO 任一通道存在真实搜索量；广度只展示，不参与排序。
    def sort_key(item: dict):
        score = item.get("kw_score") or {}
        if item.get("is_pinned"):
            segment = 0
        elif score.get("has_heat"):
            segment = 1
        else:
            segment = 2

        if segment == 1:
            inner_metric = -float(score.get("heat") or 0)
        else:
            inner_metric = -float(score.get("richness") or 0)

        return (segment, inner_metric, item.get("keyword", ""))

    keywords.sort(key=sort_key)
    return keywords


def load_monitor_payload() -> dict:
    from app.services.monitor_fast_service import try_get_fast_store

    fast_store = try_get_fast_store()
    if fast_store is not None:
        return fast_store.get_full_payload()

    data = _monitor_data_repo().load()
    data = _apply_keyword_config_scope(data)
    settings = _keyword_registry_repo().list_settings()
    data = _merge_keyword_states(data, settings)
    data = _merge_keyword_read_deltas(data)
    keywords = _sort_keywords(data["keywords"])
    data["keywords"] = keywords
    data["pinned_keyword_count"] = sum(1 for item in keywords if item.get("is_pinned"))
    data["keyword_bucket_options"] = bucket_options()
    data["keyword_scope"] = "configured"
    data["keyword_source_total"] = len(keywords)
    return data


def set_keyword_pin(keyword_id: str, keyword_text: str, is_pinned: bool) -> dict:
    return _keyword_registry_repo().set_pin_state(
        keyword_id=keyword_id,
        keyword_text=keyword_text,
        is_pinned=is_pinned,
    )


def set_keyword_topic(keyword_id: str, keyword_text: str, topic: str | None) -> dict:
    state = _keyword_registry_repo().set_topic(
        keyword_id=keyword_id,
        keyword_text=keyword_text,
        topic=topic,
    )
    rebuild_all(verbose=False)
    return state


def set_keyword_bucket(keyword_id: str, keyword_text: str, keyword_bucket: str | None) -> dict:
    state = _keyword_registry_repo().set_bucket(
        keyword_id=keyword_id,
        keyword_text=keyword_text,
        keyword_bucket=keyword_bucket,
    )
    rebuild_all(verbose=False)
    return state


def set_keyword_note(keyword_id: str, keyword_text: str, note: str) -> dict:
    return _keyword_registry_repo().set_note(
        keyword_id=keyword_id,
        keyword_text=keyword_text,
        note=note,
    )


def resolve_article_markdown(content_path: str) -> dict:
    return resolve_article_markdown_payload(
        project_root=Path(current_app.config["PROJECT_ROOT"]),
        content_path=content_path,
    )
