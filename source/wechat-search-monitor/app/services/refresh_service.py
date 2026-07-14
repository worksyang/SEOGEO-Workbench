from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from app.config import Config
from app.repositories.keyword_registry_repo import (
    KeywordRegistryRepository,
    effective_refresh_interval_hours,
)
from app.services.keyword_refresh_policy_service import (
    apply_auto_refresh_policies,
    load_policy_config,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CLIENT_SCRIPT = Path("/Users/works14/.skills-manager/skills/zk-wechat-search/scripts/wechat_search_client.py")
REBUILD_SCRIPT = PROJECT_ROOT / "scripts" / "rebuild_data.py"
ARTICLE_METRIC_SCRIPT = PROJECT_ROOT / "scripts" / "build_article_metric_observations.py"
START_BATCH_SCRIPT = PROJECT_ROOT / "scripts" / "start_keyword_batch.py"
OUTPUT_DIR = PROJECT_ROOT / "微信搜索结果" / "单词刷新"
BATCH_OUTPUT_DIR = PROJECT_ROOT / "微信搜索结果" / "批量抓取"
STATE_DIR = PROJECT_ROOT / "data" / "refresh_jobs"
BATCH_RUNS_ROOT = PROJECT_ROOT / "data" / "runs"
BATCH_TEMP_ROOT = PROJECT_ROOT / "data" / "tmp" / "batch_refresh"
LEDGER_PATH = PROJECT_ROOT / "data" / "state" / "keyword_refresh_ledger.json"
SERVER = os.environ.get("WX_SEARCH_SERVER", "http://192.168.31.238:8000")
BATCH_FINAL_STATUSES = {"completed", "completed_with_failures", "failed", "cancelled"}
BATCH_ACTIVE_STATUSES = {"starting", "running"}

STATE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
BATCH_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
BATCH_RUNS_ROOT.mkdir(parents=True, exist_ok=True)
BATCH_TEMP_ROOT.mkdir(parents=True, exist_ok=True)

# ── 单词刷新队列（线程安全） ──────────────────────────
_single_refresh_lock = threading.Lock()
_single_refresh_current: dict[str, Any] | None = None  # {keyword, job_id}
_single_refresh_queue: deque[dict[str, str]] = deque()   # [{keyword, keyword_id, job_id}]


class BatchAlreadyRunningError(RuntimeError):
    def __init__(self, state: dict[str, Any]) -> None:
        super().__init__("batch refresh already running")
        self.state = state


class SingleRefreshBusyError(RuntimeError):
    def __init__(self, current_keyword: str, queued_ahead: int = 0) -> None:
        msg = f"single refresh busy: {current_keyword}"
        super().__init__(msg)
        self.current_keyword = current_keyword
        self.queued_ahead = queued_ahead


def _state_path(job_id: str) -> Path:
    return STATE_DIR / f"{job_id}.json"


def _batch_dir(batch_id: str) -> Path:
    return BATCH_RUNS_ROOT / batch_id


def _batch_state_path(batch_id: str) -> Path:
    return _batch_dir(batch_id) / "state.json"


def _batch_launch_path(batch_id: str) -> Path:
    return _batch_dir(batch_id) / "launch.json"


def _batch_keywords_path(batch_id: str) -> Path:
    return BATCH_TEMP_ROOT / batch_id / "keywords.json"


def _batch_cancel_flag_path(batch_id: str) -> Path:
    return _batch_dir(batch_id) / "cancel.flag"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_state(job_id: str, payload: dict[str, Any]) -> None:
    _state_path(job_id).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_completed_keywords(batch_id: str) -> list[str]:
    """从 completed.jsonl 读取已完成的关键词文本列表。"""
    return [
        str(item.get("keyword") or "").strip()
        for item in _read_completed_items(batch_id)
        if str(item.get("keyword") or "").strip()
    ]


def _read_completed_items(batch_id: str) -> list[dict[str, Any]]:
    """读取成功条目及逐词完成时间，供公平调度使用。"""
    if not batch_id:
        return []
    completed_path = _batch_dir(batch_id) / "completed.jsonl"
    if not completed_path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in completed_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        kw = str(payload.get("keyword") or "").strip()
        if kw:
            items.append(payload)
    return items


def _load_keyword_catalog() -> list[dict[str, Any]]:
    payload = KeywordRegistryRepository(Config.SQLITE_PATH).load_payload()
    items: list[dict[str, Any]] = []
    for group in payload.get("groups", []):
        for keyword in group.get("keywords", []):
            keyword_id = str(keyword.get("keyword_id") or "").strip()
            keyword_text = str(keyword.get("keyword_text") or "").strip()
            if not keyword_id or not keyword_text:
                continue
            items.append({
                "group_id": group.get("group_id"),
                "group_label": group.get("label"),
                "group_order": group.get("order", 0),
                "keyword_id": keyword_id,
                "keyword_text": keyword_text,
                "batch_default_selected": bool(
                    keyword.get("batch_default_selected", True)
                ),
                "refresh_frequency_days": int(
                    keyword.get("refresh_frequency_days") or 1
                ),
                "refresh_frequency_source": keyword.get("refresh_frequency_source") or "auto",
                "last_refresh_at": keyword.get("last_refresh_at"),
                "last_refresh_attempt_at": keyword.get("last_refresh_attempt_at"),
                "last_refresh_status": keyword.get("last_refresh_status"),
                "next_refresh_at": keyword.get("next_refresh_at"),
                "created_at": keyword.get("created_at"),
                "lifecycle_stage": keyword.get("lifecycle_stage") or "established",
                "observation_started_at": keyword.get("observation_started_at"),
                "observation_deadline_at": keyword.get("observation_deadline_at"),
                "is_pinned": bool(keyword.get("is_pinned")),
            })
    return items


def _recent_keyword_duration_seconds(limit: int = 300) -> list[float]:
    """读取最近批次的逐词耗时，用事实吞吐量估算下一批容量。"""
    candidates: list[tuple[str, Path]] = []
    for state_path in BATCH_RUNS_ROOT.glob("*/state.json"):
        state = _read_json(state_path, default=None)
        if not isinstance(state, dict):
            continue
        if state.get("status") not in BATCH_FINAL_STATUSES:
            continue
        finished_at = str(state.get("finished_at") or state.get("updated_at") or "")
        candidates.append((finished_at, state_path.parent / "completed.jsonl"))

    durations: list[float] = []
    for _, completed_path in sorted(candidates, reverse=True):
        if not completed_path.exists():
            continue
        for line in reversed(completed_path.read_text(encoding="utf-8").splitlines()):
            try:
                payload = json.loads(line)
                duration = float(payload.get("duration_sec") or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if 10 <= duration <= 900:
                durations.append(duration)
            if len(durations) >= limit:
                return durations
    return durations


def _calculate_adaptive_batch_limit(
    configured_limit: int,
    durations: list[float],
    *,
    target_runtime_minutes: float,
    minimum_batch_size: int,
) -> dict[str, Any]:
    """按最近RPA吞吐量把单批控制在目标时长附近。"""
    configured = max(1, int(configured_limit))
    cleaned = [
        float(value)
        for value in durations
        if 10 <= float(value) <= 900
    ]
    if not cleaned:
        return {
            "configured_limit": configured,
            "effective_limit": configured,
            "median_keyword_seconds": None,
            "target_runtime_minutes": float(target_runtime_minutes),
            "sample_count": 0,
        }
    median_seconds = float(median(cleaned))
    estimated = int(max(1, float(target_runtime_minutes) * 60 / median_seconds))
    effective = min(
        configured,
        max(min(configured, max(1, int(minimum_batch_size))), estimated),
    )
    return {
        "configured_limit": configured,
        "effective_limit": effective,
        "median_keyword_seconds": round(median_seconds, 2),
        "target_runtime_minutes": float(target_runtime_minutes),
        "sample_count": len(cleaned),
    }


def _default_ledger() -> dict[str, Any]:
    return {
        "version": 2,
        "updated_at": _now(),
        "keywords": {},
        "daily_budget": {},
    }


def _read_ledger() -> dict[str, Any]:
    payload = _read_json(LEDGER_PATH, default=None)
    if not isinstance(payload, dict):
        return _default_ledger()
    payload.setdefault("version", 2)
    payload.setdefault("updated_at", _now())
    if not isinstance(payload.get("keywords"), dict):
        payload["keywords"] = {}
    if not isinstance(payload.get("daily_budget"), dict):
        payload["daily_budget"] = {}
    return payload


def _write_ledger(payload: dict[str, Any]) -> None:
    payload["updated_at"] = _now()
    _write_json(LEDGER_PATH, payload)


def _write_batch_state_patch(batch_id: str, patch: dict[str, Any]) -> None:
    state_path = _batch_state_path(batch_id)
    state = _read_json(state_path, default=None)
    if not isinstance(state, dict):
        return
    state.update(patch)
    state["updated_at"] = _now()
    _write_json(state_path, state)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _budget_bucket(
    ledger: dict[str, Any],
    day: str,
) -> dict[str, Any]:
    buckets = ledger.setdefault("daily_budget", {})
    bucket = buckets.get(day)
    if not isinstance(bucket, dict):
        bucket = {
            "scheduler_batches": {},
            "created_at": _now(),
        }
        buckets[day] = bucket
    if not isinstance(bucket.get("scheduler_batches"), dict):
        bucket["scheduler_batches"] = {}
    return bucket


def _scheduled_budget_used(ledger: dict[str, Any], day: str) -> int:
    bucket = _budget_bucket(ledger, day)
    batches = bucket.get("scheduler_batches") or {}
    return sum(
        max(0, int(item.get("reserved_count") or 0))
        for item in batches.values()
        if isinstance(item, dict)
    )


def get_refresh_budget_status(
    *,
    now: datetime | None = None,
    daily_keyword_budget: int | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    policy = load_policy_config()
    budget = int(
        daily_keyword_budget
        if daily_keyword_budget is not None
        else policy["scheduled_keyword_budget"]
    )
    ledger = _read_ledger()
    day = now.date().isoformat()
    used = _scheduled_budget_used(ledger, day)
    return {
        "date": day,
        "total_daily_budget": int(policy["daily_keyword_budget"]),
        "budget_type": "scheduled",
        "daily_keyword_budget": budget,
        "reserved_count": used,
        "remaining_count": max(0, budget - used),
        "discovery_reserved_count": int(policy["discovery_daily_search_budget"]),
        "manual_reserved_count": int(policy["manual_reserve_budget"]),
    }


def _is_due_for_scheduled_refresh(
    item: dict[str, Any],
    *,
    now: datetime,
    failure_retry_hours: int,
) -> tuple[bool, float]:
    """返回 (是否到期, 用于公平排序的逾期周期数)。"""
    last_attempt = _parse_datetime(item.get("last_refresh_attempt_at"))
    if (
        item.get("last_refresh_status") == "failed"
        and last_attempt is not None
        and now < last_attempt + timedelta(hours=failure_retry_hours)
    ):
        return False, -1.0

    last_refresh = _parse_datetime(item.get("last_refresh_at"))
    interval_hours = effective_refresh_interval_hours(item)
    if last_refresh is None:
        # 未抓取的词必须先获得完整观察期；创建越早，优先级越高。
        created_at = _parse_datetime(item.get("created_at"))
        age_days = (now - created_at).total_seconds() / 86400 if created_at else 0.0
        return True, 1_000_000 + age_days

    due_at = last_refresh + timedelta(hours=interval_hours)
    if now < due_at:
        return False, -1.0
    overdue_cycles = (now - due_at).total_seconds() / max(1, interval_hours * 3600)
    return True, overdue_cycles


def get_incremental_keywords(
    *,
    daily_keyword_budget: int | None = None,
    max_keywords_per_batch: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """返回预算内、已到期的自动刷新词。

    自动批跑不再按“把全词库刷完就开新一轮”循环，而是按每个词自己的
    有效刷新间隔判断是否到期。日内多次抓取只是给指标层更多时间切片；
    指标层应按日中位数/时间归一化消费这些切片，调度器不重复放大指标。
    """
    now = now or datetime.now()
    policy = load_policy_config()
    daily_budget = int(
        daily_keyword_budget
        if daily_keyword_budget is not None
        else policy["scheduled_keyword_budget"]
    )
    configured_batch_limit = int(
        max_keywords_per_batch
        if max_keywords_per_batch is not None
        else policy["max_keywords_per_batch"]
    )
    batch_capacity = _calculate_adaptive_batch_limit(
        configured_batch_limit,
        _recent_keyword_duration_seconds(),
        target_runtime_minutes=float(
            policy.get("target_batch_runtime_minutes") or 180
        ),
        minimum_batch_size=int(policy.get("minimum_adaptive_batch_size") or 20),
    )
    batch_limit = int(batch_capacity["effective_limit"])

    auto_summary = apply_auto_refresh_policies()
    catalog = _load_keyword_catalog()
    if not catalog:
        return {
            "keywords": [],
            "total_keywords": 0,
            "current_round": None,
            "round_started_at": None,
            "new_round_started": False,
            "due_count": 0,
            "deferred_due_count": 0,
            "budget": get_refresh_budget_status(
                now=now,
                daily_keyword_budget=daily_budget,
            ),
            "batch_capacity": batch_capacity,
            "auto_policy": auto_summary,
        }

    due_with_priority: list[tuple[float, dict[str, Any]]] = []
    for item in catalog:
        is_due, overdue_cycles = _is_due_for_scheduled_refresh(
            item,
            now=now,
            failure_retry_hours=int(policy["failure_retry_hours"]),
        )
        if is_due:
            due_with_priority.append((overdue_cycles, item))

    due_with_priority.sort(
        key=lambda pair: (
            -pair[0],
            int(pair[1].get("refresh_frequency_days") or 1),
            str(pair[1].get("last_refresh_at") or ""),
            str(pair[1].get("keyword_text") or ""),
        )
    )
    budget = get_refresh_budget_status(now=now, daily_keyword_budget=daily_budget)
    selected_limit = min(batch_limit, int(budget["remaining_count"]))
    due = [item for _, item in due_with_priority]
    selected = due[:selected_limit]

    return {
        "keywords": selected,
        "total_keywords": len(catalog),
        "current_round": None,
        "round_started_at": None,
        "new_round_started": False,
        "due_count": len(due),
        "deferred_due_count": max(0, len(due) - len(selected)),
        "budget": budget,
        "batch_capacity": batch_capacity,
        "auto_policy": auto_summary,
    }


def _update_ledger(batch_id: str) -> None:
    if not batch_id:
        return
    state_path = _batch_state_path(batch_id)
    state = _read_json(state_path, default=None)
    if not isinstance(state, dict):
        return
    if state.get("ledger_updated"):
        return

    catalog = _load_keyword_catalog()
    by_text = {item["keyword_text"]: item for item in catalog}
    ledger = _read_ledger()
    records = ledger.setdefault("keywords", {})
    completed_items = _read_completed_items(batch_id)
    completed_keywords = [
        str(item.get("keyword") or "").strip()
        for item in completed_items
        if str(item.get("keyword") or "").strip()
    ]
    failed_items = _read_failed_keywords(batch_id)
    failed_keywords = [item["keyword"] for item in failed_items]
    refreshed_at = str(state.get("finished_at") or _now())
    updated_count = 0
    succeeded_at_by_id: dict[str, str] = {}
    failed_ids: list[str] = []

    for completed_item in completed_items:
        keyword_text = str(completed_item.get("keyword") or "").strip()
        item = by_text.get(keyword_text)
        if not item:
            continue
        keyword_id = str(item.get("keyword_id") or "").strip()
        if not keyword_id:
            continue
        item_refreshed_at = str(
            completed_item.get("finished_at")
            or completed_item.get("started_at")
            or refreshed_at
        )
        succeeded_at_by_id[keyword_id] = item_refreshed_at
        records[keyword_id] = {
            "keyword_text": item.get("keyword_text"),
            "last_refreshed_at": item_refreshed_at,
            "last_batch_id": batch_id,
        }
        updated_count += 1
    for keyword_text in failed_keywords:
        item = by_text.get(keyword_text)
        if item and item.get("keyword_id"):
            failed_ids.append(str(item["keyword_id"]))

    refresh_result = KeywordRegistryRepository(Config.SQLITE_PATH).record_refresh_events(
        succeeded_at_by_id=succeeded_at_by_id,
        failed_keyword_ids=failed_ids,
        refreshed_at=refreshed_at,
    )

    if state.get("source") == "scheduler":
        date = str(state.get("scheduled_budget_date") or state.get("started_at") or "")[:10]
        if date:
            bucket = _budget_bucket(ledger, date)
            batches = bucket["scheduler_batches"]
            batch_record = batches.get(batch_id)
            if isinstance(batch_record, dict):
                batch_record["reserved_count"] = len(completed_keywords) + len(failed_keywords)
                batch_record["actual_count"] = len(completed_keywords) + len(failed_keywords)
                batch_record["finalized_at"] = refreshed_at

    _write_ledger(ledger)
    _write_batch_state_patch(batch_id, {
        "ledger_updated": True,
        "ledger_updated_at": refreshed_at,
        "ledger_updated_count": updated_count,
        "refresh_result": refresh_result,
    })


def _reserve_scheduler_budget(
    *,
    batch_id: str,
    keyword_ids: list[str],
    daily_keyword_budget: int | None = None,
) -> dict[str, Any]:
    """为已成功启动的自动批次预留当天额度，防止下一轮调度超额。"""
    policy = load_policy_config()
    budget = int(
        daily_keyword_budget
        if daily_keyword_budget is not None
        else policy["scheduled_keyword_budget"]
    )
    day = datetime.now().date().isoformat()
    ledger = _read_ledger()
    used = _scheduled_budget_used(ledger, day)
    count = len(keyword_ids)
    if used + count > budget:
        raise ValueError(
            f"scheduler daily budget exceeded: {used}+{count}>{budget}"
        )
    bucket = _budget_bucket(ledger, day)
    bucket["scheduler_batches"][batch_id] = {
        "reserved_count": count,
        "keyword_ids": keyword_ids,
        "reserved_at": _now(),
    }
    _write_ledger(ledger)
    return {
        "date": day,
        "daily_keyword_budget": budget,
        "reserved_count": used + count,
        "remaining_count": budget - used - count,
    }


def _normalize_batch_state(state: dict[str, Any], launch: dict[str, Any] | None = None) -> dict[str, Any]:
    launch = launch or {}
    batch_id = str(state.get("batch_id") or launch.get("batch_id") or "").strip()
    status = str(state.get("status") or "unknown").strip() or "unknown"
    total = int(state.get("total_keywords") or state.get("total") or 0)
    success_count = int(state.get("success_count") or state.get("done") or 0)
    failed_count = int(state.get("failed_count") or state.get("failed") or 0)
    processed = min(total, success_count + failed_count) if total else success_count + failed_count
    pending_count = state.get("pending_count")
    if pending_count is None:
        pending_count = max(total - processed, 0)
    pending_count = int(pending_count or 0)
    current_keyword = state.get("current_keyword")
    runner_pid = state.get("runner_pid") or launch.get("runner_pid")
    runner_alive = _is_pid_alive(int(runner_pid)) if runner_pid else False
    cancel_requested = bool(state.get("cancel_requested"))
    cancel_requested_at = state.get("cancel_requested_at")
    cancelled_at = state.get("cancelled_at")
    cancel_reason = str(state.get("cancel_reason") or "")
    is_active = status in BATCH_ACTIVE_STATUSES and (runner_alive or not runner_pid or status == "starting")
    finished = status in BATCH_FINAL_STATUSES
    if finished and batch_id and not state.get("ledger_updated"):
        _update_ledger(batch_id)
        state = _read_json(_batch_state_path(batch_id), default=state) or state
    completed_keywords = _read_completed_keywords(batch_id)
    failed_keywords = _read_failed_keywords(batch_id)
    return {
        "batch_id": batch_id,
        "status": status,
        "total": total,
        "success_count": success_count,
        "failed_count": failed_count,
        "processed_count": processed,
        "pending_count": pending_count,
        "current_keyword": current_keyword,
        "current_item_id": state.get("current_item_id"),
        "current_attempt": state.get("current_attempt"),
        "completed_keywords": completed_keywords,
        "failed_keywords": failed_keywords,
        "started_at": state.get("started_at") or launch.get("launched_at"),
        "finished_at": state.get("finished_at"),
        "updated_at": state.get("updated_at") or launch.get("launched_at"),
        "heartbeat_at": state.get("heartbeat_at"),
        "runner_pid": runner_pid,
        "runner_alive": runner_alive,
        "cancel_requested": cancel_requested,
        "cancel_requested_at": cancel_requested_at,
        "cancelled_at": cancelled_at,
        "cancel_reason": cancel_reason,
        "is_active": is_active,
        "is_finished": finished,
        "batch_dir": str(_batch_dir(batch_id)) if batch_id else None,
    }


def _iter_batch_dirs() -> list[Path]:
    return sorted(
        [path for path in BATCH_RUNS_ROOT.iterdir() if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _make_batch_id() -> str:
    return f"web_{time.strftime('%Y%m%d_%H%M%S')}"


def _write_batch_keywords_file(batch_id: str, selected_keywords: list[dict[str, Any]]) -> Path:
    groups: list[dict[str, Any]] = []
    group_index: dict[str, dict[str, Any]] = {}
    for item in selected_keywords:
        keyword_id = str(item.get("keyword_id") or "").strip()
        keyword_text = str(item.get("keyword_text") or "").strip()
        if not keyword_id or not keyword_text:
            continue
        group_key = str(item.get("group_id") or item.get("group_label") or "ungrouped")
        group = group_index.get(group_key)
        if group is None:
            group = {
                "group_id": item.get("group_id") or group_key,
                "label": item.get("group_label") or "未分组",
                "order": int(item.get("group_order") or 0),
                "keywords": [],
            }
            group_index[group_key] = group
            groups.append(group)
        group["keywords"].append({
            "keyword_id": keyword_id,
            "keyword_text": keyword_text,
            "enabled": True,
            "note": "",
        })

    payload = {
        "updated_at": _now(),
        "groups": groups,
    }
    keywords_path = _batch_keywords_path(batch_id)
    _write_json(keywords_path, payload)
    return keywords_path


def get_job_status(job_id: str) -> dict[str, Any] | None:
    p = _state_path(job_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def get_batch_status(batch_id: str) -> dict[str, Any] | None:
    batch_id = str(batch_id or "").strip()
    if not batch_id:
        return None
    state = _read_json(_batch_state_path(batch_id), default=None)
    if not isinstance(state, dict):
        return None
    launch = _read_json(_batch_launch_path(batch_id), default={}) or {}
    return _normalize_batch_state(state, launch)


def get_active_batch_status() -> dict[str, Any] | None:
    for batch_dir in _iter_batch_dirs():
        state = _read_json(batch_dir / "state.json", default=None)
        if not isinstance(state, dict):
            continue
        launch = _read_json(batch_dir / "launch.json", default={}) or {}
        normalized = _normalize_batch_state(state, launch)
        if normalized.get("is_active"):
            return normalized
    return None


def cancel_batch(batch_id: str) -> dict[str, Any]:
    batch_id = str(batch_id or "").strip()
    if not batch_id:
        raise ValueError("batch_id is required")

    state_path = _batch_state_path(batch_id)
    state = _read_json(state_path, default=None)
    if not isinstance(state, dict):
        raise FileNotFoundError(f"batch not found: {batch_id}")

    launch = _read_json(_batch_launch_path(batch_id), default={}) or {}
    status = str(state.get("status") or "unknown").strip() or "unknown"
    if status in BATCH_FINAL_STATUSES:
        return _normalize_batch_state(state, launch)

    cancel_flag = _batch_cancel_flag_path(batch_id)
    cancel_flag.parent.mkdir(parents=True, exist_ok=True)
    cancel_flag.write_text("", encoding="utf-8")

    if not state.get("cancel_requested"):
        requested_at = _now()
        state["cancel_requested"] = True
        state["cancel_requested_at"] = requested_at
        state["updated_at"] = requested_at
        _write_json(state_path, state)

    return _normalize_batch_state(state, launch)


def _run_single(keyword: str, job_id: str, keyword_id: str = "") -> None:
    global _single_refresh_current
    kw_output = OUTPUT_DIR / keyword
    kw_output.mkdir(parents=True, exist_ok=True)
    started_at = _now()
    _write_state(job_id, {"job_id": job_id, "keyword": keyword, "status": "running", "started_at": started_at})

    cmd = [
        sys.executable,
        str(CLIENT_SCRIPT),
        keyword,
        "--server",
        SERVER,
        "--fetch-depth",
        "1",
        "--fetch-max-count",
        "5",
        "--timeout",
        "480",
        "--output-dir",
        str(kw_output),
        "--no-ai-summary",
    ]
    ok = False
    failure_stage = "fetch"
    failure_reason = ""
    try:
        result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=600)
        ok = result.returncode == 0
        if not ok:
            failure_stage = "fetch"
            failure_reason = (result.stderr or result.stdout or "").strip()[-1000:]
        if ok:
            failure_stage = "rebuild"
            rebuild_result = subprocess.run(
                [sys.executable, str(REBUILD_SCRIPT)],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=300,
            )
            ok = rebuild_result.returncode == 0
            if not ok:
                failure_reason = (rebuild_result.stderr or rebuild_result.stdout or "").strip()[-1000:]
        if ok:
            failure_stage = "article_metrics"
            metric_result = subprocess.run(
                [sys.executable, str(ARTICLE_METRIC_SCRIPT)],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=600,
            )
            ok = metric_result.returncode == 0
            if not ok:
                failure_reason = (metric_result.stderr or metric_result.stdout or "").strip()[-1000:]
    except subprocess.TimeoutExpired as exc:
        ok = False
        failure_reason = f"{failure_stage or 'fetch'} timed out after {exc.timeout}s"
    except Exception as exc:
        ok = False
        failure_reason = f"{type(exc).__name__}: {exc}"
    finally:
        state = {
            "job_id": job_id,
            "keyword": keyword,
            "status": "done" if ok else "failed",
            "started_at": started_at,
            "finished_at": _now(),
            "success": ok,
        }
        if not ok:
            state["failure_stage"] = failure_stage or "unknown"
            state["error"] = failure_reason or "unknown error"
        try:
            if keyword_id:
                KeywordRegistryRepository(Config.SQLITE_PATH).record_refresh_results(
                    succeeded_keyword_ids=[keyword_id] if ok else [],
                    failed_keyword_ids=[] if ok else [keyword_id],
                    refreshed_at=state["finished_at"],
                )
            _write_state(job_id, state)
        finally:
            _drain_single_refresh_queue()


def _drain_single_refresh_queue() -> None:
    global _single_refresh_current
    with _single_refresh_lock:
        _single_refresh_current = None
        if _single_refresh_queue:
            next_item = _single_refresh_queue.popleft()
            _single_refresh_current = {
                "keyword": next_item["keyword"],
                "keyword_id": next_item.get("keyword_id", ""),
                "job_id": next_item["job_id"],
            }
        else:
            return
    # 标记排队中的 job 为 queued → running
    _write_state(next_item["job_id"], {
        "job_id": next_item["job_id"],
        "keyword": next_item["keyword"],
        "status": "queued_to_running",
        "started_at": _now(),
    })
    t = threading.Thread(
        target=_run_single,
        args=(next_item["keyword"], next_item["job_id"], next_item.get("keyword_id", "")),
        daemon=True,
    )
    t.start()


def start_single_refresh(keyword: str, keyword_id: str = "") -> dict[str, Any]:
    """启动单词刷新。如果当前有任务在跑，自动排队。

    返回:
      {"job_id": ..., "status": "running" | "queued", "keyword": ...,
       "current": "...", "queued_ahead": N}
    """
    global _single_refresh_current
    job_id = uuid.uuid4().hex[:12]

    with _single_refresh_lock:
        # 检查批量刷新是否在跑
        active_batch = get_active_batch_status()
        if active_batch:
            return {
                "job_id": job_id,
                "status": "rejected",
                "keyword": keyword,
                "reason": "batch_running",
                "current": active_batch.get("current_keyword") or "批量刷新",
            }

        if _single_refresh_current is not None:
            # 当前有单词刷新在跑，排队
            _single_refresh_queue.append({
                "keyword": keyword,
                "keyword_id": keyword_id,
                "job_id": job_id,
            })
            queued_ahead = len(_single_refresh_queue) - 1
            _write_state(job_id, {
                "job_id": job_id,
                "keyword": keyword,
                "status": "queued",
                "started_at": _now(),
                "queued_behind": _single_refresh_current["keyword"],
                "queued_ahead": queued_ahead,
            })
            return {
                "job_id": job_id,
                "status": "queued",
                "keyword": keyword,
                "current": _single_refresh_current["keyword"],
                "queued_ahead": queued_ahead,
            }
        else:
            # 没有任务在跑，立即启动
            _single_refresh_current = {
                "keyword": keyword,
                "keyword_id": keyword_id,
                "job_id": job_id,
            }

    t = threading.Thread(target=_run_single, args=(keyword, job_id, keyword_id), daemon=True)
    t.start()
    return {
        "job_id": job_id,
        "status": "running",
        "keyword": keyword,
        "current": keyword,
        "queued_ahead": 0,
    }


def get_single_refresh_status() -> dict[str, Any] | None:
    """返回当前单词刷新状态，供批量刷新互斥检查用。"""
    with _single_refresh_lock:
        if _single_refresh_current is None and not _single_refresh_queue:
            return None
        return {
            "current": _single_refresh_current["keyword"] if _single_refresh_current else None,
            "queue_length": len(_single_refresh_queue),
        }


def start_batch_refresh(
    selected_keywords: list[dict[str, Any]],
    source: str = "web_refresh_all",
    refresh_round: int | None = None,
    *,
    fetch_depth: int = 1,
    fetch_max_count: int = 5,
    timeout: int = 480,
    rebuild_every: int = 10,
) -> dict[str, Any]:
    active = get_active_batch_status()
    if active:
        raise BatchAlreadyRunningError(active)

    single_status = get_single_refresh_status()
    if single_status:
        raise BatchAlreadyRunningError({
            "status": "single_refresh_running",
            "current_keyword": single_status.get("current"),
            "queue_length": single_status.get("queue_length", 0),
        })

    cleaned = []
    seen_ids: set[str] = set()
    for item in selected_keywords:
        keyword_id = str(item.get("keyword_id") or "").strip()
        keyword_text = str(item.get("keyword_text") or "").strip()
        if not keyword_id or not keyword_text or keyword_id in seen_ids:
            continue
        seen_ids.add(keyword_id)
        cleaned.append({
            "group_id": item.get("group_id"),
            "group_label": item.get("group_label"),
            "group_order": item.get("group_order", 0),
            "keyword_id": keyword_id,
            "keyword_text": keyword_text,
        })

    if not cleaned:
        raise ValueError("no selected keywords")
    if int(fetch_depth) not in {0, 1}:
        raise ValueError("fetch_depth must be 0 or 1")

    if not START_BATCH_SCRIPT.exists():
        raise FileNotFoundError(f"batch launcher not found: {START_BATCH_SCRIPT}")

    batch_id = _make_batch_id()
    batch_dir = _batch_dir(batch_id)
    batch_dir.mkdir(parents=True, exist_ok=True)
    keywords_file = _write_batch_keywords_file(batch_id, cleaned)
    try:
        import shutil
        shutil.copyfile(keywords_file, batch_dir / "keywords.json")
    except OSError:
        pass

    bootstrap_state = {
        "batch_id": batch_id,
        "status": "starting",
        "started_at": _now(),
        "finished_at": None,
        "total_keywords": len(cleaned),
        "success_count": 0,
        "failed_count": 0,
        "pending_count": len(cleaned),
        "current_item_id": None,
        "current_keyword": None,
        "current_attempt": None,
        "source": source,
        "refresh_round": refresh_round,
        "selected_keyword_ids": [item["keyword_id"] for item in cleaned],
        "fetch_depth": int(fetch_depth),
        "fetch_max_count": max(0, int(fetch_max_count)),
    }
    _write_json(_batch_state_path(batch_id), bootstrap_state)

    cmd = [
        sys.executable,
        str(START_BATCH_SCRIPT),
        "--batch-id",
        batch_id,
        "--keywords-file",
        str(keywords_file),
        "--runs-root",
        str(BATCH_RUNS_ROOT),
        "--output-root",
        str(BATCH_OUTPUT_DIR),
        "--server",
        SERVER,
        "--fetch-depth",
        str(int(fetch_depth)),
        "--fetch-max-count",
        str(max(0, int(fetch_max_count))),
        "--timeout",
        str(max(30, int(timeout))),
        "--max-attempts",
        "2",
        "--retry-sleep",
        "20",
        "--rebuild-every",
        str(max(0, int(rebuild_every))),
        "--resume",
    ]

    result = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        failed_state = {
            **bootstrap_state,
            "status": "failed",
            "finished_at": _now(),
            "error": (result.stderr or result.stdout).strip() or f"launcher exited with {result.returncode}",
        }
        _write_json(_batch_state_path(batch_id), failed_state)
        raise RuntimeError(failed_state["error"])

    if source == "scheduler":
        budget = _reserve_scheduler_budget(
            batch_id=batch_id,
            keyword_ids=[item["keyword_id"] for item in cleaned],
        )
        _write_batch_state_patch(batch_id, {
            "scheduled_budget_date": budget["date"],
            "scheduled_budget_reserved_count": len(cleaned),
            "scheduled_budget_remaining_count": budget["remaining_count"],
        })

    state = get_batch_status(batch_id)
    if state:
        return state
    return _normalize_batch_state(bootstrap_state)


def _classify_failure(stderr_tail: str, diagnostic_summary: str = "") -> str:
    """从 stderr 尾部文本归类失败原因。"""
    diagnostic_summary = (diagnostic_summary or "").strip()
    if diagnostic_summary:
        return diagnostic_summary
    text = (stderr_tail or "").strip()
    if not text:
        return "未知错误"
    if "无法连接" in text or "Connection refused" in text or "ConnectError" in text:
        return "对方电脑掉线"
    if "超时" in text or "timeout" in text or "Timeout" in text:
        return "请求超时"
    if "500" in text or "502" in text or "503" in text:
        return "搜索服务异常"
    return text[:80]


def _read_failed_reasons(batch_id: str, limit: int = 3) -> list[str]:
    """读取 failed.jsonl，返回去重后的失败原因列表（最多 limit 条）。"""
    if not batch_id:
        return []
    failed_path = _batch_dir(batch_id) / "failed.jsonl"
    if not failed_path.exists():
        return []
    reasons: list[str] = []
    seen: set[str] = set()
    for line in failed_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        reason = _classify_failure(
            payload.get("stderr_tail") or "",
            str(payload.get("diagnostic_summary") or ""),
        )
        if reason not in seen:
            seen.add(reason)
            reasons.append(reason)
        if len(reasons) >= limit:
            break
    return reasons


def _read_failed_keywords(batch_id: str) -> list[dict[str, str]]:
    """读取 failed.jsonl，返回每个失败关键词的文本和归类原因。"""
    if not batch_id:
        return []
    failed_path = _batch_dir(batch_id) / "failed.jsonl"
    if not failed_path.exists():
        return []
    items: list[dict[str, str]] = []
    for line in failed_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        kw = str(payload.get("keyword") or "").strip()
        if not kw:
            continue
        reason = _classify_failure(
            payload.get("stderr_tail") or "",
            str(payload.get("diagnostic_summary") or ""),
        )
        items.append({"keyword": kw, "reason": reason})
    return items


def list_batch_history(limit: int = 20) -> list[dict[str, Any]]:
    """返回最近的批次历史列表，按开始时间倒序。"""
    history: list[dict[str, Any]] = []
    for batch_dir in _iter_batch_dirs()[:limit]:
        state = _read_json(batch_dir / "state.json", default=None)
        if not isinstance(state, dict):
            continue
        batch_id = str(state.get("batch_id") or batch_dir.name)
        status = str(state.get("status") or "unknown")
        failed_count = int(state.get("failed_count") or 0)
        failure_reasons: list[str] = []
        failed_keywords: list[dict[str, str]] = []
        if failed_count > 0:
            failure_reasons = _read_failed_reasons(batch_id)
            failed_keywords = _read_failed_keywords(batch_id)
        history.append({
            "batch_id": batch_id,
            "status": status,
            "total": int(state.get("total_keywords") or 0),
            "success_count": int(state.get("success_count") or 0),
            "failed_count": failed_count,
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "failure_reasons": failure_reasons,
            "failed_keywords": failed_keywords,
            "cancel_reason": str(state.get("cancel_reason") or ""),
            "source": str(state.get("source") or "web_refresh_all"),
            "refresh_round": state.get("refresh_round"),
        })
    return history
