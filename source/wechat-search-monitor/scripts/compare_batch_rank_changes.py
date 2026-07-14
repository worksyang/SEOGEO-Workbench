#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ingest.common import article_identity_key
from app.ingest.parsers.search_md_parser import ParsedHit, ParsedSnapshot, parse_search_markdown


RUNS_ROOT = PROJECT_ROOT / "data" / "runs"
OUTPUT_ROOT = PROJECT_ROOT / "微信搜索结果" / "批量抓取"


@dataclass
class HitView:
    key: str
    rank: int
    title: str
    account: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="比较两个关键词批次的排名变化")
    parser.add_argument("--base-batch", required=True, help="基线批次 ID")
    parser.add_argument("--new-batch", required=True, help="新批次 ID")
    parser.add_argument("--runs-root", default=str(RUNS_ROOT), help="批次状态根目录")
    parser.add_argument("--output-root", default=str(OUTPUT_ROOT), help="批次结果根目录")
    parser.add_argument("--top-n", type=int, default=3, help="比较前 N 名")
    parser.add_argument("--output", default="", help="输出 JSON 文件路径")
    return parser.parse_args()


def find_batch_search_files(batch_id: str, output_root: Path) -> list[Path]:
    batch_dir = output_root / batch_id
    if not batch_dir.exists():
        raise FileNotFoundError(f"结果目录不存在：{batch_dir}")
    return sorted(batch_dir.rglob("*搜索结果_*.md"))


def load_batch_snapshots(batch_id: str, output_root: Path) -> dict[str, ParsedSnapshot]:
    snaps: dict[str, ParsedSnapshot] = {}
    for path in find_batch_search_files(batch_id, output_root):
        snap = parse_search_markdown(path)
        if not snap:
            continue
        existing = snaps.get(snap.keyword_text)
        if existing is None or snap.captured_at > existing.captured_at:
            snaps[snap.keyword_text] = snap
    return snaps


def hit_to_view(hit: ParsedHit) -> HitView:
    return HitView(
        key=article_identity_key(hit.account_name_raw, hit.title_raw),
        rank=hit.rank,
        title=hit.title_raw,
        account=hit.account_name_raw,
    )


def snapshot_to_views(snapshot: ParsedSnapshot) -> list[HitView]:
    return [hit_to_view(hit) for hit in sorted(snapshot.hits, key=lambda item: item.rank)]


def build_keyword_diff(keyword: str, base: ParsedSnapshot, new: ParsedSnapshot, top_n: int) -> dict[str, Any]:
    base_views = snapshot_to_views(base)
    new_views = snapshot_to_views(new)

    base_map = {item.key: item for item in base_views}
    new_map = {item.key: item for item in new_views}
    base_top = base_views[:top_n]
    new_top = new_views[:top_n]
    base_top_keys = {item.key for item in base_top}
    new_top_keys = {item.key for item in new_top}

    overlapping_keys = sorted(
        base_map.keys() & new_map.keys(),
        key=lambda key: (new_map[key].rank, base_map[key].rank),
    )

    rank_changes = []
    for key in overlapping_keys:
        old_item = base_map[key]
        new_item = new_map[key]
        delta = old_item.rank - new_item.rank
        if delta == 0:
            continue
        rank_changes.append({
            "title": new_item.title,
            "account": new_item.account,
            "old_rank": old_item.rank,
            "new_rank": new_item.rank,
            "delta": delta,
        })

    entered_top = [
        {"title": item.title, "account": item.account, "new_rank": item.rank}
        for item in new_top
        if item.key not in base_top_keys
    ]
    left_top = [
        {"title": item.title, "account": item.account, "old_rank": item.rank}
        for item in base_top
        if item.key not in new_top_keys
    ]

    top1_old = base_top[0] if base_top else None
    top1_new = new_top[0] if new_top else None

    return {
        "keyword": keyword,
        "base_captured_at": base.captured_at.isoformat(timespec="seconds"),
        "new_captured_at": new.captured_at.isoformat(timespec="seconds"),
        "base_result_count": len(base_views),
        "new_result_count": len(new_views),
        "result_count_delta": len(new_views) - len(base_views),
        "top1_changed": bool(top1_old and top1_new and top1_old.key != top1_new.key),
        "topn_changed": [item.key for item in base_top] != [item.key for item in new_top],
        "top1_old": (
            {"title": top1_old.title, "account": top1_old.account, "rank": top1_old.rank}
            if top1_old else None
        ),
        "top1_new": (
            {"title": top1_new.title, "account": top1_new.account, "rank": top1_new.rank}
            if top1_new else None
        ),
        "entered_topn": entered_top,
        "left_topn": left_top,
        "rank_changes": rank_changes,
    }


def build_report(base_batch: str, new_batch: str, output_root: Path, top_n: int) -> dict[str, Any]:
    base_snaps = load_batch_snapshots(base_batch, output_root)
    new_snaps = load_batch_snapshots(new_batch, output_root)

    shared_keywords = sorted(base_snaps.keys() & new_snaps.keys())
    base_only = sorted(base_snaps.keys() - new_snaps.keys())
    new_only = sorted(new_snaps.keys() - base_snaps.keys())

    diffs = [
        build_keyword_diff(keyword, base_snaps[keyword], new_snaps[keyword], top_n=top_n)
        for keyword in shared_keywords
    ]

    top1_changed_count = sum(1 for item in diffs if item["top1_changed"])
    topn_changed_count = sum(1 for item in diffs if item["topn_changed"])
    result_count_changed_count = sum(1 for item in diffs if item["result_count_delta"] != 0)

    changed_keywords = [
        item["keyword"]
        for item in diffs
        if item["topn_changed"] or item["result_count_delta"] != 0
    ]

    return {
        "base_batch_id": base_batch,
        "new_batch_id": new_batch,
        "top_n": top_n,
        "summary": {
            "shared_keywords": len(shared_keywords),
            "base_only_keywords": len(base_only),
            "new_only_keywords": len(new_only),
            "changed_keywords": len(changed_keywords),
            "top1_changed_keywords": top1_changed_count,
            "topn_changed_keywords": topn_changed_count,
            "result_count_changed_keywords": result_count_changed_count,
        },
        "base_only_keywords": base_only,
        "new_only_keywords": new_only,
        "changed_keywords": changed_keywords,
        "keywords": diffs,
    }


def write_output(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_root).expanduser().resolve()
    report = build_report(
        base_batch=args.base_batch,
        new_batch=args.new_batch,
        output_root=output_root,
        top_n=max(args.top_n, 1),
    )

    if args.output:
        write_output(Path(args.output).expanduser().resolve(), report)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
