#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.keyword_discovery_service import run_discovery_cycle


def main() -> int:
    parser = argparse.ArgumentParser(description="运行动态关键词探真与生命周期闭环")
    parser.add_argument(
        "--launch",
        action="store_true",
        help="如无批次运行，按每日发现预算启动轻量探针/候选搜索",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "data/state/keyword_discovery_last_run.json"),
    )
    args = parser.parse_args()
    result = run_discovery_cycle(project_root=ROOT, auto_launch=args.launch)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
