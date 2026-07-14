#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI entrypoint for the integrated WeRSS refresh and article workflow.
"""

import sys

from workflow_service import PROJECT_DIR, WorkflowOptions, run_full_workflow


def choose_run_mode() -> str:
    while True:
        print("\n" + "=" * 60)
        print("请选择运行模式:")
        print("  (1) 刷新所有公众号后再运行工作流")
        print("  (2) 直接使用现有数据运行工作流")
        mode = input("请输入选项 [1/2]: ").strip()
        if mode in {"1", "2"}:
            return mode
        print("❌ 输入无效，请输入 1 或 2。")


def main() -> None:
    print("=" * 60)
    print("微信公众号刷新 + Article Workflow 一体化任务启动")
    print("=" * 60)
    print(f"📁 项目目录：{PROJECT_DIR}")

    mode = choose_run_mode()
    options = WorkflowOptions(refresh_before_run=(mode == "1"))

    try:
        run_full_workflow(options)
    except KeyboardInterrupt:
        print("\n\n👋 程序被用户中断")
    except Exception as exc:
        print(f"\n❌ 程序执行失败：{exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
