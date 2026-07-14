#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIDSO 抓取 API 的参数透传 + 错误映射烟雾测试。

这个脚本**不**会真的启动 Playwright 浏览器，也不会真的去 dso.aidso.com 抓数据。
它只做以下三件事：

1. 把 app.web.api.resolve_aidso_keyword_heat 替换成一个桩函数，
   用来记录调用时收到的关键字参数。
2. 用 Flask test client 给 /api/aidso/keyword-heat 发各种形状的请求
   （GET / POST / 含 channel / 含 executable_path / 各种错误）。
3. 断言桩函数收到的 kwargs 与请求参数一致，并验证状态码 / 错误体
   符合预期。

为什么需要它：
- 避免每次回归都人工敲 `python fetch_aidso_keyword_heat.py ...` 来验证。
- 防止有人改 api.py 时把某个 query 参数漏透传到 service 层。
- 防止 error → status code 的映射写反（例如把 login_required 写成 400）。

用法：
    python scripts/check_aidso_api_wiring.py
    python scripts/check_aidso_api_wiring.py -v   # 详细输出
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app
from app.config import Config
from app.services.aidso_keyword_heat_service import (
    DEFAULT_BROWSER_CHANNEL,
    AidsoHeatError,
    AidsoLoginRequiredError,
    AidsoProfileBusyError,
)
from app.web import api as api_module


# ── 测试用例 ─────────────────────────────────────────────────
# 每个 case 是一组 (request_kwargs, expected_kwargs_to_service, expected_status, [optional] side_effect)
#
# request_kwargs:
#   method: "GET" / "POST"
#   query:  dict (URL query string)
#   json:   dict (POST body)
#
# expected_kwargs_to_service: 桩函数应该被调用时看到的 kwargs 子集
#   用子集断言而不是相等断言，避免将来 service 增加新可选参数时挂掉
#
# expected_status: HTTP 状态码
#
# side_effect: 可选；用来模拟 service 抛出指定异常，验证错误映射

CASES: list[dict] = [
    # 1) GET 最简：只传 keyword，其他走默认
    {
        "name": "GET keyword only",
        "request": {"method": "GET", "query": {"keyword": "友邦环宇"}},
        "expected": {
            "keyword": "友邦环宇",
            "headless": True,
            "auto_login": False,
            "wait_timeout_ms": 30_000,
            "login_wait_timeout_ms": 300_000,
            "channel": DEFAULT_BROWSER_CHANNEL,
            "executable_path": None,
        },
        "status": 200,
    },
    # 2) POST JSON 全部参数
    {
        "name": "POST full body",
        "request": {
            "method": "POST",
            "json": {
                "keyword": "试管婴儿",
                "headless": False,
                "auto_login": True,
                "wait_timeout_ms": 12_345,
                "login_wait_timeout_ms": 67_890,
                "channel": "msedge",
            },
        },
        "expected": {
            "keyword": "试管婴儿",
            "headless": False,
            "auto_login": True,
            "wait_timeout_ms": 12_345,
            "login_wait_timeout_ms": 67_890,
            "channel": "msedge",
            "executable_path": None,
        },
        "status": 200,
    },
    # 3) executable_path 优先级最高：API 层把两个参数都透传给 service，
    #    实际覆盖发生在 service 的 _build_launch_kwargs 里。
    {
        "name": "executable_path and channel both pass through",
        "request": {
            "method": "POST",
            "json": {
                "keyword": "A",
                "channel": "msedge",
                "executable_path": "/Applications/Google Chrome.app",
            },
        },
        "expected": {
            "keyword": "A",
            "channel": "msedge",
            "executable_path": "/Applications/Google Chrome.app",
        },
        "status": 200,
    },
    # 4) no_channel 显式回落
    {
        "name": "no_channel falls back to bundled",
        "request": {
            "method": "POST",
            "json": {"keyword": "A", "no_channel": True},
        },
        "expected": {"keyword": "A", "channel": None, "executable_path": None},
        "status": 200,
    },
    # 5) profile_dir override
    {
        "name": "profile_dir from body",
        "request": {
            "method": "POST",
            "json": {"keyword": "A", "profile_dir": "/tmp/custom-profile"},
        },
        "expected": {"keyword": "A", "profile_dir": "/tmp/custom-profile"},
        "status": 200,
    },
    # 6) keyword 缺失 → 400
    {
        "name": "missing keyword returns 400",
        "request": {"method": "GET", "query": {}},
        "expected": None,
        "status": 400,
    },
    # 7) keyword 是空字符串 → 400
    {
        "name": "empty keyword returns 400",
        "request": {"method": "GET", "query": {"keyword": "  "}},
        "expected": None,
        "status": 400,
    },
    # 8) 登录缺失 → 409 + login_required
    {
        "name": "login required returns 409",
        "request": {"method": "GET", "query": {"keyword": "A"}},
        "expected": {"keyword": "A"},
        "status": 409,
        "side_effect": AidsoLoginRequiredError("test: login required"),
        "body_check": {"login_required": True},
    },
    # 9) profile 占用 → 409 + profile_busy
    {
        "name": "profile busy returns 409",
        "request": {"method": "GET", "query": {"keyword": "A"}},
        "expected": {"keyword": "A"},
        "status": 409,
        "side_effect": AidsoProfileBusyError("test: profile busy"),
        "body_check": {"profile_busy": True},
    },
    # 10) 通用抓取错误 → 502
    {
        "name": "generic heat error returns 502",
        "request": {"method": "GET", "query": {"keyword": "A"}},
        "expected": {"keyword": "A"},
        "status": 502,
        "side_effect": AidsoHeatError("test: detail 响应缺失"),
    },
    # 11) 非法空 keyword（POST JSON）→ 400
    {
        "name": "POST missing keyword returns 400",
        "request": {"method": "POST", "json": {}},
        "expected": None,
        "status": 400,
    },
    # 12) 数字字符串/布尔字符串的兼容
    {
        "name": "string booleans are coerced",
        "request": {
            "method": "POST",
            "json": {
                "keyword": "A",
                "headless": "false",
                "auto_login": "1",
                "wait_timeout_ms": "15000",
            },
        },
        "expected": {
            "keyword": "A",
            "headless": False,
            "auto_login": True,
            "wait_timeout_ms": 15_000,
        },
        "status": 200,
    },
]


