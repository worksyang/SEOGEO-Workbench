"""monitor_scoring.py — 监控派生层纯评分函数。

三榜 P99 自适应评分框架，从微信 Top3 自适应 P99 框架迁移至小红书。

每个榜（board）定义：
- 7 轴（axes_meta），每轴权重（weights）
- 先取全量账号的 raw 值，算 P99 benchmark
- raw <= P99 → 100*raw/P99; raw > P99 → 100 + 40*log2(raw/P99)
- 主分采用轻量成熟度校准：1/2/3/4/5 个有效日分别释放 88%/92%/95%/98%/100%
- 总分突破 100 的门控：confidence=1, 基础加权分>=85, 至少2轴>P99, 至少1个关键轴>P99

三榜：
1) account_score（15天窗口）— 搜索资产力
   轴: history_coverage 14%, recent_coverage 12%, durable_notes 20%,
       continuity 15%, content_matrix 13%, engagement_quality 20%, battle_breadth 6%
   关键突破轴: durable_notes, engagement_quality
   满置信度: 5个有效观察日

2) timeliness_score（最近3个有效日）— 冲榜动能
   轴: top3_volume 24%, top3_breadth 14%, new_top3 18%,
       fresh_top3 12%, upward_momentum 12%, new_entry_engagement 12%, top3_continuity 8%
   关键突破轴: top3_volume, new_top3
   满置信度: 3个有效观察日

3) today_score（今日窗口）— 即时战力
   轴: today_top3 24%, today_keywords 18%, today_notes 14%,
       today_rank_quality 16%, today_new_entries 12%, today_engagement_quality 10%, today_breadth 6%
   关键突破轴: today_top3, today_keywords
   满置信度: 今日关键词刷新完整度

所有函数零外部依赖，只接收基础数值，返回 dict/数值。
"""
from __future__ import annotations

import math
from typing import Any

WINDOW_DAYS = 15
RECENT_WINDOW_DAYS = 7
TIMELINESS_WINDOW_DAYS = 3

SCORE_BENCHMARK_PERCENTILE = 0.99
SCORE_OVERFLOW_LOG_SCALE = 40.0
CLASSIC_MIN_DAYS = 3
FRESH_ARTICLE_DAYS = 30


# ── 三榜配置 ──

BOARD_CONFIGS = {
    "account": {
        "label": "搜索资产力",
        "window_label": "滚动15天",
        "axes_meta": [
            {"key": "history_coverage", "label": "历史覆盖", "desc": "滚动15天不同关键词、笔记与topic覆盖（log1p聚合）"},
            {"key": "recent_coverage", "label": "近期覆盖", "desc": "最近7天仍然有效的关键词与笔记覆盖"},
            {"key": "durable_notes", "label": "稳定笔记", "desc": "同一笔记×关键词至少3个不同自然日出现"},
            {"key": "continuity", "label": "持续经营", "desc": "近7天在榜、15天在榜与当前连续命中"},
            {"key": "content_matrix", "label": "内容矩阵", "desc": "有效笔记数/熵，降低单篇集中度惩罚"},
            {"key": "engagement_quality", "label": "互动质量", "desc": "唯一笔记可见互动当量E=点赞+收藏*2.5+评论*3+分享*4"},
            {"key": "battle_breadth", "label": "战场广度", "desc": "不同关键词类别的有效覆盖与均衡度，不重复计算关键词数量"},
        ],
        "weights": {
            "history_coverage": 0.14,
            "recent_coverage": 0.12,
            "durable_notes": 0.20,
            "continuity": 0.15,
            "content_matrix": 0.13,
            "engagement_quality": 0.20,
            "battle_breadth": 0.06,
        },
        "required_breakthrough_axes": {"durable_notes", "engagement_quality"},
        "confidence_days": 5,
    },
    "timeliness": {
        "label": "冲榜动能",
        "window_label": "最近3个有效日",
        "axes_meta": [
            {"key": "top3_volume", "label": "Top3规模", "desc": "最近3天进入前三的笔记×关键词命中规模"},
            {"key": "top3_breadth", "label": "Top3广度", "desc": "Top3覆盖的关键词与topic"},
            {"key": "new_top3", "label": "新进Top3", "desc": "此前一有效日未进Top3，今日进入Top3的笔记×关键词"},
            {"key": "fresh_top3", "label": "新文冲榜", "desc": "发布30天内笔记冲入Top3"},
            {"key": "upward_momentum", "label": "上升动能", "desc": "今日相对昨日的新进关键词与排名上升"},
            {"key": "new_entry_engagement", "label": "新进互动", "desc": "新进Top3笔记的可见互动当量"},
            {"key": "top3_continuity", "label": "连续冲榜", "desc": "最近3天中实际出现Top3命中的天数"},
        ],
        "weights": {
            "top3_volume": 0.24,
            "top3_breadth": 0.14,
            "new_top3": 0.18,
            "fresh_top3": 0.12,
            "upward_momentum": 0.12,
            "new_entry_engagement": 0.12,
            "top3_continuity": 0.08,
        },
        "required_breakthrough_axes": {"top3_volume", "new_top3"},
        "confidence_days": 3,
    },
    "today": {
        "label": "即时战力",
        "window_label": "今天",
        "axes_meta": [
            {"key": "today_top3", "label": "今日Top3", "desc": "今天进入前三的笔记×关键词命中数量"},
            {"key": "today_keywords", "label": "今日关键词", "desc": "今天覆盖的不同监控关键词数量"},
            {"key": "today_notes", "label": "今日笔记", "desc": "今天仍在榜的不同笔记数量"},
            {"key": "today_rank_quality", "label": "排名质量", "desc": "今天全部命中的平均rank权重，不只看最佳rank"},
            {"key": "today_new_entries", "label": "今日新进", "desc": "今天新出现笔记的数量（需前一有效日）"},
            {"key": "today_engagement_quality", "label": "今日互动质量", "desc": "今天命中笔记的可见互动当量"},
            {"key": "today_breadth", "label": "今日广度", "desc": "今天覆盖的topic与bucket"},
        ],
        "weights": {
            "today_top3": 0.24,
            "today_keywords": 0.18,
            "today_notes": 0.14,
            "today_rank_quality": 0.16,
            "today_new_entries": 0.12,
            "today_engagement_quality": 0.10,
            "today_breadth": 0.06,
        },
        "required_breakthrough_axes": {"today_top3", "today_keywords"},
        "confidence_days": 1,
    },
}


