#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计人工预测词是否有下拉词/关联词事实证据。

这不是微信用户搜索日志。它只把同一历史窗口内的“关联词”和“下拉词”
分开计数：关联词重复出现是较强的替代候选证据；仅见下拉词时，不自动把
它判成真实用户表达，也不修改关键词词库。
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
PREDICTED_SOURCES = {"confirmed_seed", "predicted_seed"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审计预测关键词的 term 证据")
    parser.add_argument(
        "--input",
        default=str(ROOT / "data/keyword_lists/2026-07-11_上涨候选词150.json"),
        help="扩词候选 JSON",
    )
    parser.add_argument("--start", help="统计开始日期；默认读取候选文件 comparison")
    parser.add_argument("--end", help="统计结束日期；默认读取候选文件 comparison")
    parser.add_argument(
        "--output-md",
        default=str(ROOT / "临时产物/260711_预测词真实词审计.md"),
        help="Markdown 报告路径",
    )
    parser.add_argument(
        "--output-json",
        default=str(ROOT / "临时产物/260711_预测词真实词审计.json"),
        help="JSON 报告路径",
    )
    return parser.parse_args()


def normalize(text: object) -> str:
    """统一空白和英文大小写，避免 AIA/aia 这种写法被误计为不同词。"""
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def _parse_comparison_date_range(payload: dict[str, Any]) -> tuple[str, str]:
    comparison = payload.get("comparison") or {}
    base = str(comparison.get("base") or "")
    recent = str(comparison.get("recent") or "")
    try:
        start = base.split("~", 1)[0].strip()
        end = recent.split("~", 1)[1].strip()
    except IndexError as exc:
        raise ValueError("候选文件缺少 comparison.base/recent 日期范围") from exc
    if not start or not end:
        raise ValueError("候选文件 comparison 日期为空")
    return start, end


def _bigrams(text: str) -> set[str]:
    compact = normalize(text).replace(" ", "")
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


def _lexical_similarity(left: str, right: str) -> float:
    """仅用于列候选，不把字面相似直接当成语义等价。"""
    left_norm, right_norm = normalize(left), normalize(right)
    left_grams, right_grams = _bigrams(left_norm), _bigrams(right_norm)
    jaccard = (
        len(left_grams & right_grams) / len(left_grams | right_grams)
        if left_grams | right_grams
        else 0.0
    )
    sequence = SequenceMatcher(None, left_norm, right_norm).ratio()
    containment = 1.0 if left_norm in right_norm or right_norm in left_norm else 0.0
    return round(0.55 * jaccard + 0.35 * sequence + 0.10 * containment, 4)


def _build_term_stats(start: str, end: str) -> dict[str, dict[str, Any]]:
    snapshots = json.loads((ROOT / "normalized/snapshots.json").read_text(encoding="utf-8"))
    terms = json.loads((ROOT / "normalized/snapshot_terms.json").read_text(encoding="utf-8"))

    # 每个 keyword × 日期只留 primary 成功快照，避免多次批跑扩大计数。
    selected_snapshots = {
        str(item["snapshot_id"]): item
        for item in snapshots
        if item.get("is_primary")
        and item.get("status", "success") == "success"
        and start <= str(item.get("snapshot_date") or "") <= end
    }

    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "related_occurrences": 0,
            "suggestion_occurrences": 0,
            "related_dates": set(),
            "suggestion_dates": set(),
            "related_parents": set(),
            "suggestion_parents": set(),
            "best_related_position": None,
            "variants": set(),
        }
    )
    for term in terms:
        snapshot = selected_snapshots.get(str(term.get("snapshot_id") or ""))
        if not snapshot:
            continue
        term_text = str(term.get("term_text") or "").strip()
        key = normalize(term_text)
        if not key:
            continue
        record = stats[key]
        record["variants"].add(term_text)
        term_type = str(term.get("term_type") or "")
        date = str(snapshot.get("snapshot_date") or "")
        parent = str(snapshot.get("keyword_id") or "")
        position = int(term.get("position") or 99)
        if term_type == "related":
            record["related_occurrences"] += 1
            record["related_dates"].add(date)
            record["related_parents"].add(parent)
            previous = record["best_related_position"]
            record["best_related_position"] = (
                position if previous is None else min(previous, position)
            )
        elif term_type == "suggestion":
            record["suggestion_occurrences"] += 1
            record["suggestion_dates"].add(date)
            record["suggestion_parents"].add(parent)
    return stats


def _evidence_level(record: dict[str, Any]) -> str:
    related = int(record["related_occurrences"])
    parents = len(record["related_parents"] - {""})
    if related == 0:
        return "无关联词证据"
    if related < 3 and parents < 2:
        return "关联词证据弱"
    return "关联词证据足够"


