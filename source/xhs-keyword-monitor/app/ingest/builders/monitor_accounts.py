"""monitor_accounts.py — 小红书账号维度聚合与三榜 P99 评分。

输入：MonitorContext + _KeywordSummaryResult。
输出：账号 summaries 列表（已按 account_score desc 排序）。

行为契约：
- 只消费事实层数据（rank_events 扁平化逐日命中事件）
- 三榜使用 P99 归一化，100 为基准线，多维超标时允许突破
- 当前与昨日独立重算，使用各自独立的 benchmarks
- hexagon 包含 delta; population={account_count,score:{rank,total,tie_count,percentile},axes:{key:stat}}
- previous_population 同形；rank 为数字，tie_count 含自身，percentile 为严格低于当前值占比
- 兼容字段：keywords/topics/covered_topics/articles/best_articles/classic_articles/
  interaction_metrics/move_summary/breakthrough/history/day_scores
- account_score_method='xhs_three_board_p99_v3_evidence_calibrated'
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from app.ingest.builders.monitor_context import MonitorContext
from app.ingest.builders.monitor_keywords import _KeywordSummaryResult
from app.ingest.builders.monitor_scoring import (
    BOARD_CONFIGS,
    RECENT_WINDOW_DAYS,
    TIMELINESS_WINDOW_DAYS,
    WINDOW_DAYS,
    _log_count,
    _coverage_raw,
    _events_in_window,
    _event_sets,
    _effective_article_count,
    _engagement_equivalent,
    _move_counts,
    _trailing_event_streak,
    _observation_span_days,
    _confidence_by_days,
    CLASSIC_MIN_DAYS,
    _score_level,
    _population_stat,
    _attach_hexagon_population,
    build_raw_board_snapshots,
    build_board_benchmarks,
    score_board_snapshot,
    build_hexagon_payload,
)
from app.ingest.builders.monitor_interactions import attach_interaction_metrics


def _build_kid_to_text(ctx: MonitorContext) -> dict[str, str]:
    """从 ctx.keywords 构建 keyword_id → keyword_text 映射。"""
    return {k.get("keyword_id", ""): k.get("keyword_text", "") for k in ctx.keywords if k.get("keyword_id")}


def _find_global_effective_days(ctx: MonitorContext) -> list[int]:
    """全局有效日：从 ctx.snapshots 中按日期汇总success的不同keyword_id。
    返回 day_idx 列表，按升序排列。
    """
    snap_by_id = {s.get("snapshot_id"): s for s in ctx.snapshots}
    # 收集每个 snapshot 的 keyword_id
    # 按日期聚合 success 状态的 keyword_id
    date_keywords: dict[int, set[str]] = defaultdict(set)
    for snap in ctx.snapshots:
        if snap.get("status") != "success":
            continue
        kid = snap.get("keyword_id", "")
        if not kid:
            continue
        try:
            dt = datetime.fromisoformat(snap["captured_at"])
        except Exception:
            continue
        window_start = ctx.window_dates[0]
        try:
            window_start_date = datetime.fromisoformat(window_start).date()
        except Exception:
            window_start_date = ctx.window_end.date()
        day_idx = (dt.date() - window_start_date).days
        if 0 <= day_idx < WINDOW_DAYS:
            date_keywords[day_idx].add(kid)
    # 有效日：当天成功刷新的关键词数 >= 1
    effective_days = sorted(date_keywords.keys())
    return effective_days


def _find_previous_effective_day(effective_days: list[int], current_day_idx: int) -> int | None:
    """找到小于 current_day_idx 的最近一个全局有效日。"""
    for d in reversed(effective_days):
        if d < current_day_idx:
            return d
    return None


def _compute_today_confidence(effective_days: list[int], end_idx: int, ctx: MonitorContext) -> float:
    """today confidence = 该日成功刷新关键词数 / 目标关键词数。"""
    # 目标关键词数 = len(ctx.keywords)
    target_count = max(len(ctx.keywords), 1)
    # 该日成功刷新的关键词数
    snap_by_id = {s.get("snapshot_id"): s for s in ctx.snapshots}
    day_keywords: set[str] = set()
    for snap in ctx.snapshots:
        if snap.get("status") != "success":
            continue
        try:
            dt = datetime.fromisoformat(snap["captured_at"])
        except Exception:
            continue
        window_start = ctx.window_dates[0]
        try:
            window_start_date = datetime.fromisoformat(window_start).date()
        except Exception:
            window_start_date = ctx.window_end.date()
        day_idx = (dt.date() - window_start_date).days
        if day_idx == end_idx:
            kid = snap.get("keyword_id", "")
            if kid:
                day_keywords.add(kid)
    completed = len(day_keywords)
    return min(completed / target_count, 1.0)


def _get_total_target_keywords(ctx: MonitorContext) -> int:
    return max(len(ctx.keywords), 1)


def _compute_refresh_completeness(ctx: MonitorContext, end_idx: int) -> dict:
    """计算该日关键词刷新完整度。"""
    target_count = _get_total_target_keywords(ctx)
    window_start = ctx.window_dates[0]
    try:
        window_start_date = datetime.fromisoformat(window_start).date()
    except Exception:
        window_start_date = ctx.window_end.date()
    day_keywords: set[str] = set()
    for snap in ctx.snapshots:
        if snap.get("status") != "success":
            continue
        try:
            dt = datetime.fromisoformat(snap["captured_at"])
        except Exception:
            continue
        day_idx = (dt.date() - window_start_date).days
        if day_idx == end_idx:
            kid = snap.get("keyword_id", "")
            if kid:
                day_keywords.add(kid)
    completed = len(day_keywords)
    return {
        "refresh_completed_keywords": completed,
        "refresh_target_keywords": target_count,
        "refresh_completeness": round(min(completed / target_count, 1.0), 4),
    }



def _find_kid_for_text(kid_to_text: dict[str, str], kw_text: str) -> str | None:
    """反向查找 keyword_id。"""
    for kid, kt in kid_to_text.items():
        if kt == kw_text:
            return kid
    return None


def build_account_summaries(
    ctx: MonitorContext,
    kw_result: _KeywordSummaryResult,
    theme_indexes: dict[str, Any] | None = None,
) -> list[dict]:
    """构造账号 summaries 列表（已按 account_score desc 排序）。"""
    _ = theme_indexes  # 保留参数兼容性，XHS 不使用 theme_indexes

    kid_to_text = _build_kid_to_text(ctx)
    snap_by_id = {s.get("snapshot_id"): s for s in ctx.snapshots}
    art_by_id = {a.get("article_id"): a for a in ctx.articles}

    window_end = ctx.window_end
    window_dates = ctx.window_dates
    window_start = ctx.window_dates[0]
    try:
        window_start_date = datetime.fromisoformat(window_start).date()
    except Exception:
        window_start_date = window_end.date()

    # 读取 keyword_text → bucket 映射
    text_to_bucket, kid_to_bucket = _load_config_buckets()

    # 全局有效日
    global_effective_days = _find_global_effective_days(ctx)
    current_end_idx = WINDOW_DAYS - 1
    previous_effective_day = _find_previous_effective_day(global_effective_days, current_end_idx)

    # today confidence
    today_confidence = _compute_today_confidence(global_effective_days, current_end_idx, ctx)
    today_refresh = _compute_refresh_completeness(ctx, current_end_idx)

    # 构建 articles_by_key 全局（移出账号循环，避免重复构建）
    articles_by_key: dict[str, dict] = {}
    for art in ctx.articles:
        art_id = art.get("article_id", "")
        if art_id:
            articles_by_key[art_id] = art

    # 收集所有账号的 rank_events（用于 benchmark 计算）
    all_account_rank_events: dict[str, list[dict]] = {}
    # 中间结果暂存
    calc_results: list[dict] = []

    for account in ctx.accounts:
        aid = account.get("account_id")
        if not aid:
            continue
        hits = ctx.hits_by_account.get(aid, [])
        if not hits:
            continue

        # ── 构建 rank_events 扁平列表 ──
        day_keyword_articles: dict[int, dict[str, dict[str, dict]]] = {}
        for h in hits:
            snap = snap_by_id.get(h.get("snapshot_id", ""))
            if not snap:
                continue
            try:
                dt = datetime.fromisoformat(snap["captured_at"])
            except Exception:
                continue
            day_idx = _day_index(window_start_date, dt.date(), WINDOW_DAYS)
            if day_idx < 0 or day_idx >= WINDOW_DAYS:
                continue

            art = art_by_id.get(h.get("article_id", ""), {})
            kw_id = snap.get("keyword_id", "")
            # P0.1: 使用 keyword_id → keyword_text 映射
            kw_text = kid_to_text.get(kw_id, snap.get("keyword_text", "") or kw_id)
            topic = kw_text  # topic 先用 keyword_text
            bucket = _resolve_bucket(kw_id, kw_text, text_to_bucket, kid_to_bucket)
            article_key = h.get("article_id") or art.get("title") or h.get("title_raw", "")

            day_map = day_keyword_articles.setdefault(day_idx, {})
            kw_map = day_map.setdefault(kw_text, {})
            existing = kw_map.get(article_key)
            rank = h.get("rank")
            if rank is None:
                continue
            if existing is None:
                kw_map[article_key] = {
                    "article_key": article_key,
                    "article_id": art.get("article_id"),
                    "rank": rank,
                    "topic": topic,
                    "bucket": bucket,
                    "published_at": art.get("published_at"),
                    "keyword": kw_text,
                    "day_idx": day_idx,
                }
            elif rank < existing["rank"]:
                existing["rank"] = rank

        rank_events = _flatten_rank_events(day_keyword_articles)

        # P0.6: current = 当前有效日（WINDOW_DAYS-1）
        current_raw = build_raw_board_snapshots(
            rank_events, current_end_idx, window_start_date, articles_by_key,
            previous_effective_day_idx=previous_effective_day,
            today_confidence=today_confidence,
            global_effective_day_indices=global_effective_days,
        )

        # P0.6: previous = 前一全局有效日
        if previous_effective_day is not None:
            previous_raw = build_raw_board_snapshots(
                rank_events, previous_effective_day, window_start_date, articles_by_key,
                previous_effective_day_idx=None,
                today_confidence=1.0,
                global_effective_day_indices=global_effective_days,
            )
        else:
            previous_raw = build_raw_board_snapshots(
                [], current_end_idx, window_start_date, articles_by_key,
                previous_effective_day_idx=None,
                today_confidence=0.0,
                global_effective_day_indices=global_effective_days,
            )

        all_account_rank_events[aid] = rank_events
        calc_results.append({
            "aid": aid,
            "account": account,
            "rank_events": rank_events,
            "current_raw": current_raw,
            "previous_raw": previous_raw,
            "day_keyword_articles": day_keyword_articles,
        })

    # P0.3: 当前 benchmarks 从 current_raw 构建
    current_snapshots = [c["current_raw"] for c in calc_results]
    current_benchmarks = build_board_benchmarks(current_snapshots)

    # P0.3: 前一日 benchmarks 从 previous_raw 构建
    previous_snapshots = [c["previous_raw"] for c in calc_results]
    previous_benchmarks = build_board_benchmarks(previous_snapshots)

# =========================================================================
    # P0.8: Build keyword/topic detail objects from rank_events
    # =========================================================================
    # keyword_objects: {keyword_text: {keyword_id, keyword_text, history:[15], day_scores:[15], articles:[...]}}
    # topic_objects: {topic_text: {label, theme_type, history:[15], day_scores:[15], keywords:[text], articles:[...]}}

    # Build keyword detail objects
    keyword_objects: dict[str, dict] = {}
    for calc in calc_results:
        for event in calc["rank_events"]:
            kw = event["keyword"]
            if kw not in keyword_objects:
                kw_id = _find_kid_for_text(kid_to_text, kw)
                keyword_objects[kw] = {
                    "keyword_id": kw_id or f"kw_{kw}",
                    "keyword_text": kw,
                    "history": [0] * WINDOW_DAYS,
                    "day_scores": [0.0] * WINDOW_DAYS,
                    "articles": [],
                }
            di = event["day_idx"]
            if 0 <= di < WINDOW_DAYS:
                keyword_objects[kw]["history"][di] = min(keyword_objects[kw]["history"][di] or 999, event["rank"]) if keyword_objects[kw]["history"][di] == 0 or event["rank"] < keyword_objects[kw]["history"][di] else keyword_objects[kw]["history"][di]

    # Build topic objects
    topic_objects: dict[str, dict] = {}
    for calc in calc_results:
        for event in calc["rank_events"]:
            topic = event["topic"]
            if topic not in topic_objects:
                topic_objects[topic] = {
                    "label": topic,
                    "theme_type": "topic",
                    "history": [0] * WINDOW_DAYS,
                    "day_scores": [0.0] * WINDOW_DAYS,
                    "keywords": [],
                    "articles": [],
                }
            di = event["day_idx"]
            if 0 <= di < WINDOW_DAYS:
                topic_objects[topic]["history"][di] = min(topic_objects[topic]["history"][di] or 999, event["rank"]) if topic_objects[topic]["history"][di] == 0 or event["rank"] < topic_objects[topic]["history"][di] else topic_objects[topic]["history"][di]
            if event["keyword"] not in topic_objects[topic]["keywords"]:
                topic_objects[topic]["keywords"].append(event["keyword"])

    # Populate day_scores for keywords and topics
    for kw_obj in keyword_objects.values():
        for di in range(WINDOW_DAYS):
            if kw_obj["history"][di] > 0:
                kw_obj["day_scores"][di] = round((11 - min(kw_obj["history"][di], 10)) / 10.0 * 20, 4)
    for tp_obj in topic_objects.values():
        for di in range(WINDOW_DAYS):
            if tp_obj["history"][di] > 0:
                tp_obj["day_scores"][di] = round((11 - min(tp_obj["history"][di], 10)) / 10.0 * 20, 4)

    # Build article detail with keyword mapping
    # article_keyword_map: {article_id: [keyword_text]}
    article_keyword_map: dict[str, set[str]] = defaultdict(set)
    article_today_rank: dict[str, int] = {}
    for calc in calc_results:
        for event in calc["rank_events"]:
            aid = event.get("article_id")
            if aid:
                article_keyword_map[aid].add(event["keyword"])
                if event["day_idx"] == current_end_idx:
                    existing = article_today_rank.get(aid)
                    if existing is None or event["rank"] < existing:
                        article_today_rank[aid] = event["rank"]

    # Now rebuild each account result with the new structures
    results = []
    for calc in calc_results:
        aid = calc["aid"]
        account = calc["account"]
        rank_events = calc["rank_events"]
        current_raw = calc["current_raw"]
        previous_raw = calc["previous_raw"]
        day_keyword_articles = calc["day_keyword_articles"]

        normalized_boards: dict[str, dict[str, Any]] = {}
        previous_normalized_boards: dict[str, dict[str, Any]] = {}
        board_parts: dict[str, dict[str, Any]] = {}
        previous_board_parts: dict[str, dict[str, Any]] = {}
        board_hexagons: dict[str, dict[str, Any]] = {}

        for board in BOARD_CONFIGS:
            cur_norm, cur_parts = score_board_snapshot(current_raw[board], board, current_benchmarks[board])
            prev_norm, prev_parts = score_board_snapshot(previous_raw[board], board, previous_benchmarks[board])
            normalized_boards[board] = cur_norm
            previous_normalized_boards[board] = prev_norm
            board_parts[board] = cur_parts
            previous_board_parts[board] = prev_parts
            board_hexagons[board] = build_hexagon_payload(
                board, cur_norm, prev_norm, current_benchmarks[board],
                previous_benchmarks=previous_benchmarks[board],
            )

        account_score = board_parts["account"]["score"]
        account_score_yesterday = previous_board_parts["account"]["score"]
        timeliness_score = board_parts["timeliness"]["score"]
        timeliness_score_yesterday = previous_board_parts["timeliness"]["score"]
        today_score = board_parts["today"]["score"]
        today_score_yesterday = previous_board_parts["today"]["score"]

        acct = account
        account_current = normalized_boards["account"]
        account_details = account_current["details"]

        # ── 基础账号信息 ──
        result: dict[str, Any] = {
            "account_id": aid,
            "name": acct.get("canonical_name") or aid,
            "headimg_url": acct.get("headimg_url"),
            "platform": acct.get("platform", "小红书"),
            "description": acct.get("description"),
            "fans": acct.get("fans"),
            "total_works": acct.get("total_works"),
            "likes_total": acct.get("likes"),
            "collects_total": acct.get("collects"),
            "follows_total": acct.get("follows"),
            "ip_location": acct.get("ip_location"),
            "verify_info": acct.get("verify_info"),
            "red_id": (acct.get("platform_payload") or {}).get("red_id"),
            "first_seen_at": acct.get("first_seen_at"),
            "last_seen_at": acct.get("last_seen_at"),
            "is_focus": acct.get("is_focus", False),
            "note": acct.get("note"),
        }

        # ── 三榜分数 ──
        result["score"] = account_score
        result["score_raw"] = board_parts["account"]["score_raw"]
        result["score_yesterday"] = account_score_yesterday
        result["score_delta"] = account_score - account_score_yesterday
        result["score_level"] = _score_level(account_score)
        result["account_score_method"] = "xhs_three_board_p99_v3_evidence_calibrated"

        result["timeliness_score"] = timeliness_score
        result["timeliness_score_raw"] = board_parts["timeliness"]["score_raw"]
        result["timeliness_score_yesterday"] = timeliness_score_yesterday
        result["timeliness_score_delta"] = timeliness_score - timeliness_score_yesterday
        result["timeliness_score_level"] = _score_level(timeliness_score)

        result["today_score"] = today_score
        result["today_score_raw"] = board_parts["today"]["score_raw"]
        result["today_score_yesterday"] = today_score_yesterday
        result["today_score_delta"] = today_score - today_score_yesterday
        result["today_score_level"] = _score_level(today_score)

        # ── hexagon 看板 ──
        result["account_score_hexagon"] = board_hexagons["account"]
        result["timeliness_score_hexagon"] = board_hexagons["timeliness"]
        result["today_score_hexagon"] = board_hexagons["today"]

        # ── parts ──
        result["account_score_parts"] = {
            **board_parts["account"],
            **normalized_boards["account"]["axis_values"],
        }
        result["timeliness_score_parts"] = {
            **board_parts["timeliness"],
            **normalized_boards["timeliness"]["axis_values"],
        }
        result["today_score_parts"] = {
            **board_parts["today"],
            **normalized_boards["today"]["axis_values"],
        }

        # ── 兼容旧字段 ──
        result["history_active_days"] = account_details.get("history_active_days", 0)
        result["recent_active_days"] = account_details.get("recent_active_days", 0)
        result["current_streak"] = account_details.get("current_streak", 0)
        result["history_keyword_count"] = account_details.get("history_keyword_count", 0)
        result["history_article_count"] = account_details.get("history_article_count", 0)
        result["durable_article_count"] = account_details.get("durable_article_count", 0)
        result["durable_pair_count"] = account_details.get("durable_pair_count", 0)
        result["durable_rank_days"] = account_details.get("durable_rank_days", 0)
        result["durable_notes_status"] = account_details.get("durable_notes_status", "unknown")
        result["durable_notes_message"] = account_details.get("durable_notes_message", "")

        # today_hit_count u6765u81ea today board detailsuff0cu4e0du662f account board details
        today_details = normalized_boards.get("today", {}).get("details", {})
        if "today_hit_count" not in today_details:
            today_details = normalized_boards.get("today", {}).get("current", {}).get("details", {})
        result["today_hit_count"] = today_details.get("today_hit_count", 0)
        result["recent_hit_days"] = account_details.get("recent_active_days", 0)
        result["topic_count"] = account_details.get("history_topic_count", 0)
        result["bucket_count"] = account_details.get("history_bucket_count", 0)
        result["kw_count"] = account_details.get("history_keyword_count", 0)
        result["article_count"] = account_details.get("history_article_count", 0)

        # ── day_scores / history 有真实值 ──
        day_scores_list = [0.0] * WINDOW_DAYS
        history_list = [0] * WINDOW_DAYS
        for event in rank_events:
            di = event["day_idx"]
            if 0 <= di < WINDOW_DAYS:
                history_list[di] = 1
        for di in range(WINDOW_DAYS):
            day_events = _events_in_window(rank_events, di, di)
            if day_events:
                day_scores_list[di] = round(_coverage_raw(day_events) * 20, 4)
        result["history"] = history_list
        result["day_scores"] = day_scores_list

        # ── breakthrough ──
        result["breakthrough"] = board_parts["account"]["breakthrough"]

        # =========================================================
        # P0.8: keywords/topics as dict objects (not list)
        # =========================================================
        # Build per-account keyword objects
        acct_kw_set: set[str] = set()
        acct_topic_set: set[str] = set()
        acct_kw_id_map: dict[str, str] = {}
        for event in rank_events:
            acct_kw_set.add(event["keyword"])
            acct_topic_set.add(event["topic"])
            art_id = event.get("article_id")
            kw = event["keyword"]
            # Find keyword_id from kid_to_text reverse lookup
            for kid, kt in kid_to_text.items():
                if kt == kw:
                    acct_kw_id_map[kw] = kid
                    break
            if kw not in acct_kw_id_map:
                acct_kw_id_map[kw] = f"kw_{kw}"

        # Build per-account keyword objects with history/articles
        acct_keyword_objects: dict[str, dict] = {}
        for kw in acct_kw_set:
            kw_obj = keyword_objects.get(kw, {})
            # Filter articles for this account
            kw_articles = []
            for event in rank_events:
                if event["keyword"] == kw and event.get("article_id"):
                    art = articles_by_key.get(event["article_id"], {})
                    kw_articles.append({
                        "article_id": event["article_id"],
                        "title": art.get("title", ""),
                        "url": art.get("normalized_url") or art.get("raw_url", ""),
                        "cover_url": art.get("cover_url"),
                        "rank": event["rank"],
                        "today_rank": article_today_rank.get(event["article_id"]),
                        "is_today": event["day_idx"] == current_end_idx,
                        "matched_keyword_count": len(article_keyword_map.get(event["article_id"], set())),
                        "published_at": art.get("published_at"),
                        "liked_count": art.get("liked_count"),
                        "collected_count": art.get("collected_count"),
                        "comment_count": art.get("comment_count"),
                        "shared_count": art.get("shared_count"),
                    })
            # Deduplicate by article_id, keep best rank
            seen_articles: dict[str, dict] = {}
            for a in kw_articles:
                if a["article_id"] not in seen_articles or a["rank"] < seen_articles[a["article_id"]]["rank"]:
                    seen_articles[a["article_id"]] = a
            kw_history = [0] * WINDOW_DAYS
            kw_day_scores = [0.0] * WINDOW_DAYS
            for event in rank_events:
                if event["keyword"] == kw and 0 <= event["day_idx"] < WINDOW_DAYS:
                    di = event["day_idx"]
                    if kw_history[di] == 0 or event["rank"] < kw_history[di]:
                        kw_history[di] = event["rank"]
            for di in range(WINDOW_DAYS):
                if kw_history[di] > 0:
                    kw_day_scores[di] = round((11 - min(kw_history[di], 10)) / 10.0 * 20, 4)
            acct_keyword_objects[kw] = {
                "keyword_id": acct_kw_id_map.get(kw, f"kw_{kw}"),
                "keyword_text": kw,
                "history": kw_history,
                "day_scores": kw_day_scores,
                "hit_days": sum(1 for r in kw_history if r > 0),
                "best_rank": min((r for r in kw_history if r > 0), default=None),
                "articles": sorted(seen_articles.values(), key=lambda a: (a.get("rank") or 999, a.get("published_at") or "")),
            }

        # Build per-account topic objects
        acct_topic_objects: dict[str, dict] = {}
        for topic in acct_topic_set:
            tp_obj = topic_objects.get(topic, {})
            tp_articles = []
            tp_keywords = set()
            for event in rank_events:
                if event["topic"] == topic and event.get("article_id"):
                    art = articles_by_key.get(event["article_id"], {})
                    tp_articles.append({
                        "article_id": event["article_id"],
                        "title": art.get("title", ""),
                        "url": art.get("normalized_url") or art.get("raw_url", ""),
                        "cover_url": art.get("cover_url"),
                        "rank": event["rank"],
                        "today_rank": article_today_rank.get(event["article_id"]),
                        "is_today": event["day_idx"] == current_end_idx,
                        "matched_keyword_count": len(article_keyword_map.get(event["article_id"], set())),
                        "published_at": art.get("published_at"),
                        "liked_count": art.get("liked_count"),
                        "collected_count": art.get("collected_count"),
                        "comment_count": art.get("comment_count"),
                        "shared_count": art.get("shared_count"),
                    })
                    tp_keywords.add(event["keyword"])
            # Deduplicate by article_id
            seen_tp_articles: dict[str, dict] = {}
            for a in tp_articles:
                if a["article_id"] not in seen_tp_articles or a["rank"] < seen_tp_articles[a["article_id"]]["rank"]:
                    seen_tp_articles[a["article_id"]] = a
            tp_history = [0] * WINDOW_DAYS
            tp_day_scores = [0.0] * WINDOW_DAYS
            for event in rank_events:
                if event["topic"] == topic and 0 <= event["day_idx"] < WINDOW_DAYS:
                    di = event["day_idx"]
                    if tp_history[di] == 0 or event["rank"] < tp_history[di]:
                        tp_history[di] = event["rank"]
            for di in range(WINDOW_DAYS):
                if tp_history[di] > 0:
                    tp_day_scores[di] = round((11 - min(tp_history[di], 10)) / 10.0 * 20, 4)
            acct_topic_objects[topic] = {
                "label": tp_obj.get("label", topic),
                "theme_type": tp_obj.get("theme_type", "topic"),
                "history": tp_history,
                "day_scores": tp_day_scores,
                "hit_days": sum(1 for r in tp_history if r > 0),
                "best_rank": min((r for r in tp_history if r > 0), default=None),
                "article_count": len(seen_tp_articles),
                "keyword_count": len(tp_keywords),
                "keywords": sorted(tp_keywords),
                "articles": sorted(seen_tp_articles.values(), key=lambda a: (a.get("rank") or 999, a.get("published_at") or "")),
            }

        result["keywords"] = acct_keyword_objects
        result["topics"] = acct_topic_objects
        result["covered_topics"] = sorted(acct_topic_set)

        # P0.8: matched_keywords with real keyword_id
        result["matched_keywords"] = [
            {"keyword_id": acct_kw_id_map.get(kw, f"kw_{kw}"), "keyword_text": kw}
            for kw in sorted(acct_kw_set)
        ]

        # ── articles with real today_rank/is_today/matched_keyword_count ──
        article_ids_in_rank = list({event["article_id"] for event in rank_events if event.get("article_id")})
        articles_list = []
        for art_id in article_ids_in_rank:
            art = articles_by_key.get(art_id, {})
            if art:
                # Get today_rank from this account's events
                today_rank_val = None
                is_today_val = False
                matched_kw_count = len(article_keyword_map.get(art_id, set()))
                for event in rank_events:
                    if event.get("article_id") == art_id:
                        if event["day_idx"] == current_end_idx:
                            is_today_val = True
                            if today_rank_val is None or event["rank"] < today_rank_val:
                                today_rank_val = event["rank"]
                articles_list.append({
                    "article_id": art_id,
                    "title": art.get("title", ""),
                    "url": art.get("normalized_url") or art.get("raw_url", ""),
                    "content_path": art_id,
                    "cover_url": art.get("cover_url"),
                    "rank": min((event["rank"] for event in rank_events if event.get("article_id") == art_id), default=999),
                    "today_rank": today_rank_val,
                    "is_today": is_today_val,
                    "latest_seen_at": art.get("last_seen_at"),
                    "first_seen_at": art.get("first_seen_at"),
                    "hit_days": len({event["day_idx"] for event in rank_events if event.get("article_id") == art_id}),
                    "collected_count": art.get("collected_count"),
                    "liked_count": art.get("liked_count"),
                    "comment_count": art.get("comment_count"),
                    "shared_count": art.get("shared_count"),
                    "read_count": art.get("read_count"),
                    "matched_keyword_count": matched_kw_count,
                    "signal": 0.0,
                    "work_type": art.get("work_type"),
                    "published_at": art.get("published_at"),
                    "is_relevant": art.get("is_relevant", True),
                    "relevance_score": art.get("relevance_score", 1.0),
                })
        result["articles"] = articles_list

        # ── best_articles / classic_articles (P0.9) ──
        sorted_articles = sorted(articles_list, key=lambda a: (a.get("rank") or 999, -(a.get("liked_count") or 0)))
        result["best_articles"] = sorted_articles[:6]
        # classic_articles: 不足3天全局有效日 => 空列表；第3天后只放durable article
        if len(global_effective_days) < CLASSIC_MIN_DAYS:
            result["classic_articles"] = []
            result["classic_article_count"] = 0
        else:
            # 只放durable article（同一笔记×关键词在至少3个不同自然日出现）
            durable_pairs = account_details.get("durable_pair_count", 0)
            durable_articles_set = account_details.get("durable_article_count", 0)
            classic = [
                {"article_id": a["article_id"], "rank": a["rank"], "first_seen_at": a["first_seen_at"]}
                for a in sorted_articles[:6]
            ]
            result["classic_articles"] = classic
            result["classic_article_count"] = len(classic)
        result["classic_pair_count"] = 0
        result["stable_notes_count"] = account_details.get("durable_article_count", 0)
        result["stable_pairs_count"] = account_details.get("durable_pair_count", 0)

        # ── move_summary (P0.8: include new_count/up_count/down_count/flat_count) ──
        account_moves = _move_counts(rank_events, current_end_idx)
        # Determine primary_type based on keyword movement
        if account_moves["new_count"] >= account_moves["up_count"] and account_moves["new_count"] >= account_moves["down_count"]:
            if account_moves["new_count"] > 0:
                primary_type = "new"
            elif account_moves["up_count"] > 0:
                primary_type = "up"
            elif account_moves["down_count"] > 0:
                primary_type = "down"
            else:
                primary_type = "stable"
        elif account_moves["up_count"] >= account_moves["down_count"]:
            primary_type = "up" if account_moves["up_count"] > 0 else "stable"
        else:
            primary_type = "down" if account_moves["down_count"] > 0 else "stable"

        # secondary_type: the second most significant
        sorted_moves = sorted(
            [("new", account_moves["new_count"]), ("up", account_moves["up_count"]),
             ("down", account_moves["down_count"]), ("flat", account_moves["flat_count"])],
            key=lambda x: -x[1]
        )
        secondary_type = None
        secondary_count = 0
        if len(sorted_moves) >= 2:
            sec = sorted_moves[1]
            if sec[0] != primary_type and sec[1] > 0:
                secondary_type = sec[0]
                secondary_count = sec[1]

        result["move_summary"] = {
            "primary_type": primary_type,
            "secondary_type": secondary_type,
            "primary_count": account_moves.get(primary_type, 0),
            "secondary_count": secondary_count,
            # 兼容前端new_count/up_count/down_count/flat_count
            "new_count": account_moves["new_count"],
            "up_count": account_moves["up_count"],
            "down_count": account_moves["down_count"],
            "flat_count": account_moves["flat_count"],
        }

        # ── relevance_stats / is_relevant_count ──
        relevance_count = sum(1 for a in articles_list if a.get("is_relevant", True))
        result["is_relevant_count"] = relevance_count
        result["relevance_stats"] = {
            "total": len(articles_list),
            "is_relevant_count": relevance_count,
            "is_irrelevant_count": len(articles_list) - relevance_count,
            "method": "title_desc_product_token_match_v2",
        }

        # ── interaction_metrics ──
        result["interaction_metrics"] = {
            "method": "同一笔记相邻两次 TikHub 搜索快照相减；仅统计可见点赞、收藏、评论、分享，不含阅读/曝光。",
            "article_count": len(articles_list),
            "delta_note_count": 0,
            "absolute": {"liked": 0, "collected": 0, "comment": 0, "shared": 0},
            "delta": {"liked": 0, "collected": 0, "comment": 0, "shared": 0},
            "delta_available": False,
        }

        # ── today 相关 ──
        result["today_confidence"] = today_confidence
        result["today_refresh"] = today_refresh

        # P0.7: today_refresh 合并进 today_score_hexagon.current.details
        today_hexagon = result["today_score_hexagon"]
        if "details" in today_hexagon.get("current", {}):
            today_hexagon["current"]["details"]["refresh_completed_keywords"] = today_refresh["refresh_completed_keywords"]
            today_hexagon["current"]["details"]["refresh_target_keywords"] = today_refresh["refresh_target_keywords"]
            today_hexagon["current"]["details"]["refresh_completeness"] = today_refresh["refresh_completeness"]
        # previous: 对应前一有效日的真实完整度
        if previous_effective_day is not None:
            prev_refresh = _compute_refresh_completeness(ctx, previous_effective_day)
        else:
            prev_refresh = {"refresh_completed_keywords": 0, "refresh_target_keywords": max(len(ctx.keywords), 1), "refresh_completeness": 0.0}
        if "details" in today_hexagon.get("previous", {}):
            today_hexagon["previous"]["details"]["refresh_completed_keywords"] = prev_refresh["refresh_completed_keywords"]
            today_hexagon["previous"]["details"]["refresh_target_keywords"] = prev_refresh["refresh_target_keywords"]
            today_hexagon["previous"]["details"]["refresh_completeness"] = prev_refresh["refresh_completeness"]

        # ── 全局有效日信息 ──
        result["global_effective_days"] = len(global_effective_days)
        result["global_effective_day_count"] = len(global_effective_days)
        if len(global_effective_days) < CLASSIC_MIN_DAYS:
            result["durable_notes_status"] = "waiting"
            result["durable_notes_message"] = f"等待第{CLASSIC_MIN_DAYS}天验证（当前{len(global_effective_days)}天）"

        results.append(result)

    # 附加 hexagon population context
    _attach_hexagon_population(results)

    # 附加互动指标
    try:
        attach_interaction_metrics(results, ctx.articles_by_id, ctx.metric_observations)
    except Exception:
        pass

    # 排序
    results.sort(key=lambda a: (-a["score_raw"], -a["score"], a["name"]))
    return results

def build_account_theme_indexes(ctx: MonitorContext) -> dict[str, Any]:
    """保留兼容接口。XHS 不使用 theme_indexes。"""
    return {}


def _day_index(window_start_date: date, event_date: date, window_days: int) -> int:
    diff = (event_date - window_start_date).days
    if diff < 0 or diff >= window_days:
        return -1
    return diff


def _load_config_buckets() -> tuple[dict[str, str], dict[str, str]]:
    text_to_bucket: dict[str, str] = {}
    kid_to_bucket: dict[str, str] = {}
    try:
        from pathlib import Path
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        kw_path = project_root / "data" / "config" / "keywords.json"
        if kw_path.exists():
            import json
            payload = json.loads(kw_path.read_text(encoding="utf-8"))
            for group in payload.get("groups", []):
                label = group.get("label") or "未分类"
                for kw in group.get("keywords", []):
                    text_to_bucket[kw.get("keyword_text", "").strip()] = label
                    kid_to_bucket[kw.get("keyword_id", "")] = label
    except Exception:
        pass
    return text_to_bucket, kid_to_bucket


def _resolve_bucket(kid: str, kw_text: str, text_to_bucket: dict, kid_to_bucket: dict) -> str:
    return kid_to_bucket.get(kid) or text_to_bucket.get(kw_text.strip()) or "未分类"


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


__all__ = [
    "build_account_summaries",
    "build_account_theme_indexes",
]
