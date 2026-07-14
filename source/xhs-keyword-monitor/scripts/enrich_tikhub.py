"""TikHub 博主批量 enrich。

按账号命中次数取 Top N（默认 100），按 userId 顺序调 get_user_info，写
到 data/raw/tikhub/xhs/users/<user_id>.json。失败单独记 failures.jsonl。
幂等：已存在的 user_id 跳过。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.ingest.tikhub.detail_service import (  # noqa: E402
    enrich_creators_bulk, _user_path
)


LOG = logging.getLogger("enrich_tikhub")


def pick_top_user_ids(limit: int = 100) -> list[str]:
    """按 ranking_hits 中账号出现次数排序取 top N。"""
    ranking_path = PROJECT_ROOT / "normalized" / "ranking_hits.json"
    if not ranking_path.exists():
        return []
    data = json.loads(ranking_path.read_text(encoding="utf-8"))
    counter: Counter[str] = Counter()
    for h in data:
        aid = h.get("account_id")
        if aid:
            counter[aid] += 1
    # 按出现次数 desc，再按 userId asc 保证稳定
    return [uid for uid, _ in counter.most_common(limit)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--inter-delay", type=float, default=0.3)
    parser.add_argument("--include-disabled", action="store_true",
                        help="包含 disabled 关键词（默认跳过）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if not Config.TIKHUB_API_TOKEN:
        LOG.error("TIKHUB_API_TOKEN 未设置；拒绝执行。")
        return 2

    top_ids = pick_top_user_ids(args.limit)
    LOG.info("Enriching Top %d 博主（按命中次数）", len(top_ids))

    success, fail, failures = enrich_creators_bulk(top_ids, inter_delay=args.inter_delay)

    LOG.info("done: success=%d fail=%d", success, fail)

    if failures:
        fail_path = PROJECT_ROOT / "data" / "state" / "tikhub_enrich_failures.jsonl"
        fail_path.parent.mkdir(parents=True, exist_ok=True)
        with fail_path.open("w") as fp:
            for f in failures:
                fp.write(f + "\n")
        LOG.info("failures recorded: %s", fail_path)

    # Rebuild monitor-data so account fields enriched by detail appear
    from app.ingest.rebuild import rebuild_all
    rebuild_all(verbose=False)

    # Stats
    users_dir = PROJECT_ROOT / "data" / "raw" / "tikhub" / "xhs" / "users"
    cached = sum(1 for _ in users_dir.glob("*.json")) if users_dir.exists() else 0
    LOG.info("用户缓存: %d 个", cached)
    return 0


if __name__ == "__main__":
    sys.exit(main())
