#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台启动关键词批跑 runner + watchdog。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS_ROOT = PROJECT_ROOT / "data" / "runs"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "微信搜索结果" / "批量抓取"
DEFAULT_SERVER = os.environ.get("WX_SEARCH_SERVER", "http://192.168.31.238:8000")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_batch_id() -> str:
    return f"overnight_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="后台启动关键词批跑")
    parser.add_argument("--batch-id", default="", help="批次 ID；为空时自动生成")
    parser.add_argument("--keywords-file", default="", help="关键词清单文件；为空时从统一注册表导出 active 词")
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT), help="批次状态根目录")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="搜索结果落地根目录")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="微信搜索服务地址")
    parser.add_argument("--fetch-depth", type=int, choices=[0, 1], default=1, help="抓取深度")
    parser.add_argument("--fetch-max-count", type=int, default=5, help="正文抓取上限")
    parser.add_argument("--timeout", type=int, default=480, help="单词请求超时")
    parser.add_argument("--max-attempts", type=int, default=2, help="每个关键词最多尝试次数")
    parser.add_argument("--retry-sleep", type=int, default=20, help="重试等待秒数")
    parser.add_argument("--rebuild-every", type=int, default=10, help="每成功 N 个词做一次中途重建")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个关键词")
    parser.add_argument("--resume", action="store_true", help="恢复既有批次")
    parser.add_argument("--include-failed", action="store_true", help="恢复时重跑已失败关键词")
    parser.add_argument("--interval", type=int, default=600, help="watchdog 巡检间隔秒数")
    parser.add_argument("--stale-threshold", type=int, default=1200, help="heartbeat 过期阈值秒数")
    parser.add_argument("--no-watchdog", action="store_true", help="只启动 runner，不启动 watchdog")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    batch_id = args.batch_id or make_batch_id()
    runs_root = Path(args.runs_root).expanduser().resolve()
    batch_dir = runs_root / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    keywords_file = Path(args.keywords_file).expanduser().resolve() if args.keywords_file else batch_dir / "keywords.json"
    if not args.keywords_file:
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from app.repositories.keyword_registry_repo import KeywordRegistryRepository
        payload = KeywordRegistryRepository(
            PROJECT_ROOT / "data/state/app.db"
        ).load_payload()
        write_json(keywords_file, payload)

    runner_script = PROJECT_ROOT / "scripts" / "keyword_batch_runner.py"
    watchdog_script = PROJECT_ROOT / "scripts" / "keyword_batch_watchdog.py"

    runner_cmd = [
        sys.executable,
        str(runner_script),
        "--batch-id",
        batch_id,
        "--keywords-file",
        str(keywords_file),
        "--runs-root",
        str(runs_root),
        "--output-root",
        str(Path(args.output_root).expanduser().resolve()),
        "--server",
        args.server,
        "--fetch-depth",
        str(args.fetch_depth),
        "--fetch-max-count",
        str(args.fetch_max_count),
        "--timeout",
        str(args.timeout),
        "--max-attempts",
        str(args.max_attempts),
        "--retry-sleep",
        str(args.retry_sleep),
        "--rebuild-every",
        str(args.rebuild_every),
    ]
    if args.limit > 0:
        runner_cmd.extend(["--limit", str(args.limit)])
    if args.resume:
        runner_cmd.append("--resume")
    if args.include_failed:
        runner_cmd.append("--include-failed")

    runner_log_path = batch_dir / "runner.launch.log"
    runner_log = runner_log_path.open("a", encoding="utf-8")
    runner_proc = subprocess.Popen(
        runner_cmd,
        cwd=PROJECT_ROOT,
        stdout=runner_log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    runner_log.close()

    watchdog_proc = None
    watchdog_log_path = batch_dir / "watchdog.launch.log"
    if not args.no_watchdog:
        watchdog_cmd = [
            sys.executable,
            str(watchdog_script),
            "--batch-id",
            batch_id,
            "--runs-root",
            str(runs_root),
            "--interval",
            str(args.interval),
            "--stale-threshold",
            str(args.stale_threshold),
        ]
        watchdog_log = watchdog_log_path.open("a", encoding="utf-8")
        watchdog_proc = subprocess.Popen(
            watchdog_cmd,
            cwd=PROJECT_ROOT,
            stdout=watchdog_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        watchdog_log.close()

    payload = {
        "batch_id": batch_id,
        "launched_at": datetime.now().isoformat(timespec="seconds"),
        "batch_dir": str(batch_dir),
        "runner_pid": runner_proc.pid,
        "watchdog_pid": watchdog_proc.pid if watchdog_proc else None,
        "runner_log": str(runner_log_path),
        "watchdog_log": str(watchdog_log_path) if watchdog_proc else None,
        "runner_cmd": runner_cmd,
    }
    write_json(batch_dir / "launch.json", payload)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
