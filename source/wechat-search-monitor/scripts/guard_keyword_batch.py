#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
正式批跑护航器。

职责：
1. 监控 runner 是否仍存活。
2. 若 runner 意外退出且批次未完成，自动以 --resume 续跑。
3. 若批次进入 completed_with_failures，自动以 --resume --include-failed 重刷失败词。
4. 直到 success_count == total_keywords 且 final_rebuild_ok 为真后退出。
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
FINAL_STATUSES = {"completed", "completed_with_failures", "failed"}


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="护航关键词批跑到100个完成")
    parser.add_argument("--batch-id", required=True, help="批次 ID")
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT), help="批次状态根目录")
    parser.add_argument("--interval", type=int, default=600, help="护航轮询间隔秒数")
    parser.add_argument("--runner-stale-threshold", type=int, default=1800, help="runner 心跳超时阈值")
    parser.add_argument("--max-revive-count", type=int, default=10, help="最多自动续跑次数")
    return parser.parse_args()


def build_resume_cmd(batch_id: str, state: dict[str, Any], runs_root: Path, include_failed: bool) -> list[str]:
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "start_keyword_batch.py"),
        "--batch-id",
        batch_id,
        "--resume",
        "--no-watchdog",
        "--runs-root",
        str(runs_root),
        "--keywords-file",
        str(state.get("source_keywords_file") or state.get("keywords_file")),
        "--output-root",
        str(state.get("output_root")),
        "--server",
        str(state.get("server")),
        "--fetch-depth",
        str(state.get("fetch_depth", 1)),
        "--fetch-max-count",
        str(state.get("fetch_max_count", 5)),
        "--timeout",
        str(state.get("timeout", 480)),
        "--max-attempts",
        str(state.get("max_attempts", 2)),
        "--retry-sleep",
        str(state.get("retry_sleep", 20)),
        "--rebuild-every",
        str(state.get("rebuild_every", 10)),
    ]
    if include_failed:
        cmd.append("--include-failed")
    return cmd


def main() -> int:
    args = parse_args()
    runs_root = Path(args.runs_root).expanduser().resolve()
    batch_dir = runs_root / args.batch_id
    state_file = batch_dir / "state.json"
    launch_file = batch_dir / "launch.json"
    guard_log = batch_dir / "guard.jsonl"
    guard_status = batch_dir / "guard-status.json"
    revive_count = 0

    while True:
        state = read_json(state_file, default={}) or {}
        launch = read_json(launch_file, default={}) or {}
        runner_pid = state.get("runner_pid") or launch.get("runner_pid")
        status = state.get("status", "unknown")
        success_count = int(state.get("success_count") or 0)
        failed_count = int(state.get("failed_count") or 0)
        total_keywords = int(state.get("total_keywords") or 0)
        final_rebuild_ok = state.get("final_rebuild_ok")
        heartbeat_age = seconds_since(state.get("heartbeat_at"))
        runner_alive = is_pid_alive(runner_pid)

        snapshot = {
            "at": now_iso(),
            "batch_id": args.batch_id,
            "status": status,
            "success_count": success_count,
            "failed_count": failed_count,
            "total_keywords": total_keywords,
            "runner_pid": runner_pid,
            "runner_alive": runner_alive,
            "heartbeat_age_sec": heartbeat_age,
            "revive_count": revive_count,
            "action": None,
        }

        if status == "completed" and success_count == total_keywords and total_keywords > 0 and final_rebuild_ok is True:
            snapshot["action"] = "done"
            append_jsonl(guard_log, snapshot)
            write_json(guard_status, snapshot)
            return 0

        need_resume = False
        include_failed = False

        if status == "completed_with_failures" and failed_count > 0:
            need_resume = True
            include_failed = True
            snapshot["action"] = "resume_failed_keywords"
        elif status == "failed":
            need_resume = True
            include_failed = True
            snapshot["action"] = "resume_after_failed_status"
        elif status == "running" and (not runner_alive):
            need_resume = True
            include_failed = False
            snapshot["action"] = "resume_after_runner_exit"
        elif status == "running" and heartbeat_age is not None and heartbeat_age > args.runner_stale_threshold and not runner_alive:
            need_resume = True
            include_failed = False
            snapshot["action"] = "resume_after_stale_heartbeat"

        if need_resume:
            if revive_count >= args.max_revive_count:
                snapshot["action"] = f"{snapshot['action']}_blocked_max_revive"
                append_jsonl(guard_log, snapshot)
                write_json(guard_status, snapshot)
                return 2

            cmd = build_resume_cmd(
                batch_id=args.batch_id,
                state=state,
                runs_root=runs_root,
                include_failed=include_failed,
            )
            proc = subprocess.run(
                cmd,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            revive_count += 1
            snapshot["resume_cmd"] = cmd
            snapshot["resume_returncode"] = proc.returncode
            snapshot["resume_stdout_tail"] = (proc.stdout or "")[-3000:]
            snapshot["resume_stderr_tail"] = (proc.stderr or "")[-3000:]
            append_jsonl(guard_log, snapshot)
            write_json(guard_status, snapshot)
            time.sleep(15)
            continue

        append_jsonl(guard_log, snapshot)
        write_json(guard_status, snapshot)
        time.sleep(max(args.interval, 1))


if __name__ == "__main__":
    raise SystemExit(main())