# ── 通用辅助函数 ──

def _log_count(value: int | float) -> float:
    return math.log1p(max(float(value or 0), 0.0))


def _percentile(values: list[float], q: float) -> float:
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


def _confidence_by_days(days: int, full_days: int) -> float:
    if days <= 0:
        return 0.0
    if days >= full_days:
        return 1.0
    table_5 = {1: 0.38, 2: 0.55, 3: 0.74, 4: 0.88}
    table_3 = {1: 0.68, 2: 0.88}
    return (table_3 if full_days <= 3 else table_5).get(days, min(days / full_days, 1.0))


def _account_maturity_factor(confidence: float) -> float:
    """把观察成熟度温和地作用于主分，避免短样本直接贴近 100。

    P99 横截面从第一天就有相对排序价值，因此不再像旧版一样把主分直接乘
    confidence；但 100 代表成熟基准，必须等满 5 个有效日才完整释放。
    """
    value = max(0.0, min(float(confidence or 0.0), 1.0))
    if value <= 0:
        return 0.0
    if value >= 0.999:
        return 1.0
    if value >= 0.88:
        return 0.98
    if value >= 0.74:
        return 0.95
    if value >= 0.55:
        return 0.92
    return 0.88


def _axis_score(raw_value: float, benchmark: float) -> float:
    raw = max(float(raw_value or 0.0), 0.0)
    base = max(float(benchmark or 0.0), 1e-9)
    if raw <= 0:
        return 0.0
    if raw <= base:
        return round(100 * raw / base, 4)
    return round(100 + SCORE_OVERFLOW_LOG_SCALE * math.log2(raw / base), 4)


def _observation_span_days(events: list[dict], end_idx: int, max_days: int) -> int:
    prior_days = [event["day_idx"] for event in events if event["day_idx"] <= end_idx]
    if not prior_days:
        return 0
    return min(max(end_idx - min(prior_days) + 1, 0), max_days)


def _events_in_window(events: list[dict], start_idx: int, end_idx: int) -> list[dict]:
    return [event for event in events if start_idx <= event["day_idx"] <= end_idx]


def _event_sets(events: list[dict]) -> tuple[set[str], set[str], set[str], set[str]]:
    return (
        {event["keyword"] for event in events},
        {event["article_key"] for event in events},
        {event["topic"] for event in events},
        {event["bucket"] for event in events},
    )


