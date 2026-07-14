#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ingest.builders.article_metric_observation_builder import (
    build_article_metric_observations,
)
from app.ingest.builders.keyword_read_metric_builder import build_keyword_read_deltas
from app.ingest.common import NORMALIZED_DIR, now_iso
from app.services.agent_projection_service import build_agent_projection


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建文章指标观测事实表")
    parser.add_argument("--normalized-dir", default=str(NORMALIZED_DIR), help="normalized 数据目录")
    parser.add_argument("--output", default="article_metric_observations.json", help="观测表输出文件名或路径")
    parser.add_argument("--keyword-output", default="keyword_read_deltas.json", help="关键词阅读增量视图输出文件名或路径")
    parser.add_argument("--meta-output", default="article_metric_observations_meta.json", help="构建统计输出文件名或路径")
    parser.add_argument("--window-days", type=int, default=15, help="关键词阅读增量统计窗口")
    parser.add_argument("--limit", type=int, default=0, help="调试用：最多扫描多少个 Markdown 路径，0 表示不限制")
    parser.add_argument("--skip-keyword-deltas", action="store_true", help="只生成文章观测表，不生成关键词阅读增量视图")
    parser.add_argument("--skip-agent-brief", action="store_true", help="不生成每日 Agent 观察包")
    return parser.parse_args()


def resolve_output(normalized_dir: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    if len(path.parts) == 1:
        return normalized_dir / path
    return (PROJECT_ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    normalized_dir = Path(args.normalized_dir).expanduser().resolve()
    articles = load_json(normalized_dir / "articles.json")
    snapshots = load_json(normalized_dir / "snapshots.json")
    from app.repositories.keyword_registry_repo import KeywordRegistryRepository
    keywords = KeywordRegistryRepository(
        normalized_dir.parent / "data/state/app.db"
    ).list_keywords()
    ranking_hits = load_json(normalized_dir / "ranking_hits.json")
    snapshot_terms = load_json(normalized_dir / "snapshot_terms.json")

    observations, observation_stats = build_article_metric_observations(
        articles,
        snapshots,
        limit=args.limit or None,
    )
    output_path = resolve_output(normalized_dir, args.output)
    write_json(output_path, observations)

    keyword_stats: dict[str, Any] = {}
    keyword_output_path = None
    if not args.skip_keyword_deltas:
        keyword_rows, keyword_stats = build_keyword_read_deltas(
            observations,
            keywords,
            snapshots,
            ranking_hits,
            articles=articles,
            snapshot_terms=snapshot_terms,
            window_days=args.window_days,
        )
        keyword_output_path = resolve_output(normalized_dir, args.keyword_output)
        write_json(keyword_output_path, keyword_rows)

    meta = {
        "generated_at": now_iso(),
        "source_files": {
            "articles": str(normalized_dir / "articles.json"),
            "snapshots": str(normalized_dir / "snapshots.json"),
            "keywords": str(normalized_dir.parent / "data/state/app.db") + ":keyword_registry",
            "ranking_hits": str(normalized_dir / "ranking_hits.json"),
            "snapshot_terms": str(normalized_dir / "snapshot_terms.json"),
        },
        "outputs": {
            "article_metric_observations": str(output_path),
            "keyword_read_deltas": str(keyword_output_path) if keyword_output_path else None,
        },
        "observation_stats": observation_stats,
        "keyword_delta_stats": keyword_stats,
    }
    meta_output_path = resolve_output(normalized_dir, args.meta_output)
    write_json(meta_output_path, meta)

    print(f"[ok] observations={len(observations)} -> {output_path}")
    if keyword_output_path:
        print(f"[ok] keyword_deltas={keyword_stats.get('keywords', 0)} ok={keyword_stats.get('keywords_ok', 0)} -> {keyword_output_path}")
    print(f"[ok] meta -> {meta_output_path}")
    if not args.skip_agent_brief:
        brief_result = build_agent_projection(PROJECT_ROOT)
        print(
            f"[ok] agent_brief={brief_result['brief']['brief_id']} "
            f"articles={brief_result['brief']['summary']['recent_article_count']} "
            f"candidates={len(brief_result['brief']['event_candidates'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
