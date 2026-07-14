#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.agent_projection_service import build_agent_projection, validate_agent_projection


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建每日 Agent 观察包")
    parser.add_argument("--validate", action="store_true", help="构建后校验观察包和证据索引")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = build_agent_projection(PROJECT_ROOT)
    summary = {
        "brief_id": result["brief"]["brief_id"],
        "manifest": str(result["paths"]["manifest"]),
        "brief": str(result["paths"]["brief"]),
        "recent_article_count": result["brief"]["summary"]["recent_article_count"],
        "event_candidate_count": len(result["brief"]["event_candidates"]),
        "data_mode": result["brief"]["data_status"]["mode"],
    }
    if args.validate:
        validation = validate_agent_projection(PROJECT_ROOT)
        summary["validation"] = validation
        if not validation["valid"]:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