def _alternatives(
    keyword_text: str,
    *,
    stats: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    alternatives: list[dict[str, Any]] = []
    own_key = normalize(keyword_text)
    for candidate_key, record in stats.items():
        related = int(record["related_occurrences"])
        dates = len(record["related_dates"])
        if candidate_key == own_key or related < 3 or dates < 2:
            continue
        similarity = _lexical_similarity(keyword_text, candidate_key)
        if similarity < 0.38:
            continue
        alternatives.append(
            {
                "term_text": sorted(record["variants"], key=lambda value: (len(value), value))[0],
                "lexical_similarity": similarity,
                "related_occurrences": related,
                "related_dates": dates,
                "related_parents": len(record["related_parents"] - {""}),
                "best_related_position": record["best_related_position"],
            }
        )
    alternatives.sort(
        key=lambda item: (
            -float(item["lexical_similarity"]),
            -int(item["related_occurrences"]),
            -int(item["related_parents"]),
            int(item["best_related_position"] or 99),
            str(item["term_text"]),
        )
    )
    return alternatives[:5]


def _build_row(item: dict[str, Any], stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    keyword_text = str(item.get("keyword_text") or "").strip()
    record = stats[normalize(keyword_text)]
    return {
        "keyword_text": keyword_text,
        "group_label": item.get("group_label"),
        "import_source": item.get("source"),
        "evidence_level": _evidence_level(record),
        "related_occurrences": int(record["related_occurrences"]),
        "related_dates": len(record["related_dates"]),
        "related_parents": len(record["related_parents"] - {""}),
        "suggestion_occurrences": int(record["suggestion_occurrences"]),
        "suggestion_dates": len(record["suggestion_dates"]),
        "suggestion_parents": len(record["suggestion_parents"] - {""}),
        "alternatives": _alternatives(keyword_text, stats=stats),
    }


def _format_alternatives(row: dict[str, Any]) -> str:
    items = row["alternatives"]
    if not items:
        return "—"
    return "；".join(
        f"{item['term_text']}（关联{item['related_occurrences']}次，"
        f"{item['related_dates']}天，{item['related_parents']}父词）"
        for item in items[:3]
    )


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    summary = payload["summary"]
    window = payload["window"]
    lines = [
        "# 预测词与下拉/关联词审计",
        "",
        "## 口径",
        "",
        f"- 窗口：{window['start']} 至 {window['end']}。",
        "- 只计成功的 `primary` 快照；同一监控词同一天多次刷新不重复放大出现次数。",
        "- “关联词”与“下拉词”分开计数。下拉词可能是系统建议，不能单独当作真实查询表达的强证据。",
        "- 相近候选仅来自重复出现的关联词；按字面相似度列出，不自动修改词库。",
        "",
        "## 结论",
        "",
        f"- 本轮 {summary['imported_total']} 词中，人工预测种子 {summary['predicted_seed_total']} 个；"
        f"原始 term 精确上涨词 {summary['term_rise_total']} 个。",
        f"- 预测种子中 {summary['weak_predicted_seed_total']} 个证据弱，其中 "
        f"{summary['no_related_predicted_seed_total']} 个在关联词中为 0 次。",
        f"- 原始 term 上涨词中另有 {summary['suggestion_only_term_rise_total']} 个只见下拉词、未见关联词。",
        "",
        "## 证据弱的预测种子（优先替换/归档复核）",
        "",
        "| 预测词 | 关联词次数 / 日期 / 父词 | 下拉词次数 | 重复出现的关联词候选 |",
        "|---|---|---:|---|",
    ]
    for row in payload["weak_predicted_seeds"]:
        lines.append(
            f"| {row['keyword_text']} | {row['related_occurrences']} / "
            f"{row['related_dates']} / {row['related_parents']} | "
            f"{row['suggestion_occurrences']} | {_format_alternatives(row)} |"
        )
    lines += [
        "",
        "## 只见下拉词的原始 term 上涨词（不应单独当作强真实词）",
        "",
        "| 词 | 下拉词次数 | 重复出现的关联词候选 |",
        "|---|---:|---|",
    ]
    for row in payload["suggestion_only_term_rise"]:
        lines.append(
            f"| {row['keyword_text']} | {row['suggestion_occurrences']} | "
            f"{_format_alternatives(row)} |"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    source = json.loads(input_path.read_text(encoding="utf-8"))
    start, end = _parse_comparison_date_range(source)
    start = args.start or start
    end = args.end or end
    stats = _build_term_stats(start, end)
    rows = [_build_row(item, stats) for item in source.get("candidates", [])]
    weak_predicted = [
        row
        for row in rows
        if row["import_source"] in PREDICTED_SOURCES
        and row["evidence_level"] != "关联词证据足够"
    ]
    suggestion_only_rise = [
        row
        for row in rows
        if row["import_source"] == "term_rise" and row["related_occurrences"] == 0
    ]
    payload = {
        "window": {
            "start": start,
            "end": end,
            "selection": "仅成功 primary 快照；同一关键词同一天不重复计数",
        },
        "summary": {
            "imported_total": len(rows),
            "predicted_seed_total": sum(
                row["import_source"] in PREDICTED_SOURCES for row in rows
            ),
            "term_rise_total": sum(row["import_source"] == "term_rise" for row in rows),
            "weak_predicted_seed_total": len(weak_predicted),
            "no_related_predicted_seed_total": sum(
                row["related_occurrences"] == 0 for row in weak_predicted
            ),
            "suggestion_only_term_rise_total": len(suggestion_only_rise),
        },
        "weak_predicted_seeds": weak_predicted,
        "suggestion_only_term_rise": suggestion_only_rise,
    }
    output_json = Path(args.output_json).expanduser().resolve()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    output_md = Path(args.output_md).expanduser().resolve()
    _write_markdown(payload, output_md)
    print(json.dumps(payload["summary"], ensure_ascii=False))
    print(f"Markdown: {output_md}")
    print(f"JSON: {output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
