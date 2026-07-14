"""自动批量刷新调度器 — Flask 进程内 daemon thread，通过 HTTP 触发本地 /api/refresh-all。"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from app.services.refresh_service import (
    get_active_batch_status,
    get_incremental_keywords,
    get_refresh_budget_status,
)
from app.services.keyword_refresh_policy_service import load_policy_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PERSIST_PATH = PROJECT_ROOT / "data" / "state" / "scheduler.json"

_lock = threading.Lock()
_stop_event = threading.Event()
_trigger_event = threading.Event()
_thread: threading.Thread | None = None

# 调度器全局状态（供 API 读取）
_state: dict[str, Any] = {
    "enabled": False,
    "interval_hours": 3.0,
    "base_url": "http://127.0.0.1:8765",
    "next_run_at": None,
    "last_triggered_at": None,
    "last_result": None,
    "daily_keyword_budget": 1550,
    "max_keywords_per_batch": 250,
    "last_plan": None,
    "last_discovery": None,
}


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _advance_fixed_clock(
    scheduled_at: datetime,
    *,
    now: datetime,
    interval_hours: float,
) -> datetime:
    """从原定槽位推进到下一个未来槽位，跳过执行期间错过的槽位。"""
    interval = timedelta(hours=max(0.001, float(interval_hours)))
    next_slot = scheduled_at + interval
    while next_slot <= now:
        next_slot += interval
    return next_slot


def _budget_policy() -> dict[str, Any]:
    return load_policy_config(PROJECT_ROOT)


def _summarize_auto_policy(auto_policy: Any) -> dict[str, Any]:
    """保留调度看板需要的汇总，避免把逐词指标塞进状态 API。"""
    if not isinstance(auto_policy, dict):
        return {}
    distribution = auto_policy.get("distribution")
    return {
        "updated_count": int(auto_policy.get("updated_count") or 0),
        "total_auto_keywords": int(auto_policy.get("total_auto_keywords") or 0),
        "distribution": dict(distribution) if isinstance(distribution, dict) else {},
    }


def _load_persisted() -> dict[str, Any]:
    if not PERSIST_PATH.exists():
        return {}
    try:
        return json.loads(PERSIST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _persist() -> None:
    with _lock:
        payload = {
            "enabled": _state["enabled"],
            "interval_hours": _state["interval_hours"],
            "daily_keyword_budget": _state["daily_keyword_budget"],
            "max_keywords_per_batch": _state["max_keywords_per_batch"],
        }
    PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PERSIST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _check_active_batch() -> str | None:
    """启动时直接从运行状态接管活跃批次，避免 Flask 尚未监听时自请求失败。"""
    try:
        state = get_active_batch_status()
        if state and state.get("batch_id"):
            return str(state["batch_id"])
    except Exception as exc:
        print(f"[SCHEDULER] startup check error: {exc}", flush=True)
    return None


def _wait_for_batch(batch_id: str, base_url: str) -> bool:
    """轮询指定批次直到完成，返回 True 表示完成。"""
    print(f"[SCHEDULER] waiting for batch {batch_id} to finish …", flush=True)
    poll_url = f"{base_url}/api/refresh-all/status?batch_id={batch_id}"
    deadline = datetime.now().timestamp() + 86400
    while datetime.now().timestamp() < deadline:
        if _stop_event.wait(60):
            print("[SCHEDULER] stopped while waiting for batch", flush=True)
            return False
        try:
            s = requests.get(poll_url, timeout=10).json()
            if s.get("is_finished"):
                print(f"[SCHEDULER] batch {batch_id} finished, status={s.get('status')}", flush=True)
                with _lock:
                    _state["last_result"] = f"done:{s.get('status')}"
                return True
        except Exception as exc:
            print(f"[SCHEDULER] poll error: {exc}", flush=True)
    print(f"[SCHEDULER] batch {batch_id} polling timed out (24h)", flush=True)
    return False


def _do_refresh() -> None:
    """触发批量刷新，然后轮询直到批次完成（完成后才返回）。"""
    with _lock:
        base_url = _state["base_url"]
    print(f"[SCHEDULER] tick: triggering refresh-all at {_ts()}", flush=True)

    # 1. 触发
    batch_id: str | None = None
    try:
        with _lock:
            daily_budget = int(_state["daily_keyword_budget"])
            batch_limit = int(_state["max_keywords_per_batch"])
        plan = get_incremental_keywords(
            daily_keyword_budget=daily_budget,
            max_keywords_per_batch=batch_limit,
        )
        keywords = plan.get("keywords") or []
        keyword_ids = [
            str(item.get("keyword_id") or "").strip()
            for item in keywords
            if str(item.get("keyword_id") or "").strip()
        ]
        if not keyword_ids:
            result = "skipped:no_keywords"
        else:
            print(
                f"[SCHEDULER] due={plan.get('due_count')} selected={len(keyword_ids)} "
                f"deferred={plan.get('deferred_due_count')} "
                f"budget={plan.get('budget', {}).get('reserved_count')}/"
                f"{plan.get('budget', {}).get('daily_keyword_budget')}",
                flush=True,
            )
            resp = requests.post(
                f"{base_url}/api/refresh-all",
                json={
                    "incremental": True,
                    "keyword_ids": keyword_ids,
                    "refresh_round": plan.get("current_round"),
                },
                timeout=15,
                headers={"X-Scheduler": "true"},
            )
            if resp.status_code == 202:
                batch_id = (resp.json() or {}).get("batch_id")
                result = f"triggered:{batch_id}"
            elif resp.status_code == 409:
                busy = (resp.json() or {}).get("batch") or {}
                batch_id = busy.get("batch_id")
                result = f"joined_existing:{batch_id}"
            else:
                result = f"error:http_{resp.status_code}"
        with _lock:
            _state["last_plan"] = {
                "due_count": int(plan.get("due_count") or 0),
                "selected_count": len(keyword_ids),
                "deferred_due_count": int(plan.get("deferred_due_count") or 0),
                "budget": plan.get("budget") or {},
                "batch_capacity": plan.get("batch_capacity") or {},
                "auto_policy": _summarize_auto_policy(plan.get("auto_policy")),
            }
    except Exception as exc:
        result = f"error:{exc}"

    print(f"[SCHEDULER] tick: result={result}", flush=True)
    with _lock:
        _state["last_triggered_at"] = _ts()
        _state["last_result"] = result

    # 2. 轮询直到常规批次完成
    if batch_id:
        _wait_for_batch(batch_id, base_url)

    # 3. 常规刷新结束（或今天没有常规到期词）后，再使用独立发现额度。
    # 两类批次仍然串行，避免同时抢占浏览器和远端搜索资源。
    try:
        from app.services.keyword_discovery_service import run_discovery_cycle

        discovery = run_discovery_cycle(
            project_root=PROJECT_ROOT,
            auto_launch=True,
        )
        launch = discovery.get("launch") or {}
        discovery_batch_id = str(launch.get("batch_id") or "")
        with _lock:
            _state["last_discovery"] = discovery
        if discovery_batch_id:
            _wait_for_batch(discovery_batch_id, base_url)
            reconciled = run_discovery_cycle(
                project_root=PROJECT_ROOT,
                auto_launch=False,
            )
            with _lock:
                _state["last_discovery"] = reconciled
    except Exception as exc:
        print(f"[SCHEDULER] discovery cycle error: {exc}", flush=True)
        with _lock:
            _state["last_discovery"] = {"error": str(exc), "generated_at": _ts()}


def _run_loop() -> None:
    with _lock:
        interval_hours = _state["interval_hours"]
        enabled = _state["enabled"]

    # 启动时如果有活跃批次，先接管轮询它直到完成
    if enabled:
        active_batch = _check_active_batch()
        if active_batch:
            with _lock:
                base_url = _state["base_url"]
            print(f"[SCHEDULER] startup: found active batch {active_batch}, resuming poll", flush=True)
            _wait_for_batch(active_batch, base_url)

    next_run = datetime.now() + timedelta(hours=interval_hours)
    with _lock:
        _state["next_run_at"] = next_run.isoformat(timespec="seconds")

    print(
        f"[SCHEDULER] started, enabled={_state['enabled']}, "
        f"interval={interval_hours}h, next_run={next_run.isoformat(timespec='seconds')}",
        flush=True,
    )

    while True:
        # 从 _state 读最新 next_run（update_config 可能改过）
        with _lock:
            next_run_str = _state["next_run_at"]
            enabled = _state["enabled"]
            interval_hours = _state["interval_hours"]

        try:
            next_run = datetime.fromisoformat(next_run_str) if next_run_str else datetime.now()
        except ValueError:
            next_run = datetime.now()

        wait_secs = max(1.0, (next_run - datetime.now()).total_seconds())
        triggered = _trigger_event.wait(timeout=min(wait_secs, 60))

        if _stop_event.is_set():
            print("[SCHEDULER] stopped", flush=True)
            break

        manual = triggered and _trigger_event.is_set() and not (
            enabled and datetime.now() >= next_run
        )
        if triggered:
            _trigger_event.clear()

        # enabled=False 时仍允许手动 trigger（用于测试）
        if manual or (enabled and datetime.now() >= next_run):
            scheduled_slot = next_run
            _do_refresh()
            # 以原定槽位而非批次完成时间推进，长批次只会跳过已错过槽位，
            # 不会把后续时刻整体向后漂移；循环单线程保证不并发。
            new_next = _advance_fixed_clock(
                scheduled_slot,
                now=datetime.now(),
                interval_hours=interval_hours,
            ) if not manual else next_run
            with _lock:
                _state["next_run_at"] = new_next.isoformat(timespec="seconds")
            print(f"[SCHEDULER] next_run={new_next.isoformat(timespec='seconds')}", flush=True)


def start(base_url: str, interval_hours: float = 3.0, enabled: bool = False) -> None:
    global _thread
    persisted = _load_persisted()
    with _lock:
        _state["base_url"] = base_url
        _state["interval_hours"] = float(persisted.get("interval_hours", interval_hours))
        _state["enabled"] = bool(persisted.get("enabled", enabled))
        scheduled_cap = int(_budget_policy()["scheduled_keyword_budget"])
        _state["daily_keyword_budget"] = min(
            scheduled_cap,
            max(
                1,
                int(persisted.get("daily_keyword_budget", scheduled_cap)),
            ),
        )
        _state["max_keywords_per_batch"] = max(
            1,
            int(persisted.get("max_keywords_per_batch", _state["max_keywords_per_batch"])),
        )
    _stop_event.clear()
    _trigger_event.clear()
    _thread = threading.Thread(target=_run_loop, name="auto-refresh-scheduler", daemon=True)
    _thread.start()


def stop() -> None:
    _stop_event.set()
    _trigger_event.set()


def trigger_now() -> dict[str, Any]:
    """立即触发一次（不管 enabled 状态，用于测试）。"""
    _trigger_event.set()
    return get_status()


def update_config(
    enabled: bool | None = None,
    interval_hours: float | None = None,
    daily_keyword_budget: int | None = None,
    max_keywords_per_batch: int | None = None,
) -> dict[str, Any]:
    with _lock:
        if enabled is not None:
            _state["enabled"] = bool(enabled)
        if interval_hours is not None:
            ih = float(interval_hours)
            _state["interval_hours"] = ih
            # 重置 next_run，让新 interval 立即生效
            _state["next_run_at"] = (datetime.now() + timedelta(hours=ih)).isoformat(timespec="seconds")
        if daily_keyword_budget is not None:
            scheduled_cap = int(_budget_policy()["scheduled_keyword_budget"])
            _state["daily_keyword_budget"] = min(
                scheduled_cap,
                max(1, int(daily_keyword_budget)),
            )
        if max_keywords_per_batch is not None:
            _state["max_keywords_per_batch"] = max(1, int(max_keywords_per_batch))
    _persist()
    return get_status()


def get_status() -> dict[str, Any]:
    with _lock:
        payload = {**_state}
    payload["budget"] = get_refresh_budget_status(
        daily_keyword_budget=int(payload["daily_keyword_budget"]),
    )
    try:
        from app.services.keyword_discovery_service import get_discovery_budget_status

        discovery = get_discovery_budget_status(project_root=PROJECT_ROOT)
    except Exception as exc:
        discovery = {"error": str(exc)}
    policy = _budget_policy()
    scheduled_used = int(payload["budget"].get("reserved_count") or 0)
    discovery_used = int(discovery.get("used_count") or 0)
    payload["budget_breakdown"] = {
        "total_daily_budget": int(policy["daily_keyword_budget"]),
        "scheduled": {
            "limit": int(payload["daily_keyword_budget"]),
            "used": scheduled_used,
            "remaining": max(0, int(payload["daily_keyword_budget"]) - scheduled_used),
        },
        "discovery": {
            "limit": int(policy["discovery_daily_search_budget"]),
            "used": discovery_used,
            "remaining": max(
                0,
                int(policy["discovery_daily_search_budget"]) - discovery_used,
            ),
        },
        "manual_reserve": int(policy["manual_reserve_budget"]),
        "accounted_used": scheduled_used + discovery_used,
        "unallocated_or_manual_remaining": max(
            0,
            int(policy["daily_keyword_budget"]) - scheduled_used - discovery_used,
        ),
    }
    return payload
