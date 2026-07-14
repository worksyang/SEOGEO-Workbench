#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIDSO 单关键词热度查询 CLI（项目根入口，已弃用薄壳）。

⚠️ 弃用说明
------------
本文件是历史兼容 shim。所有逻辑、参数、默认值都集中在
`scripts/fetch_aidso_keyword_heat.py`，请优先使用那个入口：

    python scripts/fetch_aidso_keyword_heat.py 友邦环宇
    python scripts/fetch_aidso_keyword_heat.py --help

根目录入口只是为了不破坏旧命令和老文档链接，所有参数 1:1 透传。

默认行为（与 scripts 版本完全一致，由 shim 透传）：
  - 走 system Google Chrome（Playwright channel="chrome"），headless 抓取
  - 先尝试复用已有登录态
  - 若登录态失效，则自动拉起有头浏览器让用户扫码；成功后回到原目标模式继续抓取

常用命令：
  python aidso_wx_keyword.py 友邦环宇                       # 默认无头抓；必要时自动补登录
  python aidso_wx_keyword.py 友邦环宇 --pretty/--json        # 输出完整 JSON
  python aidso_wx_keyword.py 友邦环宇 --show                 # 最终抓取改成有头模式
  python aidso_wx_keyword.py --login 友邦环宇                # 只做登录，不抓取
  python aidso_wx_keyword.py 友邦环宇 --no-auto-login        # 只尝试现有会话，不自动补登录

关于 --channel / --no-channel：
  下游服务默认 channel=chrome（system Chrome）。如果在没装 Chrome 的环境
  （CI / 容器）跑，用 --no-channel 回落到 Playwright bundled chromium。
  注意：在 macOS 26.4 + Cursor 进程上下文里 bundled chromium 会 SIGTRAP，
  详见 app/services/aidso_keyword_heat_service.py 顶部踩坑笔记。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.fetch_aidso_keyword_heat import main as _canonical_main


def main() -> int:
    return _canonical_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())