def _trailing_event_streak(events: list[dict], end_idx: int, max_days: int) -> int:
    active_days = {event["day_idx"] for event in events}
    streak = 0
    for day_idx in range(end_idx, end_idx - max_days, -1):
        if day_idx not in active_days:
            break
        streak += 1
    return streak


def _effective_article_count(events: list[dict]) -> tuple[float, float]:
    counts: dict[str, int] = {}
    for event in events:
        key = event["article_key"]
        counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values())
    if total <= 0:
        return 0.0, 1.0
    probabilities = [count / total for count in counts.values()]
    effective = math.exp(-sum(p * math.log(p) for p in probabilities))
    concentration = max(counts.values()) / total
    return round(effective, 4), round(concentration, 4)


def _engagement_equivalent(note_interactions: dict) -> float:
    liked = note_interactions.get("liked_count", 0)
    collected = note_interactions.get("collected_count", 0)
    comment = note_interactions.get("comment_count", 0)
    shared = note_interactions.get("shared_count", 0)

    def _safe(v):
        if isinstance(v, (int, float)) and v is not None and v >= 0:
            return float(v)
        return 0.0

    return round(
        _safe(liked) + _safe(collected) * 2.5 + _safe(comment) * 3.0 + _safe(shared) * 4.0,
        4,
    )


def _move_counts(events: list[dict], end_idx: int) -> dict[str, int]:
    current = _best_ranks_by_keyword(events, end_idx)
    previous = _best_ranks_by_keyword(events, end_idx - 1)
    result = {"new_count": 0, "up_count": 0, "down_count": 0, "flat_count": 0}
    for keyword, rank in current.items():
        prev_rank = previous.get(keyword)
        if prev_rank is None:
            result["new_count"] += 1
        elif rank < prev_rank:
            result["up_count"] += 1
        elif rank > prev_rank:
            result["down_count"] += 1
        else:
            result["flat_count"] += 1
    return result


def _best_ranks_by_keyword(events: list[dict], day_idx: int) -> dict[str, int]:
    best: dict[str, int] = {}
    for event in events:
        if event["day_idx"] != day_idx:
            continue
        keyword = event["keyword"]
        rank = event["rank"]
        if keyword not in best or rank < best[keyword]:
            best[keyword] = rank
    return best


def _coverage_raw(events: list[dict]) -> float:
    keywords, articles, _topics, buckets = _event_sets(events)
    return round(
        0.55 * _log_count(len(keywords))
        + 0.30 * _log_count(len(articles))
        + 0.15 * _log_count(len(buckets)),
        6,
    )


def _category_breadth_raw(events: list[dict]) -> tuple[float, float, float]:
    """按关键词类别的有效数量与集中度衡量战场广度。

    旧公式同时计入 keyword/topic/bucket，而当前 XHS 的 topic 暂时等于
    keyword，导致同一件事被重复奖励。这里按“每个类别覆盖了多少不同关键词”
    计算熵等效类别数，既奖励跨类别，又惩罚只集中在单一类别。
    """
    category_keywords: dict[str, set[str]] = {}
    for event in events:
        category = str(event.get("bucket") or "未分类")
        category_keywords.setdefault(category, set()).add(str(event.get("keyword") or ""))
    counts = [len(keywords) for keywords in category_keywords.values() if keywords]
    total = sum(counts)
    if total <= 0:
        return 0.0, 0.0, 1.0
    probabilities = [count / total for count in counts]
    effective_categories = math.exp(-sum(p * math.log(p) for p in probabilities))
    concentration = max(probabilities)
    raw = _log_count(effective_categories) * (1.0 - 0.25 * concentration)
    return round(raw, 6), round(effective_categories, 4), round(concentration, 4)


def _parse_article_date(value: str | None):
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _available_axes_for_board(
    board: str,
    raw_snapshot: dict[str, Any],
) -> tuple[list[str], list[str], float]:
    """返回 (available_axes, unavailable_axes, available_weight).
    
    规则：
    - account 榜：durable_notes 在有效观察日<3天时为 unavailable
    - 其他榜：所有轴默认 available
    - current 和 previous 分别判断（各自的有效观察日不同）
    """
    config = BOARD_CONFIGS[board]
    all_axes = list(config["weights"].keys())
    weights = config["weights"]
    
    observation_span_days = raw_snapshot.get("details", {}).get("observation_span_days", 0)
    history_unavailable = (
        board == "account"
        and "history_coverage" in all_axes
        and observation_span_days <= RECENT_WINDOW_DAYS
    )
    durable_unavailable = (
        board == "account"
        and "durable_notes" in all_axes
        and observation_span_days < CLASSIC_MIN_DAYS
    )
    
    unavailable = []
    if history_unavailable:
        unavailable.append("history_coverage")
    if durable_unavailable:
        unavailable.append("durable_notes")
    
    available = [a for a in all_axes if a not in unavailable]
    available_weight = sum(weights[a] for a in available)
    return available, unavailable, available_weight


