"""monitor_accounts.py — 账号维度聚合与汇总。

把原 `build_monitor_data()` 中的账号循环（acct_day_theme_buckets 构建 +
每个账号 score/timeliness_score/topics/keywords/move_summary 计算）抽离
为独立函数。

输入：`MonitorBuildContext` + `KeywordSummaryResult`。
输出：账号 summaries 列表（已按 score desc 排序）。

行为契约：只消费事实层数据；三榜输出整数，100 为 P99 基准线，多维超标时允许突破。
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from app.ingest.builders.monitor_context import MonitorBuildContext
from app.ingest.builders.monitor_keywords import KeywordSummaryResult
from app.ingest.builders.monitor_scoring import (
    RECENT_WINDOW_DAYS,
    TIMELINESS_WINDOW_DAYS,
    WINDOW_DAYS,
    angle_bonus,
    extra_article_bonus,
    longest_positive_streak,
    rank_weight,
    recency_weight,
    summarize_account_keyword_moves,
    trailing_positive_streak,
)


SCORE_BENCHMARK_PERCENTILE = 0.99
SCORE_OVERFLOW_LOG_SCALE = 40.0
CLASSIC_MIN_DAYS = 3
FRESH_ARTICLE_DAYS = 21


def _percentile(values: list[float], q: float) -> float:
    """小样本安全分位数。"""
    nums = sorted(v for v in values if v > 0)
    if not nums:
        return 1.0
    if len(nums) == 1:
        return nums[0]
    pos = (len(nums) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return nums[lo]
    return nums[lo] * (hi - pos) + nums[hi] * (pos - lo)


BOARD_CONFIGS = {
    "account": {
        "label": "账号分",
        "window_label": "滚动15天",
        "axes_meta": [
            {"key": "history_coverage", "label": "历史覆盖", "desc": "滚动15天不同关键词、文章与主题覆盖"},
            {"key": "recent_coverage", "label": "近期覆盖", "desc": "最近7天仍然有效的关键词与文章覆盖"},
            {"key": "classic_articles", "label": "经典文章", "desc": "同文同词位于第4–10名至少3个不同日期"},
            {"key": "continuity", "label": "持续经营", "desc": "近7天在榜、15天在榜与当前连续命中"},
            {"key": "content_matrix", "label": "内容矩阵", "desc": "多篇文章共同贡献，降低单篇文章集中度"},
            {"key": "battle_breadth", "label": "战场广度", "desc": "不同产品 topic 与搜索意图类别覆盖"},
        ],
        "weights": {
            "history_coverage": 0.15,
            "recent_coverage": 0.15,
            "classic_articles": 0.30,
            "continuity": 0.20,
            "content_matrix": 0.10,
            "battle_breadth": 0.10,
        },
        "required_breakthrough_axes": {"classic_articles"},
        "confidence_days": 5,
    },
    "timeliness": {
        "label": "时效分",
        "window_label": "最近3天",
        "axes_meta": [
            {"key": "top3_volume", "label": "Top3规模", "desc": "最近3天进入前三的文章×关键词命中规模"},
            {"key": "top3_breadth", "label": "Top3广度", "desc": "前三覆盖的关键词、产品与搜索意图类别"},
            {"key": "new_top3", "label": "新进Top3", "desc": "最近3天进入前三、此前7天未进前三的文章×关键词"},
            {"key": "fresh_top3", "label": "新文冲榜", "desc": "发布21天内文章进入前三的数量"},
            {"key": "top3_continuity", "label": "连续冲榜", "desc": "最近3天中实际出现Top3命中的天数"},
            {"key": "upward_momentum", "label": "上升动能", "desc": "今天相对昨天的新进关键词与排名上升"},
        ],
        "weights": {
            "top3_volume": 0.28,
            "top3_breadth": 0.20,
            "new_top3": 0.22,
            "fresh_top3": 0.12,
            "top3_continuity": 0.10,
            "upward_momentum": 0.08,
        },
        "required_breakthrough_axes": {"top3_volume", "new_top3"},
        "confidence_days": 3,
    },
    "today": {
        "label": "当天分",
        "window_label": "今天",
        "axes_meta": [
            {"key": "today_top3", "label": "今日Top3", "desc": "今天进入前三的文章×关键词命中数量"},
            {"key": "today_keywords", "label": "今日关键词", "desc": "今天覆盖的不同监控关键词数量"},
            {"key": "today_articles", "label": "今日文章", "desc": "今天仍在榜的不同文章数量"},
            {"key": "today_themes", "label": "今日主题", "desc": "今天覆盖的产品 topic 与搜索意图类别"},
            {"key": "today_rank_quality", "label": "排名质量", "desc": "今天全部命中的平均排名质量，第一名权重最高"},
            {"key": "today_growth", "label": "今日增长", "desc": "今天相对昨天的新进关键词与排名上升"},
        ],
        "weights": {
            "today_top3": 0.30,
            "today_keywords": 0.25,
            "today_articles": 0.18,
            "today_themes": 0.10,
            "today_rank_quality": 0.10,
            "today_growth": 0.07,
        },
        "required_breakthrough_axes": {"today_top3", "today_keywords"},
        "confidence_days": 1,
    },
}


def _log_count(value: int | float) -> float:
    return math.log1p(max(float(value or 0), 0.0))


def _confidence_by_days(days: int, full_days: int) -> float:
    """观察天数置信度：账号分满 5 天公平，时效分满 3 天公平。"""
    if days <= 0:
        return 0.0
    if days >= full_days:
        return 1.0
    table_5 = {1: 0.38, 2: 0.55, 3: 0.74, 4: 0.88}
    table_3 = {1: 0.68, 2: 0.88}
    return (table_3 if full_days <= 3 else table_5).get(days, min(days / full_days, 1.0))


def _observation_span_days(events: list[dict[str, Any]], end_idx: int, max_days: int) -> int:
    """从账号首次进入监控结果起计算可观察跨度；未上榜日也是有效事实，不重复惩罚。"""
    prior_days = [event["day_idx"] for event in events if event["day_idx"] <= end_idx]
    if not prior_days:
        return 0
    return min(max(end_idx - min(prior_days) + 1, 0), max_days)


def _events_in_window(events: list[dict[str, Any]], start_idx: int, end_idx: int) -> list[dict[str, Any]]:
    return [event for event in events if start_idx <= event["day_idx"] <= end_idx]


def _event_sets(events: list[dict[str, Any]]) -> tuple[set[str], set[str], set[str], set[str]]:
    return (
        {event["keyword"] for event in events},
        {event["article_key"] for event in events},
        {event["topic"] for event in events},
        {event["bucket"] for event in events},
    )


def _trailing_event_streak(events: list[dict[str, Any]], end_idx: int, max_days: int) -> int:
    active_days = {event["day_idx"] for event in events}
    streak = 0
    for day_idx in range(end_idx, end_idx - max_days, -1):
        if day_idx not in active_days:
            break
        streak += 1
    return streak


def _effective_article_count(events: list[dict[str, Any]]) -> tuple[float, float]:
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        counts[event["article_key"]] += 1
    total = sum(counts.values())
    if total <= 0:
        return 0.0, 1.0
    probabilities = [count / total for count in counts.values()]
    effective = math.exp(-sum(probability * math.log(probability) for probability in probabilities))
    concentration = max(counts.values()) / total
    return round(effective, 4), round(concentration, 4)


def _coverage_raw(events: list[dict[str, Any]]) -> float:
    keywords, articles, topics, buckets = _event_sets(events)
    return round(
        0.55 * _log_count(len(keywords))
        + 0.30 * _log_count(len(articles))
        + 0.15 * _log_count(len(topics) + len(buckets)),
        6,
    )


def _best_ranks_by_keyword(events: list[dict[str, Any]], day_idx: int) -> dict[str, int]:
    best: dict[str, int] = {}
    for event in events:
        if event["day_idx"] != day_idx:
            continue
        keyword = event["keyword"]
        rank = event["rank"]
        if keyword not in best or rank < best[keyword]:
            best[keyword] = rank
    return best


def _move_counts(events: list[dict[str, Any]], end_idx: int) -> dict[str, int]:
    current = _best_ranks_by_keyword(events, end_idx)
    previous = _best_ranks_by_keyword(events, end_idx - 1)
    result = {"new_count": 0, "up_count": 0, "down_count": 0, "flat_count": 0}
    for keyword, rank in current.items():
        previous_rank = previous.get(keyword)
        if previous_rank is None:
            result["new_count"] += 1
        elif rank < previous_rank:
            result["up_count"] += 1
        elif rank > previous_rank:
            result["down_count"] += 1
        else:
            result["flat_count"] += 1
    return result


def _parse_article_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _build_account_raw_snapshot(events: list[dict[str, Any]], end_idx: int) -> dict[str, Any]:
    history = _events_in_window(events, end_idx - WINDOW_DAYS + 1, end_idx)
    recent = _events_in_window(events, end_idx - RECENT_WINDOW_DAYS + 1, end_idx)
    history_keywords, history_articles, history_topics, history_buckets = _event_sets(history)
    recent_keywords, recent_articles, recent_topics, recent_buckets = _event_sets(recent)

    classic_pair_days: dict[tuple[str, str], set[int]] = defaultdict(set)
    for event in history:
        if 4 <= event["rank"] <= 10:
            classic_pair_days[(event["keyword"], event["article_key"])].add(event["day_idx"])
    classic_pairs = {
        pair: days
        for pair, days in classic_pair_days.items()
        if len(days) >= CLASSIC_MIN_DAYS
    }
    classic_articles = {pair[1] for pair in classic_pairs}
    classic_keywords = {pair[0] for pair in classic_pairs}
    classic_days = sum(len(days) for days in classic_pairs.values())

    history_days = {event["day_idx"] for event in history}
    recent_days = {event["day_idx"] for event in recent}
    current_streak = _trailing_event_streak(history, end_idx, WINDOW_DAYS)
    effective_articles, concentration = _effective_article_count(history)
    content_matrix = _log_count(effective_articles) * (1 - 0.35 * concentration)
    continuity = (
        0.50 * len(recent_days) / RECENT_WINDOW_DAYS
        + 0.30 * len(history_days) / WINDOW_DAYS
        + 0.20 * current_streak / WINDOW_DAYS
    )
    classic_raw = (
        0.35 * _log_count(len(classic_pairs))
        + 0.25 * _log_count(len(classic_articles))
        + 0.15 * _log_count(len(classic_keywords))
        + 0.25 * _log_count(classic_days)
    )
    breadth_raw = (
        0.50 * _log_count(len(history_keywords))
        + 0.30 * _log_count(len(history_topics))
        + 0.20 * _log_count(len(history_buckets))
    )
    observation_span_days = _observation_span_days(events, end_idx, WINDOW_DAYS)
    confidence = _confidence_by_days(observation_span_days, 5)

    return {
        "end_idx": end_idx,
        "raw_axes": {
            "history_coverage": _coverage_raw(history),
            "recent_coverage": _coverage_raw(recent),
            "classic_articles": round(classic_raw, 6),
            "continuity": round(continuity, 6),
            "content_matrix": round(content_matrix, 6),
            "battle_breadth": round(breadth_raw, 6),
        },
        "confidence": round(confidence, 4),
        "details": {
            "history_keyword_count": len(history_keywords),
            "history_article_count": len(history_articles),
            "history_topic_count": len(history_topics),
            "history_bucket_count": len(history_buckets),
            "recent_keyword_count": len(recent_keywords),
            "recent_article_count": len(recent_articles),
            "recent_topic_count": len(recent_topics),
            "recent_bucket_count": len(recent_buckets),
            "classic_pair_count": len(classic_pairs),
            "classic_article_count": len(classic_articles),
            "classic_keyword_count": len(classic_keywords),
            "classic_rank_days": classic_days,
            "history_active_days": len(history_days),
            "recent_active_days": len(recent_days),
            "observation_span_days": observation_span_days,
            "current_streak": current_streak,
            "effective_article_count": effective_articles,
            "article_concentration": concentration,
        },
    }


def _build_timeliness_raw_snapshot(
    events: list[dict[str, Any]],
    end_idx: int,
    end_date: date,
) -> dict[str, Any]:
    recent = _events_in_window(events, end_idx - TIMELINESS_WINDOW_DAYS + 1, end_idx)
    top3 = [event for event in recent if event["rank"] <= 3]
    top3_keywords, top3_articles, top3_topics, top3_buckets = _event_sets(top3)
    prior = _events_in_window(events, end_idx - TIMELINESS_WINDOW_DAYS - 6, end_idx - TIMELINESS_WINDOW_DAYS)
    prior_top3_pairs = {
        (event["keyword"], event["article_key"])
        for event in prior
        if event["rank"] <= 3
    }
    top3_pairs = {(event["keyword"], event["article_key"]) for event in top3}
    new_top3_pairs = top3_pairs - prior_top3_pairs
    fresh_articles = {
        event["article_key"]
        for event in top3
        if (
            (published_date := _parse_article_date(event.get("published_at")))
            and 0 <= (end_date - published_date).days <= FRESH_ARTICLE_DAYS
        )
    }
    top3_days = {event["day_idx"] for event in top3}
    observed_span_days = _observation_span_days(events, end_idx, TIMELINESS_WINDOW_DAYS)
    moves = _move_counts(events, end_idx)
    upward_raw = _log_count(moves["new_count"] + 0.6 * moves["up_count"])

    return {
        "end_idx": end_idx,
        "raw_axes": {
            "top3_volume": _log_count(len(top3)),
            "top3_breadth": round(
                0.55 * _log_count(len(top3_keywords))
                + 0.25 * _log_count(len(top3_topics))
                + 0.20 * _log_count(len(top3_buckets)),
                6,
            ),
            "new_top3": _log_count(len(new_top3_pairs)),
            "fresh_top3": _log_count(len(fresh_articles)),
            "top3_continuity": round(len(top3_days) / TIMELINESS_WINDOW_DAYS, 6),
            "upward_momentum": round(upward_raw, 6),
        },
        "confidence": round(_confidence_by_days(observed_span_days, 3), 4),
        "details": {
            "top3_hit_count": len(top3),
            "top3_keyword_count": len(top3_keywords),
            "top3_article_count": len(top3_articles),
            "top3_topic_count": len(top3_topics),
            "top3_bucket_count": len(top3_buckets),
            "new_top3_pair_count": len(new_top3_pairs),
            "fresh_top3_article_count": len(fresh_articles),
            "top3_active_days": len(top3_days),
            "observed_days": observed_span_days,
            **moves,
        },
    }


def _build_today_raw_snapshot(events: list[dict[str, Any]], end_idx: int) -> dict[str, Any]:
    today = _events_in_window(events, end_idx, end_idx)
    top3 = [event for event in today if event["rank"] <= 3]
    keywords, articles, topics, buckets = _event_sets(today)
    moves = _move_counts(events, end_idx)
    rank_quality = (
        sum((11 - event["rank"]) / 10 for event in today) / len(today)
        if today
        else 0.0
    )
    growth_raw = _log_count(moves["new_count"] + 0.6 * moves["up_count"])

    return {
        "end_idx": end_idx,
        "raw_axes": {
            "today_top3": _log_count(len(top3)),
            "today_keywords": _log_count(len(keywords)),
            "today_articles": _log_count(len(articles)),
            "today_themes": _log_count(len(topics) + 0.5 * len(buckets)),
            "today_rank_quality": round(rank_quality, 6),
            "today_growth": round(growth_raw, 6),
        },
        "confidence": 1.0,
        "details": {
            "today_top3_count": len(top3),
            "today_keyword_count": len(keywords),
            "today_article_count": len(articles),
            "today_topic_count": len(topics),
            "today_bucket_count": len(buckets),
            "today_hit_count": len(today),
            "average_rank_quality": round(rank_quality, 4),
            **moves,
        },
    }


def _build_raw_board_snapshots(
    events: list[dict[str, Any]],
    end_idx: int,
    window_start: date,
) -> dict[str, dict[str, Any]]:
    end_date = window_start + timedelta(days=end_idx)
    return {
        "account": _build_account_raw_snapshot(events, end_idx),
        "timeliness": _build_timeliness_raw_snapshot(events, end_idx, end_date),
        "today": _build_today_raw_snapshot(events, end_idx),
    }


def _build_board_benchmarks(current_snapshots: list[dict[str, dict[str, Any]]]) -> dict[str, dict[str, float]]:
    benchmarks: dict[str, dict[str, float]] = {}
    for board, config in BOARD_CONFIGS.items():
        benchmarks[board] = {}
        for meta in config["axes_meta"]:
            key = meta["key"]
            values = [snapshot[board]["raw_axes"].get(key, 0.0) for snapshot in current_snapshots]
            benchmarks[board][key] = round(_percentile(values, SCORE_BENCHMARK_PERCENTILE), 6)
    return benchmarks


def _axis_score(raw_value: float, benchmark: float) -> float:
    raw = max(float(raw_value or 0.0), 0.0)
    base = max(float(benchmark or 0.0), 1e-9)
    if raw <= 0:
        return 0.0
    if raw <= base:
        return round(100 * raw / base, 4)
    return round(100 + SCORE_OVERFLOW_LOG_SCALE * math.log2(raw / base), 4)


def _score_board_snapshot(
    raw_snapshot: dict[str, Any],
    board: str,
    benchmarks: dict[str, float],
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = BOARD_CONFIGS[board]
    weights = config["weights"]
    axis_values = {
        key: _axis_score(raw_snapshot["raw_axes"].get(key, 0.0), benchmarks.get(key, 1.0))
        for key in weights
    }
    confidence = float(raw_snapshot.get("confidence") or 0.0)
    base_score = sum(min(axis_values[key], 100.0) * weight for key, weight in weights.items()) * confidence
    breakthrough_energy = sum(max(axis_values[key] - 100.0, 0.0) * weight for key, weight in weights.items()) * confidence
    over_axes = [key for key, value in axis_values.items() if value > 100.0001]
    required_axes = config["required_breakthrough_axes"]
    breakthrough_gate = (
        confidence >= 0.999
        and base_score >= 85.0
        and len(over_axes) >= 2
        and any(key in required_axes for key in over_axes)
    )
    score_raw = base_score + breakthrough_energy if breakthrough_gate else min(base_score, 100.0)
    score = max(0, int(round(score_raw)))
    normalized = {
        **raw_snapshot,
        "axes": {key: max(0, int(round(value))) for key, value in axis_values.items()},
        "axis_values": {key: round(value, 4) for key, value in axis_values.items()},
    }
    parts = {
        "score_raw": round(score_raw, 4),
        "base_score": round(base_score, 4),
        "breakthrough_energy": round(max(score_raw - 100.0, 0.0), 4),
        "confidence": round(confidence, 4),
        "breakthrough": score > 100,
        "breakthrough_gate": breakthrough_gate,
        "over_axes": over_axes,
    }
    return normalized, {"score": score, **parts}


def _build_hexagon_payload(
    board: str,
    current: dict[str, Any],
    previous: dict[str, Any],
    benchmarks: dict[str, float],
) -> dict[str, Any]:
    config = BOARD_CONFIGS[board]
    return {
        "board": board,
        "label": config["label"],
        "window_label": config["window_label"],
        "benchmark_line": 100,
        "benchmark_percentile": SCORE_BENCHMARK_PERCENTILE,
        "overflow_log_scale": SCORE_OVERFLOW_LOG_SCALE,
        "axes_meta": config["axes_meta"],
        "weights": config["weights"],
        "benchmarks": benchmarks,
        "current": current,
        "previous": previous,
        "delta": {
            key: int(current["axes"].get(key, 0) - previous["axes"].get(key, 0))
            for key in current["axes"]
        },
    }


def _score_level(score: int) -> str:
    if score >= 130:
        return "extreme_breakthrough"
    if score >= 110:
        return "strong_breakthrough"
    if score > 100:
        return "breakthrough"
    if score == 100:
        return "benchmark"
    return "within_benchmark"


def _population_stat(values: list[float], value: float) -> dict[str, int | float]:
    """把单个值翻译成全站名次、并列数和严格超过比例。"""
    total = len(values)
    if total <= 0:
        return {"rank": 0, "total": 0, "tie_count": 0, "percentile": 0.0}
    epsilon = 1e-6
    better = sum(candidate > value + epsilon for candidate in values)
    lower = sum(candidate < value - epsilon for candidate in values)
    tie_count = total - better - lower
    return {
        "rank": better + 1,
        "total": total,
        "tie_count": tie_count,
        "percentile": round(100 * lower / total, 1),
    }


def _attach_population_context(acct_summaries: list[dict[str, Any]]) -> None:
    """给三套六边形补充“全站第几/超过多少账号”，不改变原评分。"""
    board_fields = {
        "account": ("account_score_hexagon", "score", "score_yesterday"),
        "timeliness": ("timeliness_score_hexagon", "timeliness_score", "timeliness_score_yesterday"),
        "today": ("today_score_hexagon", "today_score", "today_score_yesterday"),
    }
    for board, (hexagon_field, score_field, previous_score_field) in board_fields.items():
        if not acct_summaries:
            continue
        current_score_values = [float(account.get(score_field) or 0.0) for account in acct_summaries]
        previous_score_values = [float(account.get(previous_score_field) or 0.0) for account in acct_summaries]
        axis_keys = [meta["key"] for meta in BOARD_CONFIGS[board]["axes_meta"]]
        current_axis_values = {
            key: [
                float(account[hexagon_field]["current"]["axes"].get(key) or 0.0)
                for account in acct_summaries
            ]
            for key in axis_keys
        }
        previous_axis_values = {
            key: [
                float(account[hexagon_field]["previous"]["axes"].get(key) or 0.0)
                for account in acct_summaries
            ]
            for key in axis_keys
        }

        for account in acct_summaries:
            hexagon = account[hexagon_field]
            hexagon["population"] = {
                "account_count": len(acct_summaries),
                "score": _population_stat(
                    current_score_values,
                    float(account.get(score_field) or 0.0),
                ),
                "axes": {
                    key: _population_stat(
                        current_axis_values[key],
                        float(hexagon["current"]["axes"].get(key) or 0.0),
                    )
                    for key in axis_keys
                },
            }
            hexagon["previous_population"] = {
                "account_count": len(acct_summaries),
                "score": _population_stat(
                    previous_score_values,
                    float(account.get(previous_score_field) or 0.0),
                ),
                "axes": {
                    key: _population_stat(
                        previous_axis_values[key],
                        float(hexagon["previous"]["axes"].get(key) or 0.0),
                    )
                    for key in axis_keys
                },
            }


def build_account_theme_indexes(
    ctx: MonitorBuildContext,
) -> dict[str, dict[str, Any]]:
    """构建账号 × 主题（topic/bucket）层级的中间索引。

    返回 dict，键名与原 builder 内闭包变量名一致：
      - acct_day_theme_buckets: {account_id: {day_idx: {theme_key: {label, theme_type, best_rank, articles, keywords, buckets}}}}
      - acct_theme_keyword_set: {account_id: {theme_key: {kw_text}}}
      - acct_theme_articles: {account_id: {theme_key: {article_key: {...}}}}
      - acct_theme_bucket_set: {account_id: {theme_key: {bucket_name}}}
      - acct_theme_meta: {account_id: {theme_key: {label, theme_type}}}
      - acct_day_keyword_articles: {account_id: {day_idx: {keyword: {article_key: {...}}}}}
    """
    kw_by_id = ctx.kw_by_id
    kw_topic_by_id = ctx.kw_topic_by_id
    kw_bucket_by_id = ctx.kw_bucket_by_id
    art_by_id = ctx.art_by_id
    hits_by_snap = ctx.hits_by_snap
    window_dates = ctx.window_dates

    acct_day_theme_buckets: dict[str, dict[int, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    acct_theme_keyword_set: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    acct_theme_articles: dict[str, dict[str, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    acct_theme_bucket_set: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    acct_theme_meta: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    acct_day_keyword_articles: dict[str, dict[int, dict[str, dict[str, dict]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(dict))
    )

    score_dates = [window_dates[0] - timedelta(days=1), *window_dates]
    for score_idx, day in enumerate(score_dates):
        idx = score_idx - 1
        for kid, kw in kw_by_id.items():
            ps = ctx.primary_snap_for(kid, day)
            if not ps:
                continue
            topic = kw_topic_by_id[kid]
            bucket_name = kw_bucket_by_id[kid]
            kw_text = kw["keyword_text"]
            is_product_topic = bool(topic and topic != kw_text)
            theme_key = f"topic::{topic}" if is_product_topic else f"bucket::{bucket_name}"
            theme_label = topic if is_product_topic else bucket_name
            theme_type = "topic" if is_product_topic else "bucket"
            for h in hits_by_snap.get(ps["snapshot_id"], []):
                aid = h["account_id"]
                art = art_by_id.get(h["article_id"], {})
                article_key = art.get("article_id") or h["title_raw"]
                keyword_article = acct_day_keyword_articles[aid][idx][kw_text].setdefault(article_key, {
                    "article_key": article_key,
                    "article_id": art.get("article_id"),
                    "rank": h["rank"],
                    "topic": topic,
                    "bucket": bucket_name,
                    "published_at": art.get("published_at"),
                })
                if h["rank"] < keyword_article["rank"]:
                    keyword_article["rank"] = h["rank"]

                if idx < 0:
                    continue

                day_bucket = acct_day_theme_buckets[aid][idx].setdefault(theme_key, {
                    "label": theme_label,
                    "theme_type": theme_type,
                    "best_rank": 0,
                    "articles": {},
                    "keywords": set(),
                    "buckets": set(),
                })
                if day_bucket["best_rank"] == 0 or h["rank"] < day_bucket["best_rank"]:
                    day_bucket["best_rank"] = h["rank"]
                day_bucket["keywords"].add(kw_text)
                day_bucket["buckets"].add(bucket_name)

                article_bucket = day_bucket["articles"].setdefault(article_key, {
                    "best_rank": 0,
                    "keywords": set(),
                    "buckets": set(),
                })
                if article_bucket["best_rank"] == 0 or h["rank"] < article_bucket["best_rank"]:
                    article_bucket["best_rank"] = h["rank"]
                article_bucket["keywords"].add(kw_text)
                article_bucket["buckets"].add(bucket_name)

                acct_theme_meta[aid][theme_key] = {"label": theme_label, "theme_type": theme_type}
                acct_theme_keyword_set[aid][theme_key].add(kw_text)
                acct_theme_bucket_set[aid][theme_key].add(bucket_name)
                topic_article = acct_theme_articles[aid][theme_key].get(article_key)
                pub = art.get("published_at")
                pub_short = pub[2:10].replace("-", "/") if pub else ""
                if not topic_article:
                    acct_theme_articles[aid][theme_key][article_key] = {
                        "article_id": art.get("article_id"),
                        "title": art.get("title", h["title_raw"]),
                        "url": art.get("raw_url", h["url_raw"]),
                        "cover_url": art.get("cover_url"),
                        "published_at": pub_short,
                        "rank": h["rank"],
                        "content_path": art.get("content_file_path"),
                        "read_count": art.get("read_count"),
                        "like_count": art.get("like_count"),
                        "friends_follow_count": art.get("friends_follow_count"),
                        "original_article_count": art.get("original_article_count"),
                    }
                elif h["rank"] < topic_article["rank"]:
                    topic_article["rank"] = h["rank"]

    return {
        "acct_day_theme_buckets": acct_day_theme_buckets,
        "acct_theme_keyword_set": acct_theme_keyword_set,
        "acct_theme_articles": acct_theme_articles,
        "acct_theme_bucket_set": acct_theme_bucket_set,
        "acct_theme_meta": acct_theme_meta,
        "acct_day_keyword_articles": acct_day_keyword_articles,
    }


def _flatten_rank_events(day_keyword_articles: dict[int, dict[str, dict[str, dict]]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for day_idx, keyword_map in day_keyword_articles.items():
        for keyword, article_map in keyword_map.items():
            for article in article_map.values():
                events.append({
                    "day_idx": day_idx,
                    "keyword": keyword,
                    **article,
                })
    return events


def build_account_summaries(
    ctx: MonitorBuildContext,
    kw_result: KeywordSummaryResult,
    theme_indexes: dict[str, Any],
) -> list[dict]:
    """构造账号 summaries 列表（已按 score desc 排序）。"""
    acct_by_id = ctx.acct_by_id
    acct_kw_history = kw_result.acct_kw_history
    acct_articles = kw_result.acct_articles
    kw_latest_ranks_map = kw_result.kw_latest_ranks_map
    kw_prev_ranks_map = kw_result.kw_prev_ranks_map
    today_titles_by_acct = kw_result.today_titles_by_acct

    acct_day_theme_buckets = theme_indexes["acct_day_theme_buckets"]
    acct_theme_keyword_set = theme_indexes["acct_theme_keyword_set"]
    acct_theme_articles = theme_indexes["acct_theme_articles"]
    acct_theme_bucket_set = theme_indexes["acct_theme_bucket_set"]
    acct_theme_meta = theme_indexes["acct_theme_meta"]
    acct_day_keyword_articles = theme_indexes["acct_day_keyword_articles"]

    account_calcs: list[dict[str, Any]] = []

    # 第一遍：保留页面已有的历史/文章/topic 明细，同时准备三榜评分需要的逐日排名事件。
    for aid, day_theme_map in acct_day_theme_buckets.items():
        day_scores = [0.0] * WINDOW_DAYS
        raw_day_scores = [0.0] * WINDOW_DAYS
        history = [0] * WINDOW_DAYS
        theme_histories: dict[str, list[int]] = defaultdict(lambda: [0] * WINDOW_DAYS)
        theme_day_scores: dict[str, list[float]] = defaultdict(lambda: [0.0] * WINDOW_DAYS)

        for idx, themes_for_day in day_theme_map.items():
            best_rank_of_day = 0
            total_score_of_day = 0.0
            for theme_key, bucket in themes_for_day.items():
                articles = sorted(bucket["articles"].values(), key=lambda item: item["best_rank"])
                primary_article = articles[0]
                distinct_articles = len(articles)
                score = (
                    rank_weight(primary_article["best_rank"])
                    + angle_bonus(len(primary_article["buckets"]))
                    + extra_article_bonus(distinct_articles)
                )
                total_score_of_day += score
                theme_histories[theme_key][idx] = bucket["best_rank"]
                theme_day_scores[theme_key][idx] = round(score * recency_weight(idx), 2)
                if best_rank_of_day == 0 or bucket["best_rank"] < best_rank_of_day:
                    best_rank_of_day = bucket["best_rank"]
            raw_day_scores[idx] = round(total_score_of_day, 2)
            day_scores[idx] = round(total_score_of_day * recency_weight(idx), 2)
            history[idx] = best_rank_of_day

        hit_days = sum(1 for score in raw_day_scores if score > 0)
        recent_hit_days = sum(1 for score in raw_day_scores[-RECENT_WINDOW_DAYS:] if score > 0)
        longest_streak = longest_positive_streak(raw_day_scores)
        current_streak = trailing_positive_streak(raw_day_scores)
        recent_topics = {
            meta["label"]
            for theme_key, meta in acct_theme_meta[aid].items()
            if meta["theme_type"] == "topic" and any(theme_histories[theme_key][-RECENT_WINDOW_DAYS:])
        }
        recent_buckets = {
            bucket_name
            for theme_key, buckets in acct_theme_bucket_set[aid].items()
            if any(theme_histories[theme_key][-RECENT_WINDOW_DAYS:])
            for bucket_name in buckets
        }
        topic_count = len(recent_topics)
        bucket_count = len(recent_buckets)
        base_score = round(sum(day_scores), 2)

        recent_article_keys: set[str] = set()
        for idx in range(WINDOW_DAYS - RECENT_WINDOW_DAYS, WINDOW_DAYS):
            for bucket in day_theme_map.get(idx, {}).values():
                recent_article_keys.update(bucket.get("articles", {}).keys())
        recent_article_count = len(recent_article_keys)

        best_today = None
        for kw_text, _ in acct_kw_history[aid].items():
            today_rank = kw_latest_ranks_map.get(kw_text, {}).get(aid)
            if today_rank is not None and (best_today is None or today_rank < best_today):
                best_today = today_rank

        kw_breakdown = {}
        for kw_text, kw_hist in acct_kw_history[aid].items():
            kw_ranks = [r for r in kw_hist if r > 0]
            article_items = list(acct_articles[aid].get(kw_text, {}).values())
            article_items.sort(key=lambda item: (item["rank"], item["published_at"] or "", item["title"]))
            kw_breakdown[kw_text] = {
                "history": kw_hist,
                "hit_days": len(kw_ranks),
                "best_rank": min(kw_ranks) if kw_ranks else None,
                "today_rank": kw_latest_ranks_map.get(kw_text, {}).get(aid),
                "today_prev": kw_prev_ranks_map.get(kw_text, {}).get(aid),
                "articles": article_items,
            }

        topic_breakdown = {}
        for theme_key, topic_hist in theme_histories.items():
            topic_ranks = [r for r in topic_hist if r > 0]
            topic_articles = list(acct_theme_articles[aid].get(theme_key, {}).values())
            topic_articles.sort(key=lambda item: (item["rank"], item["published_at"] or "", item["title"]))
            meta = acct_theme_meta[aid].get(theme_key, {})
            topic_breakdown[theme_key] = {
                "label": meta.get("label", theme_key),
                "theme_type": meta.get("theme_type", "topic"),
                "history": topic_hist,
                "day_scores": theme_day_scores[theme_key],
                "hit_days": len(topic_ranks),
                "best_rank": min(topic_ranks) if topic_ranks else None,
                "article_count": len(topic_articles),
                "keyword_count": len(acct_theme_keyword_set[aid].get(theme_key, set())),
                "keywords": sorted(acct_theme_keyword_set[aid].get(theme_key, set())),
                "bucket_count": len(acct_theme_bucket_set[aid].get(theme_key, set())),
                "buckets": sorted(acct_theme_bucket_set[aid].get(theme_key, set())),
                "articles": topic_articles,
            }

        unique_article_count = len({
            article["url"] + "::" + article["title"]
            for articles_by_kw in acct_articles[aid].values()
            for article in articles_by_kw.values()
        })

        all_article_titles = {
            article["title"]
            for articles_by_kw in acct_articles[aid].values()
            for article in articles_by_kw.values()
        }
        today_hit_count = len(today_titles_by_acct.get(aid, set()) & all_article_titles)
        today_hit_ratio = round(today_hit_count / unique_article_count, 2) if unique_article_count > 0 else 0.0

        move_summary = summarize_account_keyword_moves(kw_breakdown)

        # 账号级 metrics：朋友关注数、原创文章数（账号属性，取最新非空值）
        _all_acct_arts = [
            art for arts_by_kw in acct_articles[aid].values()
            for art in arts_by_kw.values()
        ]
        _ff_vals = [a.get("friends_follow_count") for a in _all_acct_arts if a.get("friends_follow_count") is not None]
        _orig_vals = [a.get("original_article_count") for a in _all_acct_arts if a.get("original_article_count") is not None]
        _friends_follow = max(_ff_vals) if _ff_vals else None
        _original_count = max(_orig_vals) if _orig_vals else None

        account_calcs.append({
            "aid": aid,
            "day_scores": day_scores,
            "raw_day_scores": raw_day_scores,
            "history": history,
            "theme_histories": theme_histories,
            "day_theme_map": day_theme_map,
            "rank_events": _flatten_rank_events(acct_day_keyword_articles[aid]),
            "base_score": base_score,
            "hit_days": hit_days,
            "recent_hit_days": recent_hit_days,
            "longest_streak": longest_streak,
            "current_streak": current_streak,
            "topic_count": topic_count,
            "bucket_count": bucket_count,
            "recent_article_count": recent_article_count,
            "best_today": best_today,
            "kw_breakdown": kw_breakdown,
            "topic_breakdown": topic_breakdown,
            "unique_article_count": unique_article_count,
            "today_hit_count": today_hit_count,
            "today_hit_ratio": today_hit_ratio,
            "move_summary": move_summary,
            "friends_follow_count": _friends_follow,
            "original_article_count": _original_count,
        })

    for calc in account_calcs:
        calc["current_raw_boards"] = _build_raw_board_snapshots(
            calc["rank_events"],
            WINDOW_DAYS - 1,
            ctx.window_dates[0],
        )
        calc["previous_raw_boards"] = _build_raw_board_snapshots(
            calc["rank_events"],
            WINDOW_DAYS - 2,
            ctx.window_dates[0],
        )
    board_benchmarks = _build_board_benchmarks([
        calc["current_raw_boards"]
        for calc in account_calcs
    ])

    acct_summaries = []
    for calc in account_calcs:
        aid = calc["aid"]
        normalized_boards: dict[str, dict[str, Any]] = {}
        previous_normalized_boards: dict[str, dict[str, Any]] = {}
        board_parts: dict[str, dict[str, Any]] = {}
        previous_board_parts: dict[str, dict[str, Any]] = {}
        board_hexagons: dict[str, dict[str, Any]] = {}
        for board in BOARD_CONFIGS:
            current_normalized, current_parts = _score_board_snapshot(
                calc["current_raw_boards"][board],
                board,
                board_benchmarks[board],
            )
            previous_normalized, previous_parts = _score_board_snapshot(
                calc["previous_raw_boards"][board],
                board,
                board_benchmarks[board],
            )
            normalized_boards[board] = current_normalized
            previous_normalized_boards[board] = previous_normalized
            board_parts[board] = current_parts
            previous_board_parts[board] = previous_parts
            board_hexagons[board] = _build_hexagon_payload(
                board,
                current_normalized,
                previous_normalized,
                board_benchmarks[board],
            )

        final_score = board_parts["account"]["score"]
        previous_score = previous_board_parts["account"]["score"]
        timeliness_score = board_parts["timeliness"]["score"]
        previous_timeliness_score = previous_board_parts["timeliness"]["score"]
        today_score = board_parts["today"]["score"]
        previous_today_score = previous_board_parts["today"]["score"]

        score_explain = (
            f"账号分 {final_score}：100是滚动窗口P99基准线，不是上限。"
            f"重点看历史覆盖、近期覆盖、经典文章、持续经营、内容矩阵和战场广度；"
            f"今天相对昨天变化 {final_score - previous_score:+d}。"
        )
        timeliness_explain = (
            f"时效分 {timeliness_score}：100是最近3天冲榜P99基准线，不是上限。"
            f"重点看Top3规模、Top3广度、新进Top3、新文冲榜、连续冲榜和上升动能；"
            f"今天相对昨天变化 {timeliness_score - previous_timeliness_score:+d}。"
        )
        today_explain = (
            f"当天分 {today_score}：只回答今天谁强，100是今日表现P99基准线。"
            f"综合今日Top3、关键词、文章、主题、排名质量和较昨日增长；"
            f"今天相对昨天变化 {today_score - previous_today_score:+d}。"
        )

        account_current = normalized_boards["account"]
        account_details = account_current["details"]
        acct_summaries.append({
            "name": acct_by_id[aid]["canonical_name"],
            "account_id": aid,
            "headimg_url": acct_by_id[aid].get("headimg_url") or None,
            "score": final_score,
            "score_raw": board_parts["account"]["score_raw"],
            "score_yesterday": previous_score,
            "score_delta": final_score - previous_score,
            "score_level": _score_level(final_score),
            "timeliness_score": timeliness_score,
            "timeliness_score_raw": board_parts["timeliness"]["score_raw"],
            "timeliness_score_yesterday": previous_timeliness_score,
            "timeliness_score_delta": timeliness_score - previous_timeliness_score,
            "timeliness_score_level": _score_level(timeliness_score),
            "today_score": today_score,
            "today_score_raw": board_parts["today"]["score_raw"],
            "today_score_yesterday": previous_today_score,
            "today_score_delta": today_score - previous_today_score,
            "today_score_level": _score_level(today_score),
            "score_explain": score_explain,
            "timeliness_explain": timeliness_explain,
            "today_explain": today_explain,
            "account_score_hexagon": board_hexagons["account"],
            "timeliness_score_hexagon": board_hexagons["timeliness"],
            "today_score_hexagon": board_hexagons["today"],
            "account_score_parts": {
                **board_parts["account"],
                **account_current["axis_values"],
            },
            "timeliness_score_parts": {
                **board_parts["timeliness"],
                **normalized_boards["timeliness"]["axis_values"],
            },
            "today_score_parts": {
                **board_parts["today"],
                **normalized_boards["today"]["axis_values"],
            },
            "base_score": calc["base_score"],
            "continuity_multiplier": account_current["axis_values"].get("continuity", 0.0) / 100,
            "topic_breadth_bonus": account_current["axis_values"].get("battle_breadth", 0.0) / 100,
            "bucket_breadth_bonus": account_current["axis_values"].get("content_matrix", 0.0) / 100,
            "history": calc["history"],
            "day_scores": calc["day_scores"],
            "raw_day_scores": calc["raw_day_scores"],
            "kw_count": len(acct_kw_history[aid]),
            "topic_count": account_details.get("recent_topic_count", calc["topic_count"]),
            "bucket_count": account_details.get("recent_bucket_count", calc["bucket_count"]),
            "recent_article_count": account_details.get("recent_article_count", calc["recent_article_count"]),
            "classic_article_count": account_details.get("classic_article_count", 0),
            "classic_pair_count": account_details.get("classic_pair_count", 0),
            "classic_rank_days": account_details.get("classic_rank_days", 0),
            "article_count": calc["unique_article_count"],
            "today_hit_count": calc["today_hit_count"],
            "today_hit_ratio": calc["today_hit_ratio"],
            "hit_days": calc["hit_days"],
            "recent_hit_days": calc["recent_hit_days"],
            "longest_streak": calc["longest_streak"],
            "current_streak": calc["current_streak"],
            "best_today": calc["best_today"],
            "move_summary": calc["move_summary"],
            "friends_follow_count": calc["friends_follow_count"],
            "original_article_count": calc["original_article_count"],
            "topics": calc["topic_breakdown"],
            "keywords": calc["kw_breakdown"],
        })
    _attach_population_context(acct_summaries)
    acct_summaries.sort(
        key=lambda a: (-a["score_raw"], -a["recent_hit_days"], -a["current_streak"], -a["hit_days"], a["name"])
    )
    return acct_summaries


__all__ = ["build_account_theme_indexes", "build_account_summaries"]
