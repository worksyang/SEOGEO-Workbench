#!/usr/bin/env python3
"""
AIDSO 单关键词热度抓取 CLI（脚本入口）。

默认行为：
  - 走 system Google Chrome（Playwright channel="chrome"）
  - 默认无头抓取
  - 若登录态失效，则自动拉起有头浏览器等待扫码，成功后回到原目标模式继续抓取
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config
from app.services.aidso_keyword_heat_service import (
    DEFAULT_BROWSER_CHANNEL,
    AidsoHeatError,
    ensure_aidso_login,
    resolve_aidso_keyword_heat,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AIDSO 单关键词热度抓取")
    parser.add_argument("keyword", nargs="?", default="友邦环宇", help="关键词")
    parser.add_argument(
        "--login",
        action="store_true",
        help="只打开有头浏览器，等待扫码登录完成，不执行抓取",
    )
    parser.add_argument(
        "--profile-dir",
        default=str(Config.AIDSO_PLAYWRIGHT_PROFILE_DIR),
        help="Playwright 持久化 profile 目录",
    )
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument(
        "--show",
        action="store_true",
        help="最终抓取时使用有头模式（默认无头）",
    )
    visibility.add_argument(
        "--headless",
        action="store_true",
        help="显式声明无头模式（与默认行为一致）",
    )
    parser.add_argument(
        "--no-auto-login",
        action="store_true",
        help="登录态失效时不自动拉起扫码窗口，直接报错",
    )
    parser.add_argument(
        "--login-wait-timeout-ms",
        type=int,
        default=300_000,
        help="自动补登录或显式登录时，等待扫码完成的最长时间（毫秒）",
    )
    parser.add_argument("--pretty", "--json", dest="pretty", action="store_true", help="输出格式化 JSON")
    channel_group = parser.add_mutually_exclusive_group()
    channel_group.add_argument(
        "--channel",
        default=DEFAULT_BROWSER_CHANNEL,
        help=f"Playwright channel（chrome / msedge 等），默认 {DEFAULT_BROWSER_CHANNEL}",
    )
    channel_group.add_argument(
        "--no-channel",
        action="store_true",
        help="不指定 channel，回落到 bundled chromium（macOS+Cursor 环境慎用）",
    )
    parser.add_argument(
        "--executable-path",
        default=None,
        help="自定义浏览器可执行文件路径（一旦设置则忽略 --channel）",
    )
    return parser.parse_args(argv)


def _resolve_channel(args: argparse.Namespace) -> str | None:
    if args.no_channel:
        return None
    return args.channel or None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    channel = _resolve_channel(args)
    headless = not args.show
    try:
        if args.login:
            payload = ensure_aidso_login(
                profile_dir=args.profile_dir,
                keyword=args.keyword,
                wait_timeout_ms=args.login_wait_timeout_ms,
                channel=channel,
                executable_path=args.executable_path,
            )
        else:
            payload = resolve_aidso_keyword_heat(
                keyword=args.keyword,
                profile_dir=args.profile_dir,
                headless=headless,
                auto_login=not args.no_auto_login,
                login_wait_timeout_ms=args.login_wait_timeout_ms,
                channel=channel,
                executable_path=args.executable_path,
            )
    except (AidsoHeatError, ValueError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    if args.login:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    detail = payload.get("detail") or {}
    print(f"关键词: {payload['keyword']}")
    print(f"收录: {'是' if payload.get('found') else '否'}")
    print(f"月均搜索量: {detail.get('month_cover_count_str') or '-'}")
    print(f"月均点击量: {detail.get('month_click_count') or '-'}")
    print(f"下拉词数量: {detail.get('down_keyword_count') or '-'}")
    print(f"下拉词月覆盖: {detail.get('down_keyword_month_covercount') or '-'}")
    print(f"竞争程度: {detail.get('competition_cn') or detail.get('competition') or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
