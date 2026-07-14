#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用历史每日快照模拟 3/7/15 天刷新，验证阅读指标的参考性。

“真值”采用同一公式在高密度历史快照上的结果；稀疏方案删除相应关键词
快照及其专属文章观测后重算。验收默认定义为：估算值落在高密度参考值的
60%～140% 之间，即相对误差不超过40%。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ingest.builders.keyword_read_metric_builder import (
    HISTORY_LOOKBACK_DAYS,
    _parse_iso,
    build_keyword_read_deltas,
)
from app.repositories.keyword_registry_repo import KeywordRegistryRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回测变频后的关键词阅读指标")
    parser.add_argument(
        "--output",
        default=str(ROOT / "临时产物/keyword_read_metric_backtest_v3.json"),
    )
    parser.add_argument("--tolerance", type=float, default=0.40)
    parser.add_argument("--minimum-pass-rate", type=float, default=0.60)
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _batch_id(snapshot: dict[str, Any]) -> str:
    match = re.search(
        r"批量抓取/((?:web|overnight)_\d{8}_\d{2,6})/",
        str(snapshot.get("source_file_path") or ""),
    )
    return match.group(1) if match else ""


def _rankdata(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while (
            end + 1 < len(order)
            and values[order[end + 1]] == values[order[index]]
        ):
            end += 1
        rank = (index + end) / 2 + 1
        for cursor in range(index, end + 1):
            result[order[cursor]] = rank
        index = end + 1
    return result


def _spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2:
        return None
    left_rank = _rankdata(left)
    right_rank = _rankdata(right)
    left_mean = sum(left_rank) / len(left_rank)
    right_mean = sum(right_rank) / len(right_rank)
    numerator = sum(
        (x - left_mean) * (y - right_mean)
        for x, y in zip(left_rank, right_rank)
    )
    left_square = sum((x - left_mean) ** 2 for x in left_rank)
    right_square = sum((y - right_mean) ** 2 for y in right_rank)
    if not left_square or not right_square:
        return None
    return numerator / math.sqrt(left_square * right_square)


def _select_dates(
    snapshots: list[dict[str, Any]],
    *,
    history_start: datetime,
    window_end: datetime,
    frequency_for: Callable[[str], int],
) -> dict[str, set[Any]]:
    dates_by_keyword: dict[str, set[Any]] = defaultdict(set)
    for snapshot in snapshots:
        captured_at = _parse_iso(snapshot.get("captured_at"))
        if (
            snapshot.get("status") == "success"
            and snapshot.get("is_primary") is True
            and captured_at
            and history_start <= captured_at <= window_end
        ):
            dates_by_keyword[str(snapshot.get("keyword_id") or "")].add(
                captured_at.date()
            )

    selected_by_keyword: dict[str, set[Any]] = {}
    for keyword_id, dates in dates_by_keyword.items():
        frequency = max(1, int(frequency_for(keyword_id)))
        selected: list[Any] = []
        for date in sorted(dates):
            if not selected or (date - selected[-1]).days >= frequency:
                selected.append(date)
        selected_by_keyword[keyword_id] = set(selected)
    return selected_by_keyword


def _downsample(
    observations: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    ranking_hits: list[dict[str, Any]],
    snapshot_terms: list[dict[str, Any]],
    *,
    history_start: datetime,
    window_end: datetime,
    frequency_for: Callable[[str], int],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    selected_dates = _select_dates(
        snapshots,
        history_start=history_start,
        window_end=window_end,
        frequency_for=frequency_for,
    )
    kept_snapshots: list[dict[str, Any]] = []
    kept_snapshot_ids: set[str] = set()
    for snapshot in snapshots:
        snapshot_id = str(snapshot.get("snapshot_id") or "")
        captured_at = _parse_iso(snapshot.get("captured_at"))
        is_target = (
            snapshot.get("status") == "success"
            and snapshot.get("is_primary") is True
            and captured_at
            and history_start <= captured_at <= window_end
        )
        if (
            is_target
            and captured_at.date()
            not in selected_dates.get(str(snapshot.get("keyword_id") or ""), set())
        ):
            continue
        kept_snapshots.append(snapshot)
        kept_snapshot_ids.add(snapshot_id)

    kept_hits = [
        hit
        for hit in ranking_hits
        if str(hit.get("snapshot_id") or "") in kept_snapshot_ids
    ]
    kept_terms = [
        term
        for term in snapshot_terms
        if str(term.get("snapshot_id") or "") in kept_snapshot_ids
    ]
    snapshots_by_id = {
        str(snapshot.get("snapshot_id") or ""): snapshot
        for snapshot in kept_snapshots
    }
    visible_batch_articles: set[tuple[str, str]] = set()
    for hit in kept_hits:
        snapshot = snapshots_by_id.get(str(hit.get("snapshot_id") or ""))
        captured_at = _parse_iso(snapshot.get("captured_at")) if snapshot else None
        if not captured_at or not history_start <= captured_at <= window_end:
            continue
        visible_batch_articles.add((
            _batch_id(snapshot),
            str(hit.get("article_id") or ""),
        ))

    kept_observations: list[dict[str, Any]] = []
    for observation in observations:
        observed_at = _parse_iso(observation.get("observed_at"))
        if (
            not observed_at
            or observed_at < history_start
            or observed_at > window_end
        ):
            kept_observations.append(observation)
            continue
        batch_id = str(observation.get("source_batch_id") or "")
        if not batch_id or (
            batch_id,
            str(observation.get("article_id") or ""),
        ) in visible_batch_articles:
            kept_observations.append(observation)
    return kept_observations, kept_snapshots, kept_hits, kept_terms


def _metric_accuracy(
    ids: list[str],
    reference: dict[str, dict[str, Any]],
    simulated: dict[str, dict[str, Any]],
    field: str,
    tolerance: float,
) -> dict[str, Any]:
    usable = [
        keyword_id
        for keyword_id in ids
        if float(reference[keyword_id].get(field) or 0) > 0
        and simulated[keyword_id].get(field) is not None
    ]
    ratios = [
        float(simulated[keyword_id][field])
        / float(reference[keyword_id][field])
        for keyword_id in usable
    ]
    errors = [abs(ratio - 1) for ratio in ratios]
    lower, upper = 1 - tolerance, 1 + tolerance
    return {
        "sample_count": len(usable),
        "median_ratio": round(median(ratios), 4) if ratios else None,
        "median_absolute_percentage_error": (
            round(median(errors), 4) if errors else None
        ),
        "within_tolerance_rate": (
            round(sum(lower <= ratio <= upper for ratio in ratios) / len(ratios), 4)
            if ratios
            else None
        ),
        "p90_absolute_percentage_error": (
            round(sorted(errors)[int(0.9 * (len(errors) - 1))], 4)
            if errors
            else None
        ),
        "spearman": (
            round(
                _spearman(
                    [float(reference[keyword_id][field]) for keyword_id in usable],
                    [float(simulated[keyword_id][field]) for keyword_id in usable],
                )
                or 0.0,
                4,
            )
            if len(usable) > 1
            else None
        ),
    }


def main() -> int:
    args = parse_args()
    normalized = ROOT / "normalized"
    observations = load_json(normalized / "article_metric_observations.json")
    articles = load_json(normalized / "articles.json")
    snapshots = load_json(normalized / "snapshots.json")
    ranking_hits = load_json(normalized / "ranking_hits.json")
    snapshot_terms = load_json(normalized / "snapshot_terms.json")
    keywords = KeywordRegistryRepository(
        ROOT / "data/state/app.db"
    ).list_keywords()
    keywords_by_id = {
        str(keyword["keyword_id"]): keyword
        for keyword in keywords
    }
    active_ids = {
        keyword_id
        for keyword_id, keyword in keywords_by_id.items()
        if keyword.get("status") == "active"
    }
    window_end = max(
        parsed
        for parsed in (_parse_iso(row.get("captured_at")) for row in snapshots)
        if parsed
    )
    history_start = datetime.combine(
        window_end.date() - timedelta(days=HISTORY_LOOKBACK_DAYS - 1),
        datetime.min.time(),
    )

    reference_keywords = [
        {**keyword, "refresh_frequency_days": 1}
        for keyword in keywords
    ]
    reference_rows, _ = build_keyword_read_deltas(
        observations,
        reference_keywords,
        snapshots,
        ranking_hits,
        articles=articles,
        snapshot_terms=snapshot_terms,
        window_days=15,
    )
    reference = {
        str(row["keyword_id"]): row
        for row in reference_rows
    }
    stable_ids = {
        keyword_id
        for keyword_id, row in reference.items()
        if keyword_id in active_ids
        and row.get("status") == "ok"
        and int(row.get("observed_days") or 0) >= 13
    }

    cases = {
        "all_3d": lambda _keyword_id: 3,
        "all_7d": lambda _keyword_id: 7,
        "all_15d": lambda _keyword_id: 15,
        "current_mixed_policy": lambda keyword_id: int(
            keywords_by_id.get(keyword_id, {}).get("refresh_frequency_days") or 1
        ),
    }
    case_results: dict[str, Any] = {}
    for case_name, frequency_for in cases.items():
        (
            kept_observations,
            kept_snapshots,
            kept_hits,
            kept_terms,
        ) = _downsample(
            observations,
            snapshots,
            ranking_hits,
            snapshot_terms,
            history_start=history_start,
            window_end=window_end,
            frequency_for=frequency_for,
        )
        simulated_keywords = [
            {
                **keyword,
                "refresh_frequency_days": frequency_for(
                    str(keyword["keyword_id"])
                ),
            }
            for keyword in keywords
        ]
        simulated_rows, _ = build_keyword_read_deltas(
            kept_observations,
            simulated_keywords,
            kept_snapshots,
            kept_hits,
            articles=articles,
            snapshot_terms=kept_terms,
            window_days=15,
        )
        simulated = {
            str(row["keyword_id"]): row
            for row in simulated_rows
        }
        available_ids = [
            keyword_id
            for keyword_id in stable_ids
            if simulated.get(keyword_id, {}).get("status") == "ok"
        ]
        metrics = {
            field: _metric_accuracy(
                available_ids,
                reference,
                simulated,
                field,
                args.tolerance,
            )
            for field in ("steady_read_median", "read_delta_estimated")
        }
        passed = all(
            float(metric.get("within_tolerance_rate") or 0)
            >= args.minimum_pass_rate
            for metric in metrics.values()
        )
        case_results[case_name] = {
            "reference_sample_count": len(stable_ids),
            "available_count": len(available_ids),
            "availability_rate": round(
                len(available_ids) / len(stable_ids),
                4,
            )
            if stable_ids
            else 0.0,
            "metrics": metrics,
            "passed": passed,
        }

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method": "historical_daily_to_sparse_downsampling_v3",
        "reference_definition": "同一V3公式下，15日内至少13个观测日的active关键词",
        "accuracy_definition": (
            f"估算值位于高密度参考值的"
            f"{1 - args.tolerance:.0%}～{1 + args.tolerance:.0%}"
        ),
        "minimum_pass_rate": args.minimum_pass_rate,
        "window_end": window_end.isoformat(),
        "history_start": history_start.isoformat(),
        "reference_sample_count": len(stable_ids),
        "cases": case_results,
        "overall_passed": all(
            result["passed"]
            for result in case_results.values()
        ),
    }
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"output={output}")
    return 0 if payload["overall_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