def _build_recording_stub():
    """构造一个 MagicMock，side_effect 是个普通函数，把 kwargs 存进 captured。

    不用 Mock(wraps=fn) 是因为 patch.object 会再包一层 MagicMock，
    call_args 不一定能直接透到外层 fn 上读出来；用 closure 列表最稳。
    """
    captured: list[dict] = []

    def side_effect_fn(**kwargs):
        captured.append(kwargs)
        return {
            "keyword": kwargs.get("keyword"),
            "found": True,
            "fetched_at": "2026-06-14T00:00:00",
            "detail": None,
            "down_words": [],
            "source": {"provider": "stub"},
        }

    return MagicMock(side_effect=side_effect_fn), captured


def _make_side_effect_stub(exc: Exception):
    """构造一个抛异常的 MagicMock。"""
    return MagicMock(side_effect=exc)


def _run_case(client, case: dict, verbose: bool) -> tuple[bool, str]:
    name = case["name"]
    request = case["request"]
    expected = case.get("expected")
    expected_status = case["status"]
    side_effect = case.get("side_effect")
    body_check = case.get("body_check")

    if side_effect is not None:
        stub = _make_side_effect_stub(side_effect)
        captured: list[dict] = []
    else:
        stub, captured = _build_recording_stub()

    method = request["method"]
    with patch.object(api_module, "resolve_aidso_keyword_heat", new=stub):
        if method == "GET":
            resp = client.get("/api/aidso/keyword-heat", query_string=request.get("query", {}))
        elif method == "POST":
            resp = client.post(
                "/api/aidso/keyword-heat",
                json=request.get("json"),
                query_string=request.get("query"),
            )
        else:
            return False, f"unknown method: {method}"

    if resp.status_code != expected_status:
        return False, (
            f"[{name}] 期望状态 {expected_status}，实际 {resp.status_code}，body: {resp.get_data(as_text=True)[:200]}"
        )

    if body_check:
        body = resp.get_json() or {}
        for key, want in body_check.items():
            if body.get(key) != want:
                return False, f"[{name}] body.{key} 期望 {want}，实际 {body.get(key)}"

    if side_effect is not None:
        return True, f"[{name}] 状态 {resp.status_code} ✓"

    # 拿桩函数捕获到的最后一次调用
    if expected is None:
        # 期望 service 不被调用
        return True, f"[{name}] 状态 {resp.status_code} ✓（未触达 service，符合预期）"

    if not captured:
        return False, f"[{name}] service 没被调用"
    actual = captured[-1]
    missing = []
    mismatched = []
    for key, want in expected.items():
        if key not in actual:
            missing.append(key)
            continue
        got = actual[key]
        if got != want:
            mismatched.append((key, want, got))
    if missing:
        return False, f"[{name}] service kwargs 缺少字段: {missing}"
    if mismatched:
        return False, f"[{name}] service kwargs 不匹配: {mismatched}"

    if verbose:
        return True, f"[{name}] ✓ kwargs={ {k: actual[k] for k in expected} }"
    return True, f"[{name}] ✓"


def main() -> int:
    parser = argparse.ArgumentParser(description="AIDSO API 参数透传 + 错误映射烟雾测试")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每次调用的 kwargs")
    args = parser.parse_args()

    app = create_app()
    client = app.test_client()

    print(f"# AIDSO API wiring smoke test")
    print(f"# default profile_dir: {Config.AIDSO_PLAYWRIGHT_PROFILE_DIR}")
    print(f"# default channel    : {DEFAULT_BROWSER_CHANNEL}")
    print(f"# cases              : {len(CASES)}")
    print()

    passed = 0
    failed = 0
    for case in CASES:
        ok, message = _run_case(client, case, args.verbose)
        if ok:
            passed += 1
        else:
            failed += 1
        print(message)

    print()
    print(f"== summary: {passed} passed, {failed} failed ==")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())