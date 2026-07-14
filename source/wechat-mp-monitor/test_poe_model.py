#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最小 Poe 模型测试脚本。

用途：
1. 快速验证 Poe API 是否可连通；
2. 验证代理是否生效；
3. 验证指定模型是否能正常返回内容。
"""
import argparse
import os
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SOMEURL2MD_DIR = os.path.join(PROJECT_DIR, "SomeURL2MD")

if SOMEURL2MD_DIR not in sys.path:
    sys.path.insert(0, SOMEURL2MD_DIR)

from openaiapi import OpenAIAPIService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="测试 Poe 模型连通性")
    parser.add_argument("--platform", default="poe", help="平台名，默认 poe")
    parser.add_argument("--model", default="gemini-3-flash", help="模型名，默认 gemini-3-flash")
    parser.add_argument(
        "--prompt",
        default='只回复"ok"这两个字，不要加任何解释。',
        help="测试提示词",
    )
    parser.add_argument("--max-tokens", type=int, default=20, help="最大输出 token")
    parser.add_argument(
        "--thinking-level",
        default="minimal",
        choices=["minimal", "low", "high"],
        help="Gemini-3-Flash 的 thinking_level",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    config = OpenAIAPIService.load_config()
    service = OpenAIAPIService(config)
    service.set_platform(args.platform)
    service.set_model(args.model)

    extra_body = {}
    if "gemini-3" in args.model.lower():
        extra_body["thinking_level"] = args.thinking_level

    print("\n" + "=" * 60)
    print("🚀 开始测试 Poe 模型")
    print("=" * 60)
    print(f"平台: {args.platform}")
    print(f"模型: {args.model}")
    print(f"prompt: {args.prompt}")

    started_at = time.time()
    response = service.generate_text(
        prompt=args.prompt,
        stream=False,
        max_tokens=args.max_tokens,
        extra_body=extra_body,
    )
    elapsed = time.time() - started_at

    print(f"\n耗时: {elapsed:.2f}s")
    if response is None:
        print("❌ 测试失败：未收到有效响应")
        return 1

    print(f"✅ 测试成功，响应内容: {response!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