def _score_board_snapshot_v2(
    raw_snapshot: dict[str, Any],
    board: str,
    benchmarks: dict[str, float],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """v2 评分：可用轴、置信度分离、突破门槛收紧。"""
    config = BOARD_CONFIGS[board]
    weights = config["weights"]
    axis_values = {
        key: _axis_score(raw_snapshot["raw_axes"].get(key, 0.0), benchmarks.get(key, 1.0))
        for key in weights
    }
    confidence = float(raw_snapshot.get("confidence") or 0.0)
    
    available_axes, unavailable_axes, available_weight = _available_axes_for_board(board, raw_snapshot)
    
    if available_weight > 0:
        base_score = sum(min(axis_values[key], 100.0) * weights[key] for key in available_axes) / available_weight
    else:
        base_score = 0.0
    
    confidence_adjusted_score = base_score * confidence
    maturity_factor = _account_maturity_factor(confidence) if board == "account" else 1.0
    maturity_calibrated_score = base_score * maturity_factor
    
    if available_weight > 0:
        breakthrough_energy = sum(max(axis_values[key] - 100.0, 0.0) * weights[key] for key in available_axes) / available_weight
    else:
        breakthrough_energy = 0.0
    
    over_axes = [key for key, value in axis_values.items() if key in available_axes and value > 100.0001]
    required_axes = config["required_breakthrough_axes"]
    
    durable_available = "durable_notes" not in unavailable_axes if board == "account" else True
    breakthrough_gate = (
        confidence >= 0.999
        and durable_available
        and base_score >= 85.0
        and len(over_axes) >= 2
        and any(key in required_axes for key in over_axes)
    )
    
    score_raw = (
        maturity_calibrated_score + breakthrough_energy
        if breakthrough_gate
        else min(maturity_calibrated_score, 100.0)
    )
    score = max(0, int(round(score_raw)))
    
    normalized = {
        **raw_snapshot,
        "axes": {key: max(0, int(round(value))) for key, value in axis_values.items()},
        "axis_values": {key: round(value, 4) for key, value in axis_values.items()},
        "breakthrough_energy": round(breakthrough_energy, 4),
    }
    parts = {
        "score_raw": round(score_raw, 4),
        "base_score": round(base_score, 4),
        "breakthrough_energy": round(breakthrough_energy, 4),
        "confidence": round(confidence, 4),
        "breakthrough": score > 100,
        "breakthrough_gate": breakthrough_gate,
        "over_axes": over_axes,
        "available_axes": available_axes,
        "unavailable_axes": unavailable_axes,
        "available_weight": round(available_weight, 4),
        "strength_score": round(base_score, 4),
        "maturity_factor": round(maturity_factor, 4),
        "maturity_calibrated_score": round(maturity_calibrated_score, 4),
        "confidence_adjusted_score": round(confidence_adjusted_score, 4),
        "score_semantics": "v3_evidence_calibrated: available-axis P99 strength with mild maturity calibration; conservative score shown separately",
    }
    return normalized, {"score": score, **parts}




# ── 各榜 raw snapshot 构建 ──

def _build_account_raw_snapshot(events: list[dict], end_idx: int, articles_by_key: dict[str, dict], global_effective_day_count: int | None = None) -> dict[str, Any]:
    history = _events_in_window(events, end_idx - WINDOW_DAYS + 1, end_idx)
    recent = _events_in_window(events, end_idx - RECENT_WINDOW_DAYS + 1, end_idx)
    history_keywords, history_articles, history_topics, history_buckets = _event_sets(history)
    recent_keywords, recent_articles, recent_topics, recent_buckets = _event_sets(recent)

    # durable_notes: 同一笔记×关键词至少3个不同自然日出现
    durable_pair_days: dict[tuple[str, str], set[int]] = {}
    for event in history:
        key = (event["keyword"], event["article_key"])
        if key not in durable_pair_days:
            durable_pair_days[key] = set()
        durable_pair_days[key].add(event["day_idx"])
    durable_pairs = {
        pair: days for pair, days in durable_pair_days.items()
        if len(days) >= CLASSIC_MIN_DAYS
    }
    durable_articles = {pair[1] for pair in durable_pairs}
    durable_keywords = {pair[0] for pair in durable_pairs}
    durable_days = sum(len(days) for days in durable_pairs.values())

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

    durable_raw = (
        0.35 * _log_count(len(durable_pairs))
        + 0.25 * _log_count(len(durable_articles))
        + 0.15 * _log_count(len(durable_keywords))
        + 0.25 * _log_count(durable_days)
    )

    # engagement_quality: 按唯一笔记聚合，不重复累加
    engagement_total = 0.0
    engagement_rank_quality = 0.0
    engagement_count = 0
    # 全局唯一笔记去重
    unique_article_keys = set()
    for event in history:
        unique_article_keys.add(event["article_key"])
    for ak in unique_article_keys:
        art = articles_by_key.get(ak, {})
        eq = _engagement_equivalent(art)
        if eq > 0:
            engagement_total += eq
            engagement_count += 1
    # rank quality: 对每个唯一笔记取最佳 rank quality，然后在唯一笔记间平均，严格 0..1
    unique_rank_quality_sum = 0.0
    unique_rank_quality_count = 0
    for ak in unique_article_keys:
        best_rq = 0.0
        has_rank = False
        for event in history:
            if event["article_key"] == ak:
                rq = (11 - min(event["rank"], 10)) / 10.0
                if rq > best_rq:
                    best_rq = rq
                has_rank = True
        if has_rank:
            unique_rank_quality_sum += best_rq
            unique_rank_quality_count += 1
    if unique_rank_quality_count > 0:
        engagement_rank_quality = min(unique_rank_quality_sum / unique_rank_quality_count, 1.0)
    else:
        engagement_rank_quality = 0.0
    engagement_raw = _log_count(engagement_total) * (0.7 + 0.3 * engagement_rank_quality)

    breadth_raw, effective_category_count, category_concentration = _category_breadth_raw(history)

    if global_effective_day_count is not None:
        observation_span_days = global_effective_day_count
        confidence = _confidence_by_days(global_effective_day_count, BOARD_CONFIGS["account"]["confidence_days"])
    else:
        observation_span_days = _observation_span_days(events, end_idx, WINDOW_DAYS)
        confidence = _confidence_by_days(observation_span_days, BOARD_CONFIGS["account"]["confidence_days"])

    # durable_notes_status
    if observation_span_days < CLASSIC_MIN_DAYS:
        durable_notes_status = "waiting"
        durable_notes_message = f"等待第{CLASSIC_MIN_DAYS}天验证（当前{observation_span_days}天）"
    elif len(durable_pairs) == 0:
        durable_notes_status = "no_durable"
        durable_notes_message = "暂无稳定笔记（同一笔记×关键词未在至少3个不同自然日出现）"
    else:
        durable_notes_status = "stable"
        durable_notes_message = ""

    return {
        "end_idx": end_idx,
        "raw_axes": {
            "history_coverage": _coverage_raw(history),
            "recent_coverage": _coverage_raw(recent),
            "durable_notes": round(durable_raw, 6),
            "continuity": round(continuity, 6),
            "content_matrix": round(content_matrix, 6),
            "engagement_quality": round(engagement_raw, 6),
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
            "durable_pair_count": len(durable_pairs),
            "durable_article_count": len(durable_articles),
            "durable_keyword_count": len(durable_keywords),
            "durable_rank_days": durable_days,
            "history_active_days": len(history_days),
            "recent_active_days": len(recent_days),
            "observation_span_days": observation_span_days,
            "current_streak": current_streak,
            "effective_article_count": effective_articles,
            "article_concentration": concentration,
            "effective_category_count": effective_category_count,
            "category_concentration": category_concentration,
            "engagement_total": round(engagement_total, 4),
            "engagement_rank_quality": round(engagement_rank_quality, 4),
            "durable_notes_status": durable_notes_status,
            "durable_notes_message": durable_notes_message,
        },
    }


def _build_timeliness_raw_snapshot(
    events: list[dict],
    end_idx: int,
    end_date: Any,
    articles_by_key: dict[str, dict],
    previous_effective_day_idx: int | None = None,
    global_effective_day_indices: list[int] | None = None,
) -> dict[str, Any]:
    # 使用最近3个全局有效日（而非连续日历end_idx-2..end_idx）
    if global_effective_day_indices and len(global_effective_day_indices) >= 1:
        effective_days_past = [d for d in global_effective_day_indices if d <= end_idx]
        recent_effective_days = effective_days_past[-TIMELINESS_WINDOW_DAYS:] if len(effective_days_past) >= TIMELINESS_WINDOW_DAYS else effective_days_past
        recent = [event for event in events if event["day_idx"] in recent_effective_days]
    else:
        recent = _events_in_window(events, end_idx - TIMELINESS_WINDOW_DAYS + 1, end_idx)
    top3 = [event for event in recent if event["rank"] <= 3]
    top3_keywords, top3_articles, top3_topics, top3_buckets = _event_sets(top3)

    # new_top3: 只比较当前有效日Top3与前一有效日Top3（不是整个3日窗口）
    # 当前有效日 = end_idx
    current_day_events = [event for event in events if event["day_idx"] == end_idx]
    current_top3_pairs = {
        (event["keyword"], event["article_key"])
        for event in current_day_events if event.get("rank", 999) <= 3
    }
    # 前一有效日
    if previous_effective_day_idx is not None:
        prior_day_events = [event for event in events if event["day_idx"] == previous_effective_day_idx]
        prior_top3_pairs = {
            (event["keyword"], event["article_key"])
            for event in prior_day_events if event.get("rank", 999) <= 3
        }
    else:
        prior_top3_pairs = set()
    new_top3_pairs = current_top3_pairs - prior_top3_pairs

    # fresh_top3: 新文冲榜
    fresh_articles = set()
    for event in top3:
        published_date = _parse_article_date(event.get("published_at"))
        if published_date is not None and 0 <= (end_date - published_date).days <= FRESH_ARTICLE_DAYS:
            fresh_articles.add(event["article_key"])

    # new_entry_engagement: 按唯一笔记去重
    new_entry_article_keys = {pair[1] for pair in new_top3_pairs}
    new_entry_eq = 0.0
    for ak in new_entry_article_keys:
        art = articles_by_key.get(ak, {})
        new_entry_eq += _engagement_equivalent(art)
    new_entry_raw = _log_count(new_entry_eq)

    top3_days = {event["day_idx"] for event in top3}
    if global_effective_day_indices and len(global_effective_day_indices) >= 1:
        effective_days_past = [d for d in global_effective_day_indices if d <= end_idx]
        observed_span_days = min(len(effective_days_past), TIMELINESS_WINDOW_DAYS)
    else:
        observed_span_days = _observation_span_days(events, end_idx, TIMELINESS_WINDOW_DAYS)
    moves = _move_counts(events, end_idx)
    upward_raw = _log_count(moves["new_count"] + 0.6 * moves["up_count"])

    return {
        "end_idx": end_idx,
        "raw_axes": {
            "top3_volume": _log_count(len(top3)),
            "top3_breadth": round(
                0.75 * _log_count(len(top3_keywords))
                + 0.25 * _log_count(len(top3_buckets)),
                6,
            ),
            "new_top3": _log_count(len(new_top3_pairs)),
            "fresh_top3": _log_count(len(fresh_articles)),
            "upward_momentum": round(upward_raw, 6),
            "new_entry_engagement": round(new_entry_raw, 6),
            "top3_continuity": round(len(top3_days) / TIMELINESS_WINDOW_DAYS, 6),
        },
        "confidence": round(_confidence_by_days(observed_span_days, BOARD_CONFIGS["timeliness"]["confidence_days"]), 4),
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
            "new_entry_article_count": len(new_entry_article_keys),
            "new_entry_engagement_total": round(new_entry_eq, 4),
            **moves,
        },
    }


