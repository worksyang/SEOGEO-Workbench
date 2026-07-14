#!/usr/bin/env python3
"""TikHub 批量刷新护航器。

runner 因进程异常退出时从未完成关键词继续；若本轮只剩失败词则有限次重试。
用户主动取消永远不复活任务。达到恢复上限后写入 failed，由失败诊断器接管。
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
RUNNER = PROJECT_ROOT / "scripts" / "tikhub_batch_runner.py"
AUTOFIX = PROJECT_ROOT / "scripts" / "refresh_failure_autofix.py"
ACTIVE = {"starting", "running"}
FINAL = {"completed", "completed_with_failures", "failed", "cancelled"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TikHub batch refresh guard")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--max-revive-count", type=int, default=2)
    return parser.parse_args()


def launch_resume(
    *,
    batch_id: str,
    runs_root: Path,
    batch_dir: Path,
    include_failed: bool,
) -> int:
    keywords_file = batch_dir / "keywords.json"
    if not keywords_file.exists():
        raise FileNotFoundError("批次缺少 keywords.json，无法恢复")
    cmd = [
        sys.executable, str(RUNNER),
        "--batch-id", batch_id,
        "--keywords-file", str(keywords_file),
        "--runs-root", str(runs_root),
        "--resume",
    ]
    if include_failed:
        cmd.append("--include-failed")
    log_path = batch_dir / "runner.log"
    with log_path.open("a", encoding="utf-8") as log_fp:
        proc = subprocess.Popen(
            cmd,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    launch_path = batch_dir / "launch.json"
    launch = read_json(launch_path, {}) or {}
    launch.update({
        "batch_id": batch_id,
        "runner_pid": proc.pid,
        "runner_resumed_at": now_iso(),
        "runner_command": cmd,
        "log_path": str(log_path),
    })
    write_json(launch_path, launch)
    state_path = batch_dir / "state.json"
    state = read_json(state_path, {}) or {}
    state.update({
        "status": "starting",
        "runner_pid": proc.pid,
        "current_keyword": None,
        "current_attempt": None,
        "heartbeat_at": now_iso(),
        "updated_at": now_iso(),
        "last_resume_include_failed": include_failed,
    })
    write_json(state_path, state)
    return proc.pid


def mark_terminal_failure(batch_dir: Path, reason: str) -> None:
    state_path = batch_dir / "state.json"
    state = read_json(state_path, {}) or {}
    state.update({
        "status": "failed",
        "finished_at": now_iso(),
        "current_keyword": None,
        "current_attempt": None,
        "error": reason,
        "updated_at": now_iso(),
    })
    write_json(state_path, state)


def launch_autofix(batch_id: str, runs_root: Path) -> None:
    """最终恢复失败才交 Claude -p；正常自动续跑期间不抢先诊断。"""
    if os.environ.get("XHS_REFRESH_AUTOFIX_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    if not AUTOFIX.exists():
        return
    try:
        subprocess.Popen(
            [sys.executable, str(AUTOFIX), "--batch-id", batch_id, "--runs-root", str(runs_root)],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def main() -> int:
    args = parse_args()
    runs_root = args.runs_root.resolve()
    batch_dir = runs_root / args.batch_id
    state_path = batch_dir / "state.json"
    launch_path = batch_dir / "launch.json"
    guard_log = batch_dir / "guard.jsonl"
    guard_status = batch_dir / "guard-status.json"
    revive_count = 0

    while True:
        state = read_json(state_path, {}) or {}
        launch = read_json(launch_path, {}) or {}
        status = str(state.get("status") or "unknown")
        cancel_requested = bool(state.get("cancel_requested")) or (batch_dir / "cancel.flag").exists()
        runner_pid = state.get("runner_pid") or launch.get("runner_pid")
        runner_alive = is_pid_alive(int(runner_pid)) if runner_pid else False
        snapshot: dict[str, Any] = {
            "at": now_iso(),
            "batch_id": args.batch_id,
            "status": status,
            "runner_pid": runner_pid,
            "runner_alive": runner_alive,
            "success_count": int(state.get("success_count") or 0),
            "failed_count": int(state.get("failed_count") or 0),
            "total_keywords": int(state.get("total_keywords") or 0),
            "revive_count": revive_count,
            "action": "wait",
        }

        if cancel_requested or status == "cancelled":
            snapshot["action"] = "exit_cancelled"
            append_jsonl(guard_log, snapshot)
            write_json(guard_status, snapshot)
            return 0
        if status == "completed":
            snapshot["action"] = "done"
            append_jsonl(guard_log, snapshot)
            write_json(guard_status, snapshot)
            return 0

        include_failed = status in {"failed", "completed_with_failures"}
        needs_resume = (
            status in {"failed", "completed_with_failures"}
            or (status in ACTIVE and not runner_alive)
        )
        if needs_resume:
            if revive_count >= max(0, args.max_revive_count):
                reason = "自动恢复次数已用尽；请查看 Claude 自动诊断报告。"
                mark_terminal_failure(batch_dir, reason)
                launch_autofix(args.batch_id, runs_root)
                snapshot["action"] = "blocked_max_revive"
                snapshot["error"] = reason
                append_jsonl(guard_log, snapshot)
                write_json(guard_status, snapshot)
                return 2
            try:
                pid = launch_resume(
                    batch_id=args.batch_id,
                    runs_root=runs_root,
                    batch_dir=batch_dir,
                    include_failed=include_failed,
                )
            except Exception as exc:
                mark_terminal_failure(batch_dir, f"自动恢复启动失败：{type(exc).__name__}: {exc}")
                launch_autofix(args.batch_id, runs_root)
                snapshot["action"] = "resume_launch_failed"
                snapshot["error"] = str(exc)
                append_jsonl(guard_log, snapshot)
                write_json(guard_status, snapshot)
                return 2
            revive_count += 1
            snapshot.update({
                "action": "resume_failed_keywords" if include_failed else "resume_after_runner_exit",
                "new_runner_pid": pid,
                "revive_count": revive_count,
            })

        append_jsonl(guard_log, snapshot)
        write_json(guard_status, snapshot)
        time.sleep(max(args.interval, 1.0))


if __name__ == "__main__":
    raise SystemExit(main())
