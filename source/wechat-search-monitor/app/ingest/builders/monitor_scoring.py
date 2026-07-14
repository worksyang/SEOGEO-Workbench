"""monitor_scoring.py — 监控派生层纯评分函数。

这些函数没有任何外部状态依赖（不读 ent、不读文件、不读 SQLite），
只接收基础数值并返回数值 / dict，是 `build_monitor_data` 派生层最稳的
基础原语。

每个函数都保持与原 `monitor_builder.py` 中的实现完全一致，
只是换了归属模块。任何字段语义、阈值、公式改动都不在本模块进行。
"""
from __future__ import annotations

import math


WINDOW_DAYS = 15
RECENT_WINDOW_DAYS = 7
TIMELINESS_WINDOW_DAYS = 3


def rank_weight(r: int) -> float:
    """账号分主权重：名次越靠前，权重越高。

    旧版曾把第 4 名权重设得高于第 1 名，用来偏向“稳定搜索位”；
    百分制账号分里改回用户直觉：第 1 名必须比第 4 名更值钱。
    """
    if r <= 0:
        return 0.0
    weights = {
        1: 10.0,
        2: 8.2,
        3: 6.8,
        4: 5.6,
        5: 4.6,
        6: 3.7,
        7: 3.0,
        8: 2.4,
        9: 1.9,
        10: 1.5,
    }
    return weights.get(r, 0.0)


def timeliness_rank_weight(r: int) -> float:
    """时效榜专用：强看 Top3，Top4/5 给少量冲榜信号。"""
    if r <= 0:
        return 0.0
    weights = {
        1: 10.0,
        2: 7.5,
        3: 5.5,
        4: 3.0,
        5: 1.8,
    }
    return weights.get(r, 0.0)


def saturated_percent_score(evidence: float) -> int:
    """把任意非负证据强度压到 0-100 整数。

    采用 `100 * (1 - exp(-x))`：证据越强越接近 100，但数学上不会超过 100。
    返回整数用于前端展示，避免 820 这类累计积分误读。
    """
    x = max(float(evidence or 0), 0.0)
    return max(0, min(100, int(round(100 * (1 - math.exp(-x))))))


def angle_bonus(distinct_buckets: int) -> float:
    extra_buckets = max(distinct_buckets - 1, 0)
    return round(min(0.3, 0.15 * extra_buckets), 2)


def extra_article_bonus(distinct_articles: int) -> float:
    steps = (0.6, 0.3, 0.15, 0.1)
    total = 0.0
    extra_articles = max(distinct_articles - 1, 0)
    for idx in range(extra_articles):
        if idx >= len(steps):
            break
        total += steps[idx]
    return round(total, 2)


def recency_weight(window_index: int) -> float:
    age = WINDOW_DAYS - 1 - window_index
    if age <= 2:
        return 1.0
    if age <= 6:
        return 0.82
    if age <= 10:
        return 0.6
    return 0.38


def continuity_multiplier(recent_hit_days: int, current_streak: int) -> float:
    if recent_hit_days <= 0:
        return 1.0
    return round(
        1 + 0.08 * min(max(recent_hit_days - 1, 0), 4) + 0.06 * min(max(current_streak - 1, 0), 4),
        2,
    )


def topic_breadth_bonus(topic_count: int) -> float:
    total = 0.0
    for idx in range(2, max(topic_count, 0) + 1):
        if idx <= 5:
            total += 1.0
        elif idx <= 10:
            total += 0.6
        else:
            total += 0.3
    return round(total, 2)


def bucket_breadth_bonus(bucket_count: int) -> float:
    total = 0.0
    for idx in range(2, max(bucket_count, 0) + 1):
        if idx <= 4:
            total += 0.4
        elif idx <= 8:
            total += 0.2
        else:
            total += 0.1
    return round(total, 2)


def longest_positive_streak(values: list[float]) -> int:
    best = 0
    cur = 0
    for value in values:
        if value > 0:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


def trailing_positive_streak(values: list[float]) -> int:
    cur = 0
    for value in reversed(values):
        if value > 0:
            cur += 1
            continue
        break
    return cur


def display_day_count(history: list[int | float], is_currently_visible: bool = False) -> int:
    days = sum(1 for value in history if value and value > 0)
    if days == 0 and is_currently_visible:
        return 1
    return days


def summarize_account_keyword_moves(keyword_breakdown: dict[str, dict]) -> dict:
    """汇总账号在多关键词下的新命中/上/下/平变化情况，供前端徽章使用。"""
    counts = {"new": 0, "up": 0, "down": 0, "flat": 0}
    for info in keyword_breakdown.values():
        today = info.get("today_rank")
        prev = info.get("today_prev")
        if today is None:
            counts["flat"] += 1
            continue
        if prev is None:
            counts["new"] += 1
            continue
        if prev > today:
            counts["up"] += 1
        elif prev < today:
            counts["down"] += 1
        else:
            counts["flat"] += 1

    total = sum(counts.values())

    if counts["new"] > 0:
        primary_type = "new"
        primary_count = counts["new"]
        if counts["up"] > counts["down"]:
            secondary_type = "up"
            secondary_count = counts["up"]
        elif counts["down"] > 0:
            secondary_type = "down"
            secondary_count = counts["down"]
        else:
            secondary_type = None
            secondary_count = 0
    else:
        primary_count = max(counts["up"], counts["down"], counts["flat"])
        if counts["up"] > counts["down"]:
            primary_type = "up"
            primary_count = counts["up"]
        elif counts["down"] > 0:
            primary_type = "down"
            primary_count = counts["down"]
        else:
            primary_type = "flat"
            primary_count = counts["flat"]
        secondary_type = None
        secondary_count = 0

    return {
        "new_count": counts["new"],
        "up_count": counts["up"],
        "down_count": counts["down"],
        "flat_count": counts["flat"],
        "total_keywords": total,
        "primary_type": primary_type,
        "primary_count": primary_count,
        "secondary_type": secondary_type,
        "secondary_count": secondary_count,
    }


__all__ = [
    "WINDOW_DAYS",
    "RECENT_WINDOW_DAYS",
    "TIMELINESS_WINDOW_DAYS",
    "rank_weight",
    "timeliness_rank_weight",
    "saturated_percent_score",
    "angle_bonus",
    "extra_article_bonus",
    "recency_weight",
    "continuity_multiplier",
    "topic_breadth_bonus",
    "bucket_breadth_bonus",
    "longest_positive_streak",
    "trailing_positive_streak",
    "display_day_count",
    "summarize_account_keyword_moves",
]