def _build_today_raw_snapshot(
    events: list[dict],
    end_idx: int,
    articles_by_key: dict[str, dict],
    previous_effective_day_idx: int | None = None,
    today_confidence: float = 1.0,
    global_effective_day_indices: list[int] | None = None,
) -> dict[str, Any]:
    today = _events_in_window(events, end_idx, end_idx)
    top3 = [event for event in today if event["rank"] <= 3]
    keywords, articles, topics, buckets = _event_sets(today)
    unique_articles = set(articles)

    # today_new_entries: 比较小于当前日的最近一个全局有效日
    if previous_effective_day_idx is not None:
        yesterday = _events_in_window(events, previous_effective_day_idx, previous_effective_day_idx)
        yesterday_article_keys = {event["article_key"] for event in yesterday}
        new_entries = [ak for ak in unique_articles if ak not in yesterday_article_keys]
    else:
        yesterday_article_keys = set()
        new_entries = []

    # today_rank_quality: 对每个唯一笔记取最佳rank quality，然后在唯一笔记间平均，严格0..1
    rank_quality = 0.0
    if today:
        unique_rq = {}
        for event in today:
            ak = event["article_key"]
            rq = (11 - min(event["rank"], 10)) / 10.0
            if ak not in unique_rq or rq > unique_rq[ak]:
                unique_rq[ak] = rq
        rank_quality = min(sum(unique_rq.values()) / len(unique_rq), 1.0)

    # today_engagement_quality: 按唯一笔记聚合一次
    today_eq = 0.0
    for ak in unique_articles:
        art = articles_by_key.get(ak, {})
        today_eq += _engagement_equivalent(art)
    today_eq_raw = _log_count(today_eq)

    breadth_raw = _log_count(len(buckets))

    return {
        "end_idx": end_idx,
        "raw_axes": {
            "today_top3": _log_count(len(top3)),
            "today_keywords": _log_count(len(keywords)),
            "today_notes": _log_count(len(unique_articles)),
            "today_rank_quality": round(rank_quality, 6),
            "today_new_entries": _log_count(len(new_entries)),
            "today_engagement_quality": round(today_eq_raw, 6),
            "today_breadth": round(breadth_raw, 6),
        },
        "confidence": round(today_confidence, 4),
        "details": {
            "today_top3_count": len(top3),
            "today_keyword_count": len(keywords),
            "today_article_count": len(unique_articles),
            "today_topic_count": len(topics),
            "today_bucket_count": len(buckets),
            "today_hit_count": len(today),
            "today_new_entry_count": len(new_entries),
            "average_rank_quality": round(rank_quality, 4),
            "today_engagement_total": round(today_eq, 4),
        },
    }


