"""自动批量刷新调度器 — Flask 进程内 daemon thread。

生产服务开启后，每 24 小时触发一次 TikHub 全量关键词刷新。下一次执行时间会
落盘，服务重启不会把“每日一次”不断往后推迟。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.refresh_service import (
    BatchAlreadyRunningError,
    get_active_batch_status,
    get_batch_status,
    get_incremental_keywords,
    start_batch_refresh,
)


def _project_root() -> Path:
    from app.config import Config
    return Config.PROJECT_ROOT


PROJECT_ROOT = _project_root()
PERSIST_PATH = PROJECT_ROOT / "data" / "state" / "scheduler.json"

_lock = threading.Lock()
_stop_event = threading.Event()
_trigger_event = threading.Event()
_thread: threading.Thread | None = None

_state: dict[str, Any] = {
    "enabled": False,
    "interval_hours": 24.0,
    "base_url": "http://127.0.0.1:8766",
    "next_run_at": None,
    "last_triggered_at": None,
    "last_result": None,
}


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


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
            "next_run_at": _state["next_run_at"],
            "last_triggered_at": _state["last_triggered_at"],
            "last_result": _state["last_result"],
        }
    PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PERSIST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _check_active_batch() -> str | None:
    active = get_active_batch_status()
    return str(active.get("batch_id") or "") if active else None


def _wait_for_batch(batch_id: str, base_url: str) -> bool:
    deadline = datetime.now().timestamp() + 86400
    while datetime.now().timestamp() < deadline:
        if _stop_event.wait(60):
            return False
        try:
            s = get_batch_status(batch_id) or {}
            if s.get("is_finished"):
                with _lock:
                    _state["last_result"] = f"done:{s.get('status')}"
                return True
        except Exception:
            pass
    return False


def _do_refresh() -> None:
    with _lock:
        base_url = _state["base_url"]
    batch_id: str | None = None
    try:
        active = get_active_batch_status()
        if active:
            batch_id = active.get("batch_id")
            result = f"joined_existing:{batch_id}"
            with _lock:
                _state["last_triggered_at"] = _ts()
                _state["last_result"] = result
            _wait_for_batch(str(batch_id), base_url)
            return

        plan = get_incremental_keywords()
        keywords = plan.get("keywords") or []
        if not keywords:
            result = "skipped:no_keywords"
        else:
            state = start_batch_refresh(
                keywords,
                source="scheduler",
                refresh_round=plan.get("current_round"),
            )
            batch_id = state.get("batch_id")
            result = f"triggered:{batch_id}"
    except BatchAlreadyRunningError as exc:
        batch_id = exc.state.get("batch_id")
        result = f"joined_existing:{batch_id}"
    except Exception as exc:
        result = f"error:{exc}"
    with _lock:
        _state["last_triggered_at"] = _ts()
        _state["last_result"] = result
    _persist()
    if not batch_id:
        return
    _wait_for_batch(batch_id, base_url)


def _run_loop() -> None:
    with _lock:
        interval_hours = _state["interval_hours"]
        enabled = _state["enabled"]
    if enabled:
        active_batch = _check_active_batch()
        if active_batch:
            with _lock:
                base_url = _state["base_url"]
            _wait_for_batch(active_batch, base_url)

    with _lock:
        persisted_next = _state.get("next_run_at")
    try:
        next_run = datetime.fromisoformat(str(persisted_next)) if persisted_next else None
    except ValueError:
        next_run = None
    if next_run is None:
        next_run = datetime.now() + timedelta(hours=interval_hours)
        with _lock:
            _state["next_run_at"] = next_run.isoformat(timespec="seconds")
        _persist()

    while True:
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
            break
        manual = triggered and _trigger_event.is_set()
        if triggered:
            _trigger_event.clear()
        if manual or (enabled and datetime.now() >= next_run):
            _do_refresh()
            new_next = datetime.now() + timedelta(hours=interval_hours)
            with _lock:
                _state["next_run_at"] = new_next.isoformat(timespec="seconds")
            _persist()


def start(base_url: str, interval_hours: float = 24.0, enabled: bool = False) -> None:
    global _thread
    persisted = _load_persisted()
    with _lock:
        _state["base_url"] = base_url
        _state["interval_hours"] = float(persisted.get("interval_hours", interval_hours))
        _state["enabled"] = bool(persisted.get("enabled", enabled))
        _state["next_run_at"] = persisted.get("next_run_at")
        _state["last_triggered_at"] = persisted.get("last_triggered_at")
        _state["last_result"] = persisted.get("last_result")
    _stop_event.clear()
    _trigger_event.clear()
    _thread = threading.Thread(target=_run_loop, name="xhs-auto-refresh-scheduler", daemon=True)
    _thread.start()


def stop() -> None:
    _stop_event.set()
    _trigger_event.set()


def trigger_now() -> dict[str, Any]:
    _trigger_event.set()
    return get_status()


def update_config(enabled: bool | None = None, interval_hours: float | None = None) -> dict[str, Any]:
    with _lock:
        if enabled is not None:
            _state["enabled"] = bool(enabled)
        if interval_hours is not None:
            ih = float(interval_hours)
            _state["interval_hours"] = ih
            _state["next_run_at"] = (datetime.now() + timedelta(hours=ih)).isoformat(timespec="seconds")
    _persist()
    return get_status()


def get_status() -> dict[str, Any]:
    with _lock:
        return {**_state}
