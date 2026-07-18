"""扫描现有微信 contents，回填正文阅读/互动指标。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from content_hub.config import Settings  # noqa: E402
from content_hub.services.wechat_article_metrics import (  # noqa: E402
    WechatArticleMetricsService,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observed-at", help="观测时间；默认当前 UTC")
    parser.add_argument("--source-ref", help="统一来源引用；默认每篇 asset 路径")
    parser.add_argument("--database", type=Path, help="覆盖 Hub SQLite 路径")
    args = parser.parse_args(argv)

    settings = Settings.load()
    if args.database:
        from dataclasses import replace
        settings = replace(settings, database_path=args.database.resolve(), lock_path=args.database.resolve().with_suffix(".lock"))
    stats = WechatArticleMetricsService(settings).backfill_contents(
        observed_at=args.observed_at, source_ref=args.source_ref
    )
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