def build_raw_board_snapshots(
    events: list[dict],
    end_idx: int,
    window_start: Any,
    articles_by_key: dict[str, dict],
    previous_effective_day_idx: int | None = None,
    today_confidence: float = 1.0,
    global_effective_day_indices: list[int] | None = None,
) -> dict[str, dict[str, Any]]:
    from datetime import timedelta
    end_date = window_start + timedelta(days=end_idx)
    # 计算截至end_idx的全局有效日数
    global_effective_day_count = None
    if global_effective_day_indices:
        global_effective_day_count = len([d for d in global_effective_day_indices if d <= end_idx])
    return {
        "account": _build_account_raw_snapshot(events, end_idx, articles_by_key, global_effective_day_count=global_effective_day_count),
        "timeliness": _build_timeliness_raw_snapshot(events, end_idx, end_date, articles_by_key, previous_effective_day_idx, global_effective_day_indices=global_effective_day_indices),
        "today": _build_today_raw_snapshot(events, end_idx, articles_by_key, previous_effective_day_idx, today_confidence, global_effective_day_indices=global_effective_day_indices),
    }


def build_board_benchmarks(
    current_snapshots: list[dict[str, dict[str, Any]]],
) -> dict[str, dict[str, float]]:
    benchmarks: dict[str, dict[str, float]] = {}
    for board, config in BOARD_CONFIGS.items():
        benchmarks[board] = {}
        for meta in config["axes_meta"]:
            key = meta["key"]
            values = [snapshot[board]["raw_axes"].get(key, 0.0) for snapshot in current_snapshots]
            benchmarks[board][key] = round(_percentile(values, SCORE_BENCHMARK_PERCENTILE), 6)
    return benchmarks


