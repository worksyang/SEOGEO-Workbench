#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批跑巡检器。

默认每 10 分钟巡检一次：
1. runner 进程是否还活着
2. heartbeat 是否长时间不更新
3. 当前完成数 / 失败数 / 正在执行关键词
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS_ROOT = PROJECT_ROOT / "data" / "runs"
FINAL_STATUSES = {"completed", "completed_with_failures", "failed", "cancelled"}
AUTOFIX_FAILURE_STATUSES = {"completed_with_failures", "failed"}
REMOTE_CANCEL_MARKERS = ("掉线", "离线", "offline", "connection", "network")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def seconds_since(iso_text: str | None) -> int | None:
    if not iso_text:
        return None
    try:
        dt = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    return int((datetime.now() - dt).total_seconds())


def should_autofix_terminal_state(state: dict[str, Any]) -> bool:
    status = str(state.get("status") or "")
    if status in AUTOFIX_FAILURE_STATUSES:
        return True
    if status != "cancelled":
        return False
    reason = str(state.get("cancel_reason") or "").lower()
    return any(marker in reason for marker in REMOTE_CANCEL_MARKERS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="巡检关键词批跑任务")
    parser.add_argument("--batch-id", required=True, help="批次 ID")
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT), help="批次状态根目录")
    parser.add_argument("--interval", type=int, default=600, help="巡检间隔秒数")
    parser.add_argument("--stale-threshold", type=int, default=1200, help="heartbeat 过期阈值秒数")
    parser.add_argument(
        "--disable-autofix",
        action="store_true",
        help="故障结束后不启动 Claude -p 自动排障",
    )
    return parser.parse_args()


def launch_autofix(
    batch_id: str,
    runs_root: Path,
    batch_dir: Path,
    *,
    trigger: str = "",
) -> None:
    """异步唤醒自动修复器；watchdog 自己不等待 Claude 完成。"""
    if os.environ.get("WX_REFRESH_AUTOFIX_ENABLED", "1").lower() in {"0", "false", "no"}:
        return
    script = PROJECT_ROOT / "scripts" / "refresh_failure_autofix.py"
    if not script.exists():
        return
    log_path = batch_dir / "autofix.launch.log"
    with log_path.open("a", encoding="utf-8") as log:
        command = [
            sys.executable,
            str(script),
            "--batch-id",
            batch_id,
            "--runs-root",
            str(runs_root),
        ]
        if trigger:
            command.extend(["--trigger", trigger])
        if os.environ.get("WX_REFRESH_AUTOFIX_DRY_RUN", "").lower() in {"1", "true", "yes"}:
            command.append("--dry-run")
        subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )


def main() -> int:
    args = parse_args()
    runs_root = Path(args.runs_root).expanduser().resolve()
    batch_dir = runs_root / args.batch_id
    state_file = batch_dir / "state.json"
    launch_file = batch_dir / "launch.json"
    watchdog_log = batch_dir / "watchdog.jsonl"
    watchdog_status_file = batch_dir / "watchdog-status.json"

    batch_dir.mkdir(parents=True, exist_ok=True)

    while True:
        state = read_json(state_file, default={}) or {}
        launch = read_json(launch_file, default={}) or {}
        status = state.get("status", "unknown")
        heartbeat_age = seconds_since(state.get("heartbeat_at"))
        runner_pid = state.get("runner_pid") or launch.get("runner_pid")
        runner_alive = is_pid_alive(runner_pid)

        summary = {
            "at": now_iso(),
            "batch_id": args.batch_id,
            "status": status,
            "runner_pid": runner_pid,
            "runner_alive": runner_alive,
            "heartbeat_age_sec": heartbeat_age,
            "success_count": state.get("success_count"),
            "failed_count": state.get("failed_count"),
            "pending_count": state.get("pending_count"),
            "current_item_id": state.get("current_item_id"),
            "current_keyword": state.get("current_keyword"),
            "current_attempt": state.get("current_attempt"),
            "alert": None,
        }

        if status in FINAL_STATUSES:
            summary["alert"] = "batch_finished"
            append_jsonl(watchdog_log, summary)
            write_json(watchdog_status_file, summary)
            if not args.disable_autofix and should_autofix_terminal_state(state):
                launch_autofix(args.batch_id, runs_root, batch_dir)
            return 0

        if not state:
            summary["alert"] = "bootstrap_wait" if runner_alive else "state_missing"
        elif not runner_alive:
            summary["alert"] = "runner_not_alive"
            append_jsonl(watchdog_log, summary)
            write_json(watchdog_status_file, summary)
            if not args.disable_autofix:
                launch_autofix(
                    args.batch_id,
                    runs_root,
                    batch_dir,
                    trigger="runner_not_alive",
                )
            return 1
        elif heartbeat_age is not None and heartbeat_age > args.stale_threshold:
            summary["alert"] = "heartbeat_stale"

        append_jsonl(watchdog_log, summary)
        write_json(watchdog_status_file, summary)
        time.sleep(max(args.interval, 1))


if __name__ == "__main__":
    raise SystemExit(main())
