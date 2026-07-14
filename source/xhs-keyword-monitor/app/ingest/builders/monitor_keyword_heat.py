"""keyword_heat_metric — 小红书关键词"平台内可见互动热度"派生模型。

算法概要（见规范 1-9）：
  1. 单篇互动当量 E = liked*1 + collected*2.5 + comment*3 + shared*4，所有输入安全归零
  2. 每次快照仅取相关笔记 Top10，同笔记去重取最佳 rank；单篇贡献 log1p(E)，排名权重 1/log2(rank+1)；
     将已观测权重均值标准化回完整 Top10 权重，得到 run_heat；空榜为 0 但不可伪装成高置信
  3. 同一天多个 run_heat 取中位数为 daily heat；常态热度 = 15 日有效日 daily heat 中位数；峰值 = max；热度增量 = 末日 - 首日
  4. 趋势至少 4 个有效日才 ready：最近最多 3 个有效日中位数 vs 此前最多 7 个有效日中位数
  5. 价值信号至少 4 日：0.60*热度趋势 + 0.25*笔记供给趋势 + 0.15*创作者广度趋势
  6. current_interactions 基于最新成功快照 Top10
  7. confidence 综合有效天数、Top10 完整度、最近更新时间、可比较天数
  8. 保留旧 kw_score 仅作为兼容占位，但后端默认排序必须改成 pinned > steady_heat > peak_heat > 中文名
  9. daily_heat_points 每日包含 date/heat/run_count/note_count/creator_count/互动字段
  10. 字段无 NaN/Infinity
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any

from app.ingest.common import parse_captured_at_iso
from app.ingest.builders.monitor_scoring import WINDOW_DAYS

# ── 互动权重 ──
WT_LIKE = 1.0
WT_COLLECT = 2.5
WT_COMMENT = 3.0
WT_SHARE = 4.0
INTERACTION_METHOD_NOTE = (
    "互动当量 E = 点赞×1.0 + 收藏×2.5 + 评论×3.0 + 分享×4.0；负数或空值按 0 计。"
)

# ── 趋势阈值 ──
TREND_THRESHOLD = 0.20
VALUE_THRESHOLD = 0.20
MIN_EFFECTIVE_DAYS_FOR_TREND = 4
MIN_EFFECTIVE_DAYS_FOR_VALUE = 4

# ── 趋势窗口 ──
RECENT_MAX_DAYS = 3
BASELINE_MAX_DAYS = 7

# ── 版本 ──
METHOD = "xhs_visible_interaction_heat_v1"
METHOD_VERSION = "1.0.0"
_SUCCESS_STATUSES = {"success", "completed"}


# ── 工具函数 ──

def _safe_int(v):
    """安全归零：None/NaN/负数 → 0，浮点取整。"""
    if v is None:
        return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        if math.isnan(v) or math.isinf(v) or v < 0:
            return 0
        return int(v)
    return 0


def _safe_float(v):
    """安全归零浮点。"""
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        if math.isnan(v) or math.isinf(v) or v < 0:
            return 0.0
        return float(v)
    return 0.0


def _clamp(value, lo=-1.0, hi=1.0):
    if math.isnan(value) or math.isinf(value):
        return 0.0
    return max(lo, min(hi, value))


def _median(values):
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _rank_weight_fn(rank):
    if rank is None or rank <= 0:
        return 0.0
    return 1.0 / math.log2(max(2, rank + 1))


def _log1p(v):
    return math.log1p(max(0.0, v))


def _is_successful_run(run):
    """缺失/空状态按历史兼容成功；明确状态仅 success/completed 纳入。"""
    status = run.get("status")
    if status is None or not str(status).strip():
        return True
    return str(status).strip().lower() in _SUCCESS_STATUSES


def interaction_weights_payload():
    """返回稳定、可序列化的互动当量权重契约。"""
    return {
        "likes": WT_LIKE,
        "collects": WT_COLLECT,
        "comments": WT_COMMENT,
        "shares": WT_SHARE,
        "method_note": INTERACTION_METHOD_NOTE,
    }


def _trend_ratio(recent_median, baseline_median):
    """稳健趋势比，clamp[-1,1]。

    ratio = (recent - baseline) / max(abs(baseline), 1)
    """
    denom = max(abs(baseline_median), 1.0)
    return _clamp((recent_median - baseline_median) / denom)


def _trend_label(ratio):
    if ratio >= TREND_THRESHOLD:
        return "上升"
    if ratio <= -TREND_THRESHOLD:
        return "下降"
    return "平稳"


def _value_label(ratio):
    if ratio >= VALUE_THRESHOLD:
        return "价值上升"
    if ratio <= -VALUE_THRESHOLD:
        return "价值下降"
    return "价值平稳"


def _compute_rank_weights_for_top10():
    """预计算完整 Top10 的 rank 权重列表（rank=1..10）。"""
    return [_rank_weight_fn(r) for r in range(1, 11)]


_FULL_TOP10_WEIGHTS = _compute_rank_weights_for_top10()
_FULL_TOP10_WEIGHT_SUM = sum(_FULL_TOP10_WEIGHTS)


# ── 核心指标 ──

def compute_engagement_equivalent(note):
    """单篇互动当量 E = liked*1 + collected*2.5 + comment*3 + shared*4。"""
    liked = _safe_int(note.get("liked_count"))
    collected = _safe_int(note.get("collected_count"))
    comment = _safe_int(note.get("comment_count"))
    shared = _safe_int(note.get("shared_count"))
    return liked * WT_LIKE + collected * WT_COLLECT + comment * WT_COMMENT + shared * WT_SHARE


def compute_run_heat(articles, max_rank=10):
    """从单次快照的相关笔记 Top10 计算 run_heat。"""
    relevant = [a for a in articles if a.get("is_relevant") != False]
    if not relevant:
        return 0.0

    best_by_id = {}
    for a in relevant:
        art_id = a.get("article_id") or ""
        rank = a.get("rank")
        if rank is None or rank <= 0:
            rank = 999
        if art_id not in best_by_id or rank < best_by_id[art_id][0]:
            best_by_id[art_id] = (rank, a)

    ranked = sorted(best_by_id.values(), key=lambda x: x[0])
    top10 = ranked[:max_rank]
    if not top10:
        return 0.0

    observed_weight_sum = 0.0
    weighted_engagement = 0.0
    for rank, article in top10:
        w = _rank_weight_fn(rank)
        observed_weight_sum += w
        e = compute_engagement_equivalent(article)
        weighted_engagement += _log1p(e) * w

    if observed_weight_sum <= 0:
        return 0.0
    normalization = _FULL_TOP10_WEIGHT_SUM / observed_weight_sum
    return weighted_engagement * normalization


def compute_daily_heat(run_heats):
    """同一天多个 run_heat 取中位数。"""
    if not run_heats:
        return 0.0
    return _median(run_heats)


def compute_steady_heat(daily_heats):
    """常态热度 = 15 日有效日 daily heat 中位数。"""
    valid = [h for h in daily_heats if h > 0]
    if not valid:
        return 0.0
    return _median(valid)


def compute_peak_heat(daily_heats):
    """峰值 = 所有有效日 max。"""
    valid = [h for h in daily_heats if h > 0]
    if not valid:
        return 0.0
    return max(valid)


def compute_heat_delta(daily_heats):
    """热度增量 = 末日 - 首日（仅有效日）。"""
    valid = [h for h in daily_heats if h > 0]
    if len(valid) < 2:
        return 0.0
    return valid[-1] - valid[0]


def compute_trend(daily_heats, effective_days):
    """趋势分析。"""
    if effective_days < MIN_EFFECTIVE_DAYS_FOR_TREND:
        return {"trend_signal": 0.0, "trend_ratio": 0.0, "trend_label": "观察中"}

    valid_indices = [i for i, h in enumerate(daily_heats) if h > 0]
    valid_heats = [daily_heats[i] for i in valid_indices]
    if len(valid_heats) < MIN_EFFECTIVE_DAYS_FOR_TREND:
        return {"trend_signal": 0.0, "trend_ratio": 0.0, "trend_label": "观察中"}

    recent = valid_heats[-RECENT_MAX_DAYS:]
    baseline = valid_heats[:min(len(valid_heats) - len(recent), BASELINE_MAX_DAYS)]
    if not baseline:
        return {"trend_signal": 0.0, "trend_ratio": 0.0, "trend_label": "观察中"}

    recent_m = _median(recent)
    baseline_m = _median(baseline)
    ratio = _trend_ratio(recent_m, baseline_m)
    return {"trend_signal": ratio, "trend_ratio": ratio, "trend_label": _trend_label(ratio)}


def compute_value_signal(heat_trend_ratio, note_supply_trend_ratio, creator_breadth_trend_ratio):
    """价值信号 = 0.60*热度趋势 + 0.25*笔记供给趋势 + 0.15*创作者广度趋势。"""
    score = (
        0.60 * _clamp(heat_trend_ratio)
        + 0.25 * _clamp(note_supply_trend_ratio)
        + 0.15 * _clamp(creator_breadth_trend_ratio)
    )
    score = _clamp(score)
    return {
        "score": score,
        "label": _value_label(score),
        "components": {
            "heat_trend_ratio": heat_trend_ratio,
            "note_supply_trend_ratio": note_supply_trend_ratio,
            "creator_breadth_trend_ratio": creator_breadth_trend_ratio,
        },
    }


def compute_confidence(effective_days, top10_completeness, hours_since_update, comparable_days):
    """置信度评估。"""
    if effective_days < 2:
        return {"confidence_score": 0, "confidence_level": "insufficient"}

    day_score = min(40, effective_days * 4)
    completeness_score = top10_completeness * 30
    freshness_score = max(0, 20 - hours_since_update * (20 / 72))
    comp_score = min(10, comparable_days * 2)
    score = max(0, min(100, int(round(day_score + completeness_score + freshness_score + comp_score))))

    if score >= 70:
        level = "high"
    elif score >= 40:
        level = "medium"
    else:
        level = "low"
    return {"confidence_score": score, "confidence_level": level}


def compute_current_interactions(latest_run_articles, max_rank=10):
    """基于最新成功快照 Top10 的可见互动结构。"""
    relevant = [a for a in latest_run_articles if a.get("is_relevant") != False]
    best_by_id = {}
    for a in relevant:
        art_id = a.get("article_id") or ""
        rank = a.get("rank")
        if rank is None or rank <= 0:
            rank = 999
        if art_id not in best_by_id or rank < best_by_id[art_id][0]:
            best_by_id[art_id] = (rank, a)
    ranked = sorted(best_by_id.values(), key=lambda x: x[0])
    top = ranked[:max_rank]

    total_likes = 0
    total_collects = 0
    total_comments = 0
    total_shares = 0
    total_equivalent = 0.0
    creators = set()

    for _, article in top:
        likes = _safe_int(article.get("liked_count"))
        collects = _safe_int(article.get("collected_count"))
        comments = _safe_int(article.get("comment_count"))
        shares = _safe_int(article.get("shared_count"))
        total_likes += likes
        total_collects += collects
        total_comments += comments
        total_shares += shares
        total_equivalent += likes * WT_LIKE + collects * WT_COLLECT + comments * WT_COMMENT + shares * WT_SHARE
        if article.get("account_id"):
            creators.add(article["account_id"])

    if total_equivalent > 0:
        likes_pct = round((total_likes * WT_LIKE) / total_equivalent * 100, 1)
        collects_pct = round((total_collects * WT_COLLECT) / total_equivalent * 100, 1)
        comments_pct = round((total_comments * WT_COMMENT) / total_equivalent * 100, 1)
        shares_pct = round((total_shares * WT_SHARE) / total_equivalent * 100, 1)
    else:
        likes_pct = collects_pct = comments_pct = shares_pct = 0.0

    return {
        "likes": total_likes,
        "collects": total_collects,
        "comments": total_comments,
        "shares": total_shares,
        "equivalent": round(total_equivalent, 1),
        "note_count": len(top),
        "creator_count": len(creators),
        "interaction_structure": {
            "likes_pct": likes_pct, "collects_pct": collects_pct,
            "comments_pct": comments_pct, "shares_pct": shares_pct,
        },
    }


def compute_daily_heat_points(daily_heats, daily_run_counts, daily_note_counts,
                               daily_creator_counts, daily_likes, daily_collects,
                               daily_comments, daily_shares, daily_equivalents,
                               window_dates):
    """生成每日 heat 数据点。"""
    points = []
    for date in window_dates:
        points.append({
            "date": date,
            "heat": round(daily_heats.get(date, 0.0), 4),
            "run_count": daily_run_counts.get(date, 0),
            "note_count": daily_note_counts.get(date, 0),
            "creator_count": daily_creator_counts.get(date, 0),
            "likes": daily_likes.get(date, 0),
            "collects": daily_collects.get(date, 0),
            "comments": daily_comments.get(date, 0),
            "shares": daily_shares.get(date, 0),
            "equivalent": round(daily_equivalents.get(date, 0.0), 1),
        })
    return points


def compute_top10_completeness(latest_run_articles):
    """最近快照中相关笔记 Top10 的完整度 0-1。"""
    relevant = [a for a in latest_run_articles if a.get("is_relevant") != False]
    if not relevant:
        return 0.0
    count = sum(1 for a in relevant if a.get("rank") is not None and 1 <= a["rank"] <= 10)
    return min(1.0, count / 10.0)


def build_keyword_heat_metric(runs, window_dates):
    """为单个关键词构建完整的 keyword_heat_metric。"""
    successful_runs = [r for r in runs if _is_successful_run(r)]
    runs_by_date = {}
    for r in successful_runs:
        date = r.get("date") or r.get("snapshot_date") or ""
        if date:
            runs_by_date.setdefault(date, []).append(r)

    daily_run_heats = {}
    daily_note_counts = {}
    daily_creator_counts = {}
    daily_likes = {}
    daily_collects = {}
    daily_comments = {}
    daily_shares = {}
    daily_equivalents = {}

    for date, date_runs in runs_by_date.items():
        heats = []
        for r in date_runs:
            articles = r.get("articles", [])
            rh = compute_run_heat(articles)
            heats.append(rh)

            relevant = [a for a in articles if a.get("is_relevant") != False]
            best_by_id = {}
            for a in relevant:
                art_id = a.get("article_id") or ""
                rank = a.get("rank")
                if rank is None or rank <= 0:
                    rank = 999
                if art_id not in best_by_id or rank < best_by_id[art_id][0]:
                    best_by_id[art_id] = (rank, a)
            ranked = sorted(best_by_id.values(), key=lambda x: x[0])
            top10 = ranked[:10]

            for _, article in top10:
                daily_likes[date] = daily_likes.get(date, 0) + _safe_int(article.get("liked_count"))
                daily_collects[date] = daily_collects.get(date, 0) + _safe_int(article.get("collected_count"))
                daily_comments[date] = daily_comments.get(date, 0) + _safe_int(article.get("comment_count"))
                daily_shares[date] = daily_shares.get(date, 0) + _safe_int(article.get("shared_count"))
                daily_equivalents[date] = daily_equivalents.get(date, 0.0) + compute_engagement_equivalent(article)
                if article.get("account_id"):
                    daily_creator_counts.setdefault(date, set()).add(article["account_id"])
            daily_note_counts[date] = daily_note_counts.get(date, 0) + len(top10)

        daily_run_heats[date] = heats
        if date not in daily_creator_counts:
            daily_creator_counts[date] = set()

    daily_heats = {}
    for date in window_dates:
        if date in daily_run_heats:
            daily_heats[date] = compute_daily_heat(daily_run_heats[date])
        else:
            daily_heats[date] = 0.0

    effective_days = sum(1 for d in window_dates if daily_heats.get(d, 0) > 0)
    daily_heat_list = [daily_heats.get(d, 0.0) for d in window_dates]

    steady_heat = compute_steady_heat(daily_heat_list)
    peak_heat = compute_peak_heat(daily_heat_list)
    heat_delta_15d = compute_heat_delta(daily_heat_list)

    trend = compute_trend(daily_heat_list, effective_days)

    note_counts_list = [daily_note_counts.get(d, 0) for d in window_dates]
    note_effective_days = sum(1 for c in note_counts_list if c > 0)
    note_trend = compute_trend([float(c) for c in note_counts_list], note_effective_days)

    creator_counts_list = [len(daily_creator_counts.get(d, set())) for d in window_dates]
    creator_effective_days = sum(1 for c in creator_counts_list if c > 0)
    creator_trend = compute_trend([float(c) for c in creator_counts_list], creator_effective_days)

    if effective_days >= MIN_EFFECTIVE_DAYS_FOR_VALUE:
        value_signal = compute_value_signal(
            trend["trend_signal"], note_trend["trend_signal"], creator_trend["trend_signal"])
    else:
        value_signal = {"score": 0.0, "label": "观察中",
                        "components": {"heat_trend_ratio": trend["trend_signal"],
                                       "note_supply_trend_ratio": note_trend["trend_signal"],
                                       "creator_breadth_trend_ratio": creator_trend["trend_signal"]}}

    sorted_runs = sorted(successful_runs, key=lambda r: r.get("captured_at") or "", reverse=True)
    latest_run = sorted_runs[0] if sorted_runs else None
    latest_articles = latest_run.get("articles", []) if latest_run else []
    current_interactions = compute_current_interactions(latest_articles)
    top10_completeness = compute_top10_completeness(latest_articles) if latest_articles else 0.0

    if latest_run:
        cap_str = latest_run.get("captured_at") or ""
        cap_dt = parse_captured_at_iso(cap_str) if cap_str else datetime.now()
        hours_since = (datetime.now().astimezone(cap_dt.tzinfo) - cap_dt).total_seconds() / 3600
    else:
        hours_since = 999.0

    comparable_days = max(0, effective_days - RECENT_MAX_DAYS)
    confidence = compute_confidence(effective_days, top10_completeness, hours_since, comparable_days)

    if effective_days < MIN_EFFECTIVE_DAYS_FOR_TREND:
        trend = {"trend_signal": 0.0, "trend_ratio": 0.0, "trend_label": "观察中"}
    if effective_days < MIN_EFFECTIVE_DAYS_FOR_VALUE:
        value_signal = {"score": 0.0, "label": "观察中",
                        "components": {"heat_trend_ratio": 0.0, "note_supply_trend_ratio": 0.0,
                                       "creator_breadth_trend_ratio": 0.0}}
    if effective_days < 2:
        confidence = {"confidence_score": 0, "confidence_level": "insufficient"}

    peak_date = None
    if effective_days > 0:
        peak_date = max(((d, daily_heats.get(d, 0.0)) for d in window_dates), key=lambda x: x[1])[0]

    daily_heat_points = compute_daily_heat_points(
        daily_heats,
        {d: len(runs_by_date.get(d, [])) for d in window_dates},
        {d: daily_note_counts.get(d, 0) for d in window_dates},
        {d: len(daily_creator_counts.get(d, set())) for d in window_dates},
        {d: daily_likes.get(d, 0) for d in window_dates},
        {d: daily_collects.get(d, 0) for d in window_dates},
        {d: daily_comments.get(d, 0) for d in window_dates},
        {d: daily_shares.get(d, 0) for d in window_dates},
        {d: daily_equivalents.get(d, 0.0) for d in window_dates},
        window_dates,
    )

    return {
        "status": "available" if effective_days > 0 else "no_data",
        "method": METHOD,
        "version": METHOD_VERSION,
        "window_days": WINDOW_DAYS,
        "effective_days": effective_days,
        "steady_heat": round(steady_heat, 4),
        "peak_heat": round(peak_heat, 4),
        "heat_delta_15d": round(heat_delta_15d, 4),
        "peak_date": peak_date,
        **trend,
        **confidence,
        "interaction_weights": interaction_weights_payload(),
        "value_signal": value_signal,
        "current_interactions": current_interactions,
        "daily_heat_points": daily_heat_points,
    }