def score_board_snapshot(
    raw_snapshot: dict[str, Any],
    board: str,
    benchmarks: dict[str, float],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """dispatch to v2 for account board, keep v1 for timeliness/today."""
    if board == "account":
        return _score_board_snapshot_v2(raw_snapshot, board, benchmarks)
    
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
        "breakthrough_energy": round(breakthrough_energy, 4),
    }
    parts = {
        "score_raw": round(score_raw, 4),
        "base_score": round(base_score, 4),
        "breakthrough_energy": round(breakthrough_energy, 4),
        "confidence": round(confidence, 4),
        "breakthrough": score > 100,
        "breakthrough_gate": breakthrough_gate,
        "over_axes": over_axes,
        "available_axes": list(weights.keys()),
        "unavailable_axes": [],
        "available_weight": 1.0,
        "confidence_adjusted_score": round(base_score, 4),
        "score_semantics": "xhs_three_board_p99_v1",
    }
    return normalized, {"score": score, **parts}


def build_hexagon_payload(
    board: str,
    current: dict[str, Any],
    previous: dict[str, Any],
    benchmarks: dict[str, float],
    previous_benchmarks: dict[str, float] | None = None,
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
        "previous_benchmarks": previous_benchmarks if previous_benchmarks is not None else benchmarks,
        "current": current,
        "previous": previous,
        "delta": {
            key: int(current["axes"].get(key, 0) - previous["axes"].get(key, 0))
            for key in current["axes"]
        },
        "population": {},
        "previous_population": {},
    }


