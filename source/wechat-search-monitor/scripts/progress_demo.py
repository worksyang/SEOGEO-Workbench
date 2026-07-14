#!/usr/bin/env python3
"""演示：用现有 refresh_service 跑 2-3 个关键词，实时打印进度条。"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.refresh_service import start_single_refresh, get_job_status


def progress_demo(keywords: list[str]) -> None:
    total = len(keywords)
    print(f"=== 进度条演示：共 {total} 个关键词 ===", flush=True)
    print("开始时间:", time.strftime("%Y-%m-%d %H:%M:%S"), flush=True)
    print("-" * 60, flush=True)

    done = 0
    failed = 0

    for kw in keywords:
        print(f"\n▶ 启动关键词 [{done+failed+1}/{total}]: {kw}", flush=True)
        job_id = start_single_refresh(kw)

        # 给子线程一点时间写状态文件，最多等 5 秒
        state = None
        for _ in range(10):
            state = get_job_status(job_id)
            if state:
                break
            time.sleep(0.5)

        if not state:
            print("  ⚠ 超时未拿到状态，跳过", flush=True)
            continue

        last_status = ""
        while True:
            state = get_job_status(job_id)
            if not state:
                break

            status = state.get("status", "unknown")
            if status != last_status:
                bar = "█" * (done + failed + 1) + "░" * (total - done - failed - 1)
                print(f"  {bar} 状态: {status}", flush=True)
                last_status = status

            if status in ("done", "failed"):
                if status == "done":
                    done += 1
                    print(f"  ✅ 完成 [{done}/{total}] — 成功", flush=True)
                else:
                    failed += 1
                    print(f"  ❌ 失败 [{failed}/{total}] — 失败", flush=True)
                break
            time.sleep(2)

    print("\n" + "-" * 60, flush=True)
    print(f"全部完成 | 成功: {done} | 失败: {failed} | 总计: {total}", flush=True)
    print("结束时间:", time.strftime("%Y-%m-%d %H:%M:%S"), flush=True)


if __name__ == "__main__":
    demo_keywords = [
        "友邦财富盈活",
        "安盛盛利2",
    ]
    progress_demo(demo_keywords)