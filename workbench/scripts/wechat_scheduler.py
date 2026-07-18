#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


BASE_URL = os.getenv("WORKBENCH_BASE_URL", "http://127.0.0.1:8799").rstrip("/")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = PROJECT_ROOT / "data/runtime/wechat-scheduler.lock"
REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("WECHAT_SCHEDULER_REQUEST_TIMEOUT_SECONDS", "43200")
)


def log(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def request_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 30,
) -> tuple[int, dict[str, Any]]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        key = str(payload.get("idempotency_key") or "")
        if key:
            headers["Idempotency-Key"] = key
    request = urllib.request.Request(
        BASE_URL + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
    value = json.loads(raw.decode("utf-8")) if raw else {}
    return status, value if isinstance(value, dict) else {"data": value}


def parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def is_due(status: dict[str, Any]) -> bool:
    if not bool(status.get("enabled")) or bool(status.get("is_active")):
        return False
    now = datetime.now(UTC)
    last_triggered = parse_time(status.get("last_triggered_at"))
    if last_triggered is None:
        return True
    interval = max(0.1, float(status.get("interval_hours") or 3.0))
    return now >= last_triggered + timedelta(hours=interval)


def ensure_default_enabled(status: dict[str, Any]) -> dict[str, Any]:
    """Recover the uninitialised scheduler without overriding explicit disable."""
    if bool(status.get("enabled")) or bool(status.get("enabled_explicit")):
        return status
    if os.getenv("WECHAT_SCHEDULER_AUTO_ENABLE_DEFAULT", "1").strip().lower() in {"0", "false", "no"}:
        log("新系统微信调度为默认未初始化状态；按环境配置保持关闭")
        return status
    key = f"launchd-wechat-scheduler-default-enable-{datetime.now(UTC).date().isoformat()}"
    code, result = request_json(
        "/api/scheduler/config",
        method="POST",
        payload={"enabled": True, "idempotency_key": key},
    )
    if code not in {200, 202}:
        raise RuntimeError(f"恢复默认微信调度失败：HTTP {code} {result}")
    log("检测到调度状态未初始化，已恢复默认启用路径")
    return result


def main() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("已有新系统微信调度进程运行，本轮跳过")
            return 0
        try:
            status_code, status = request_json("/api/scheduler/status")
        except Exception as exc:
            log(f"读取新系统调度状态失败：{exc}")
            return 1
        if status_code != 200:
            log(f"读取新系统调度状态失败：HTTP {status_code}")
            return 1
        try:
            status = ensure_default_enabled(status)
        except Exception as exc:
            log(f"恢复新系统微信调度默认状态失败：{exc}")
            return 1
        if not bool(status.get("enabled")):
            log("新系统微信调度被显式禁用，本轮跳过（enabled=0）")
            return 0
        if bool(status.get("is_active")):
            log("新系统已有微信刷新批次运行，本轮跳过")
            return 0
        if not is_due(status):
            log(f"尚未到刷新时间，next_run_at={status.get('next_run_at')}")
            return 0
        interval_seconds = max(
            360,
            int(float(status.get("interval_hours") or 3.0) * 3600),
        )
        slot = int(time.time()) // interval_seconds
        idempotency_key = f"launchd-wechat-scheduler-{slot}"
        log("开始调用新系统微信定时刷新")
        try:
            response_code, result = request_json(
                "/api/scheduler/trigger",
                method="POST",
                payload={"idempotency_key": idempotency_key},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            log(f"新系统微信定时刷新请求失败：{exc}")
            return 1
        log(
            "新系统微信定时刷新结束："
            f"HTTP {response_code} trigger_status={result.get('trigger_status')} "
            f"batch_id={result.get('batch_id')} batch_status={result.get('batch_status')}"
        )
        return 0 if response_code in {200, 202, 409} else 1


if __name__ == "__main__":
    raise SystemExit(main())