def _score_level(score: int) -> str:
    if score >= 120:
        return "extreme_breakthrough"
    if score >= 110:
        return "strong_breakthrough"
    if score > 100:
        return "breakthrough"
    if score >= 100:
        return "benchmark"
    return "within_benchmark"


def _population_stat(values: list[float], value: float) -> dict[str, int | float]:
    """把单个值翻译成全站名次、并列数和严格超过比例。
    rank: 数字名次，tie_count含自身，percentile为严格低于当前值的账号占比。
    """
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


def _attach_hexagon_population(acct_summaries: list[dict]) -> None:
    """给三套六边形补充population/previous_population（WeChat契约），含每轴人口统计。"""
    board_fields = {
        "account": ("account_score_hexagon", "score", "score_yesterday"),
        "timeliness": ("timeliness_score_hexagon", "timeliness_score", "timeliness_score_yesterday"),
        "today": ("today_score_hexagon", "today_score", "today_score_yesterday"),
    }
    for board, (hexagon_field, score_field, previous_score_field) in board_fields.items():
        if not acct_summaries:
            continue
        current_score_values = [float(acct.get(score_field) or 0.0) for acct in acct_summaries]
        previous_score_values = [float(acct.get(previous_score_field) or 0.0) for acct in acct_summaries]
        axis_keys = [meta["key"] for meta in BOARD_CONFIGS[board]["axes_meta"]]
        current_axis_values = {
            key: [
                float(acct[hexagon_field]["current"]["axes"].get(key) or 0.0)
                for acct in acct_summaries
            ]
            for key in axis_keys
        }
        previous_axis_values = {
            key: [
                float(acct[hexagon_field]["previous"]["axes"].get(key) or 0.0)
                for acct in acct_summaries
            ]
            for key in axis_keys
        }
        for acct in acct_summaries:
            hexagon = acct[hexagon_field]
            hexagon["population"] = {
                "account_count": len(acct_summaries),
                "score": _population_stat(current_score_values, float(acct.get(score_field) or 0.0)),
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
                "score": _population_stat(previous_score_values, float(acct.get(previous_score_field) or 0.0)),
                "axes": {
                    key: _population_stat(
                        previous_axis_values[key],
                        float(hexagon["previous"]["axes"].get(key) or 0.0),
                    )
                    for key in axis_keys
                },
            }


__all__ = [
    "WINDOW_DAYS",
    "RECENT_WINDOW_DAYS",
    "TIMELINESS_WINDOW_DAYS",
    "SCORE_BENCHMARK_PERCENTILE",
    "SCORE_OVERFLOW_LOG_SCALE",
    "CLASSIC_MIN_DAYS",
    "FRESH_ARTICLE_DAYS",
    "BOARD_CONFIGS",
    "_log_count",
    "_percentile",
    "_confidence_by_days",
    "_axis_score",
    "_observation_span_days",
    "_events_in_window",
    "_event_sets",
    "_trailing_event_streak",
    "_effective_article_count",
    "_engagement_equivalent",
    "_move_counts",
    "_best_ranks_by_keyword",
    "_coverage_raw",
    "_parse_article_date",
    "_available_axes_for_board",
    "_score_board_snapshot_v2",
    "_build_account_raw_snapshot",
    "_build_timeliness_raw_snapshot",
    "_build_today_raw_snapshot",
    "build_raw_board_snapshots",
    "build_board_benchmarks",
    "score_board_snapshot",
    "build_hexagon_payload",
    "_score_level",
    "_population_stat",
    "_attach_hexagon_population",
]
