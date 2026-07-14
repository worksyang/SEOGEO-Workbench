"""关键词刷新频率的可复算策略。

只根据已抓到的快照、文章集合和排名变化计算频率；不输出业务解释。
人工锁定的刷新周期由 repository 保护，不会被这里覆盖。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import Config
from app.repositories.keyword_registry_repo import KeywordRegistryRepository


DEFAULT_POLICY: dict[str, Any] = {
    "daily_keyword_budget": 1600,
    "scheduled_keyword_budget": 1550,
    "discovery_daily_search_budget": 30,
    "discovery_probe_reserve": 10,
    "discovery_candidate_validation_reserve": 20,
    "manual_reserve_budget": 20,
    "discovery_batch_limit": 30,
    "max_keywords_per_batch": 250,
    "target_batch_runtime_minutes": 180,
    "minimum_adaptive_batch_size": 20,
    "failure_retry_hours": 12,
    "observation_refresh_interval_hours": 3,
    "minimum_observation_days": 15,
    "analysis_window_days": 30,
    "refresh_frequency_days": [1, 3, 7, 15],
    "turnover_thresholds": {
        "daily_min": 0.30,
        "three_day_min": 0.15,
        "seven_day_min": 0.05,
    },
    "rank_movement_weight": 0.60,
    "auto_archive": {
        "enabled": True,
        "minimum_observation_days": 15,
        "maximum_commercial_value_score": 3,
        "required_refresh_frequency_days": 15,
        "maximum_steady_read_median": 10,
    },
}


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def load_policy_config(project_root: Path | None = None) -> dict[str, Any]:
    root = Path(project_root or Config.PROJECT_ROOT)
    configured = _read_json(
        root / "data" / "config" / "keyword_refresh_policy.json",
        {},
    )
    policy = {
        **DEFAULT_POLICY,
        **(configured if isinstance(configured, dict) else {}),
    }
    thresholds = configured.get("turnover_thresholds", {}) if isinstance(configured, dict) else {}
    policy["turnover_thresholds"] = {
        **DEFAULT_POLICY["turnover_thresholds"],
        **(thresholds if isinstance(thresholds, dict) else {}),
    }
    archive = configured.get("auto_archive", {}) if isinstance(configured, dict) else {}
    policy["auto_archive"] = {
        **DEFAULT_POLICY["auto_archive"],
        **(archive if isinstance(archive, dict) else {}),
    }
    return policy


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _select_daily_snapshot_ids(snapshots: list[dict[str, Any]]) -> dict[str, list[str]]:
    """每个 keyword × 日期只选一个快照，优先 primary，否则选当天最新一份。"""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for snapshot in snapshots:
        keyword_id = str(snapshot.get("keyword_id") or "").strip()
        snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
        date = str(snapshot.get("snapshot_date") or "").strip()
        if not keyword_id or not snapshot_id or not date:
            continue
        if str(snapshot.get("status") or "success") != "success":
            continue
        grouped[(keyword_id, date)].append(snapshot)

    daily: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for (keyword_id, date), items in grouped.items():
        items.sort(
            key=lambda item: (
                1 if item.get("is_primary") else 0,
                str(item.get("captured_at") or ""),
            ),
            reverse=True,
        )
        daily[keyword_id].append((date, str(items[0]["snapshot_id"])))

    return {
        keyword_id: [snapshot_id for _, snapshot_id in sorted(values)]
        for keyword_id, values in daily.items()
    }


def build_refresh_metrics(
    *,
    normalized_dir: Path | None = None,
    analysis_window_days: int | None = None,
) -> dict[str, dict[str, Any]]:
    """计算每个关键词最近窗口内的文章换新率与排名移动率。"""
    normalized_dir = Path(normalized_dir or Config.NORMALIZED_DIR)
    policy = load_policy_config()
    window = int(analysis_window_days or policy["analysis_window_days"])
    snapshots = _read_json(normalized_dir / "snapshots.json", [])
    hits = _read_json(normalized_dir / "ranking_hits.json", [])
    if not isinstance(snapshots, list) or not isinstance(hits, list):
        return {}

    daily_snapshots = _select_daily_snapshot_ids(snapshots)
    snapshot_to_keyword: dict[str, str] = {}
    for keyword_id, snapshot_ids in daily_snapshots.items():
        for snapshot_id in snapshot_ids:
            snapshot_to_keyword[snapshot_id] = keyword_id

    articles: dict[str, set[str]] = defaultdict(set)
    ranks: dict[str, dict[str, int]] = defaultdict(dict)
    for hit in hits:
        snapshot_id = str(hit.get("snapshot_id") or "").strip()
        if snapshot_id not in snapshot_to_keyword:
            continue
        article_id = str(hit.get("article_id") or "").strip()
        if not article_id:
            continue
        articles[snapshot_id].add(article_id)
        try:
            rank = int(hit.get("rank"))
        except (TypeError, ValueError):
            continue
        prior = ranks[snapshot_id].get(article_id)
        ranks[snapshot_id][article_id] = rank if prior is None else min(prior, rank)

    metrics: dict[str, dict[str, Any]] = {}
    for keyword_id, all_snapshot_ids in daily_snapshots.items():
        snapshot_ids = all_snapshot_ids[-window:]
        turnover_samples: list[float] = []
        rank_samples: list[float] = []
        for previous, current in zip(snapshot_ids, snapshot_ids[1:]):
            previous_articles = articles.get(previous, set())
            current_articles = articles.get(current, set())
            if not previous_articles or not current_articles:
                continue
            overlap = previous_articles & current_articles
            turnover_samples.append(
                1 - (len(overlap) / max(len(previous_articles), len(current_articles)))
            )
            rank_changes: list[float] = []
            for article_id in overlap:
                previous_rank = ranks.get(previous, {}).get(article_id)
                current_rank = ranks.get(current, {}).get(article_id)
                if previous_rank is None or current_rank is None:
                    continue
                rank_changes.append(min(1.0, abs(previous_rank - current_rank) / 3))
            if rank_changes:
                rank_samples.append(sum(rank_changes) / len(rank_changes))

        metrics[keyword_id] = {
            "observation_days": len(snapshot_ids),
            "comparison_count": len(turnover_samples),
            "article_turnover_rate": round(
                sum(turnover_samples) / len(turnover_samples), 4
            )
            if turnover_samples
            else 0.0,
            "rank_movement_rate": round(sum(rank_samples) / len(rank_samples), 4)
            if rank_samples
            else 0.0,
            "last_snapshot_id": snapshot_ids[-1] if snapshot_ids else None,
        }
    return metrics


def recommend_frequency(
    metric: dict[str, Any] | None,
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把透明阈值转换为 1 / 3 / 7 / 15 天。"""
    policy = policy or load_policy_config()
    metric = metric or {}
    observed_days = int(metric.get("observation_days") or 0)
    minimum_days = int(policy["minimum_observation_days"])
    if observed_days < minimum_days:
        return {
            "refresh_frequency_days": 1,
            "refresh_policy_reason": (
                f"自动：有效观察 {observed_days}/{minimum_days} 天，观察期按3小时切片刷新"
            ),
        }

    turnover = float(metric.get("article_turnover_rate") or 0)
    rank_movement = float(metric.get("rank_movement_rate") or 0)
    signal = max(
        turnover,
        rank_movement * float(policy.get("rank_movement_weight") or 0.6),
    )
    thresholds = policy["turnover_thresholds"]
    if signal >= float(thresholds["daily_min"]):
        days = 1
    elif signal >= float(thresholds["three_day_min"]):
        days = 3
    elif signal >= float(thresholds["seven_day_min"]):
        days = 7
    else:
        days = 15

    return {
        "refresh_frequency_days": days,
        "refresh_policy_reason": (
            "自动：近30日有效观察"
            f"{observed_days}天，文章换新率{turnover:.1%}，"
            f"排名移动率{rank_movement:.1%}，信号{signal:.1%}，每{days}天刷新"
        ),
    }


def apply_auto_refresh_policies(
    *,
    repository: KeywordRegistryRepository | None = None,
    normalized_dir: Path | None = None,
) -> dict[str, Any]:
    """重新计算并保存自动策略，返回可供 API/日志展示的统计。"""
    repository = repository or KeywordRegistryRepository(Config.SQLITE_PATH)
    policy = load_policy_config()
    metrics = build_refresh_metrics(
        normalized_dir=normalized_dir,
        analysis_window_days=int(policy["analysis_window_days"]),
    )
    candidates = repository.list_keywords(include_archived=False)
    updates = {
        item["keyword_id"]: recommend_frequency(metrics.get(item["keyword_id"]), policy=policy)
        for item in candidates
        if item.get("refresh_frequency_source") != "manual"
    }
    updated = repository.apply_auto_refresh_policies(updates)
    distribution: dict[int, int] = defaultdict(int)
    for item in candidates:
        update = updates.get(item["keyword_id"])
        if update:
            distribution[int(update["refresh_frequency_days"])] += 1
        else:
            distribution[int(item["refresh_frequency_days"])] += 1
    return {
        "updated_count": updated,
        "total_auto_keywords": len(updates),
        "distribution": dict(sorted(distribution.items())),
        "metrics": metrics,
    }
