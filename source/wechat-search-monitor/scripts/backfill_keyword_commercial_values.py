#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Config
from app.repositories.keyword_registry_repo import KeywordRegistryRepository
from app.services.keyword_commercial_value_service import score_keyword


def main() -> int:
    parser = argparse.ArgumentParser(description="回填关键词商业价值控制层评分")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写入；默认只输出预演结果",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "data/state/keyword_commercial_value_backfill.json"),
    )
    args = parser.parse_args()

    repository = KeywordRegistryRepository(Config.SQLITE_PATH)
    rows = repository.list_keywords(include_archived=False)
    distribution: Counter[int] = Counter()
    changes: list[dict[str, object]] = []
    updates: dict[str, dict[str, object]] = {}
    for row in rows:
        result = score_keyword(row["keyword_text"])
        score = int(result["commercial_value_score"])
        distribution[score] += 1
        if row.get("commercial_value_source") == "manual":
            continue
        updates[row["keyword_id"]] = result
        if score != int(row.get("commercial_value_score") or 5):
            changes.append({
                "keyword_id": row["keyword_id"],
                "keyword_text": row["keyword_text"],
                "before": int(row.get("commercial_value_score") or 5),
                "after": score,
                "reason": result["commercial_value_reason"],
            })

    updated_count = (
        repository.apply_auto_commercial_values(updates)
        if args.apply
        else 0
    )
    payload = {
        "mode": "apply" if args.apply else "dry_run",
        "evaluated_count": len(rows),
        "would_change_count": len(changes),
        "updated_count": updated_count,
        "distribution": dict(sorted(distribution.items())),
        "changes": changes,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in payload.items() if key != "changes"}, ensure_ascii=False, indent=2))
    print(f"detail_file={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
