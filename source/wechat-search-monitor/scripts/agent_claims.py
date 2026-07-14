#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.agent_projection_service import apply_claim_decision


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="写入每日播报事件账本")
    subparsers = parser.add_subparsers(dest="command", required=True)
    apply_parser = subparsers.add_parser("apply", help="根据日报决策文件更新事件账本")
    apply_parser.add_argument("--decision-file", required=True, help="data/agent/decisions/ 下的 JSON 文件")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "apply":
        result = apply_claim_decision(PROJECT_ROOT, args.decision_file)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

