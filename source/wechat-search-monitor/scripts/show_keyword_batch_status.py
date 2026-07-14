#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS_ROOT = PROJECT_ROOT / "data" / "runs"


def seconds_since(iso_text: str | None) -> int | None:
    if not iso_text:
        return None
    try:
        dt = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    return int((datetime.now() - dt).total_seconds())


def main() -> int:
    parser = argparse.ArgumentParser(description="查看关键词批跑状态")
    parser.add_argument("--batch-id", required=True, help="批次 ID")
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT), help="批次状态根目录")
    args = parser.parse_args()

    state_file = Path(args.runs_root).expanduser().resolve() / args.batch_id / "state.json"
    if not state_file.exists():
        print(f"状态文件不存在：{state_file}")
        return 1

    state = json.loads(state_file.read_text(encoding="utf-8"))
    heartbeat_age = seconds_since(state.get("heartbeat_at"))
    lines = [
        f"批次：{state.get('batch_id')}",
        f"状态：{state.get('status')}",
        f"开始：{state.get('started_at')}",
        f"结束：{state.get('finished_at')}",
        f"成功：{state.get('success_count')}",
        f"失败：{state.get('failed_count')}",
        f"待跑：{state.get('pending_count')}",
        f"当前：{state.get('current_item_id') or '-'} {state.get('current_keyword') or ''}".rstrip(),
        f"尝试：{state.get('current_attempt') or '-'}",
        f"最近心跳：{state.get('heartbeat_at')}（{heartbeat_age if heartbeat_age is not None else '-'} 秒前）",
        f"最终重建：{state.get('final_rebuild_ok')}",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
