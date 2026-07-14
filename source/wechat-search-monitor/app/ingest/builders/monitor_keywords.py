"""monitor_keywords.py — 关键词维度汇总。

把原 `build_monitor_data()` 中的关键词大循环（生成 runs、history、accounts、
heat_summary、kw_score、kw_summaries 排序）抽离为独立函数。

输入：`MonitorBuildContext` 和预计算好的 `HeatContext`。
输出：`KeywordSummaryResult` dataclass，包含：
  - `keywords`：最终 kw_summaries 列表（已按 total desc 排序）
  - `acct_kw_history` / `acct_articles`：账号聚合阶段需要消费的中间结构
  - `kw_latest_ranks_map` / `kw_prev_ranks_map`：今日/昨日 rank map
  - `today_titles_by_acct`：今日命中文章标题集合
  - `max_dso` / `max_acct` / `max_art`：归一化系数

行为契约：与原 builder 完全一致，不修改任何字段语义或排序规则。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.ingest.builders.monitor_context import MonitorBuildContext
from app.ingest.builders.monitor_heat import (
    HeatContext,
    _compute_kw_score,
    heat_summary_for,
)
from app.ingest.builders.monitor_scoring import (
    WINDOW_DAYS,
    display_day_count,
    rank_weight,
)


@dataclass
class KeywordSummaryResult:
    keywords: list[dict] = field(default_factory=list)
    acct_kw_history: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    acct_articles: dict[str, dict[str, dict[str, dict]]] = field(default_factory=dict)
    kw_latest_ranks_map: dict[str, dict[str, int]] = field(default_factory=dict)
    kw_prev_ranks_map: dict[str, dict[str, int]] = field(default_factory=dict)
    today_titles_by_acct: dict[str, set[str]] = field(default_factory=dict)
    max_dso: int = 0
    max_acct: int = 1
    max_art: int = 1


def build_keyword_summaries(
    ctx: MonitorBuildContext,
    heat: HeatContext,
) -> KeywordSummaryResult:
    """生成关键词维度的所有汇总数据。

    行为完全与原 builder 第 177-358 行一致：
    1. 对每个关键词：
       - 计算 15 天内每个账号的最优 rank 矩阵
       - 聚合每日的 day_scores / history_best / history_hits
       - 构造 runs（含文章列表 + terms 列表）
       - 计算 latest_run / today_best / today_count
       - 计算前一次主快照的 prev_ranks
       - 构造 account_lens（每个关键词下，账号的命中汇总）
    2. 注入 heat_summary
    3. 注入 kw_score（依赖全量 max_dso / max_acct / max_art）
    4. 按 kw_score.total 倒排
    """
    result = KeywordSummaryResult(
        acct_kw_history=defaultdict(dict),
        acct_articles=defaultdict(lambda: defaultdict(dict)),
        today_titles_by_acct=defaultdict(set),
    )
    acct_kw_history = result.acct_kw_history
    acct_articles = result.acct_articles
    kw_latest_ranks_map: dict[str, dict[str, int]] = {}
    kw_prev_ranks_map: dict[str, dict[str, int]] = {}
    today_titles_by_acct: dict[str, set[str]] = result.today_titles_by_acct

    kw_by_id = ctx.kw_by_id
    acct_by_id = ctx.acct_by_id
    art_by_id = ctx.art_by_id
    kw_topic_by_id = ctx.kw_topic_by_id
    kw_bucket_by_id = ctx.kw_bucket_by_id
    snaps_by_kw = ctx.snaps_by_kw
    hits_by_snap = ctx.hits_by_snap
    snapshot_terms = ctx.snapshot_terms
    ent = ctx.ent

    kw_summaries: list[dict] = []

    for kid, kw in kw_by_id.items():
        per_acct = ctx.keyword_account_history(kid)
        day_scores = [0.0] * WINDOW_DAYS
        history_best = [0] * WINDOW_DAYS
        history_hits = [0] * WINDOW_DAYS
        for aid, hist in per_acct.items():
            for i, r in enumerate(hist):
                if r > 0:
                    day_scores[i] += rank_weight(r)
                    history_hits[i] += 1
                    if history_best[i] == 0 or r < history_best[i]:
                        history_best[i] = r

        for aid, hist in per_acct.items():
            acct_kw_history[aid][kw["keyword_text"]] = hist

        runs_out = []
        for s in snaps_by_kw.get(kid, []):
            articles_in_run = []
            for h in hits_by_snap.get(s["snapshot_id"], []):
                art = art_by_id.get(h["article_id"], {})
                aid = h["account_id"]
                hist = per_acct.get(aid, [0] * WINDOW_DAYS)
                hit_days = display_day_count(hist, is_currently_visible=True)
                pub = art.get("published_at")
                pub_short = pub[2:10].replace("-", "/") if pub else ""
                articles_in_run.append({
                    "rank": h["rank"],
                    "account": acct_by_id[aid]["canonical_name"],
                    "account_id": aid,
                    "account_headimg": acct_by_id[aid].get("headimg_url") or None,
                    "article_id": art.get("article_id"),
                    "title": art.get("title", h["title_raw"]),
                    "summary": art.get("summary") or h.get("summary_raw"),
                    "published_at": pub_short,
                    "url": art.get("raw_url", h["url_raw"]),
                    "cover_url": art.get("cover_url"),
                    "content_path": art.get("content_file_path"),
                    "hit_days": hit_days,
                    "read_count": art.get("read_count"),
                    "like_count": art.get("like_count"),
                    "friends_follow_count": art.get("friends_follow_count"),
                    "original_article_count": art.get("original_article_count"),
                })

                acct_bucket = acct_articles[aid][kw["keyword_text"]]
                bucket_key = art.get("article_id") or h["title_raw"]
                if bucket_key not in acct_bucket:
                    acct_bucket[bucket_key] = {
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
                elif h["rank"] < acct_bucket[bucket_key]["rank"]:
                    acct_bucket[bucket_key]["rank"] = h["rank"]

            run_terms = {"suggestions": [], "related": []}
            for t in snapshot_terms:
                if t["snapshot_id"] == s["snapshot_id"]:
                    bucket = "suggestions" if t["term_type"] == "suggestion" else "related"
                    run_terms[bucket].append(t["term_text"])

            runs_out.append({
                "id": s["snapshot_id"],
                "date": s["snapshot_date"],
                "time": s["snapshot_time"],
                "run_at": f"{s['snapshot_date']} {s['snapshot_time']}",
                "trigger_type": s["trigger_type"],
                "is_primary": s["is_primary"],
                "result_count": s["result_count"],
                "note": "",
                "articles": articles_in_run,
                "terms": run_terms,
            })

        latest_run = runs_out[0] if runs_out else None
        today_best = latest_run["articles"][0]["rank"] if latest_run and latest_run["articles"] else None
        today_count = latest_run["result_count"] if latest_run else 0

        latest_run_day = datetime.fromisoformat(latest_run["date"]).date() if latest_run else None
        prev_snap = ctx.previous_primary_snap_before(kid, latest_run_day) if latest_run_day else None
        prev_ranks: dict[str, int] = {}
        if prev_snap:
            for h in hits_by_snap.get(prev_snap["snapshot_id"], []):
                prev_ranks[h["account_id"]] = h["rank"]

        latest_snap_ranks: dict[str, int] = {}
        if latest_run:
            for art in latest_run["articles"]:
                aid = art["account_id"]
                if aid not in latest_snap_ranks or art["rank"] < latest_snap_ranks[aid]:
                    latest_snap_ranks[aid] = art["rank"]

        kw_latest_ranks_map[kw["keyword_text"]] = latest_snap_ranks
        kw_prev_ranks_map[kw["keyword_text"]] = prev_ranks

        if latest_run:
            for art in latest_run["articles"]:
                today_titles_by_acct[art["account_id"]].add(art["title"])

        account_lens = []
        for aid, hist in per_acct.items():
            ranks_in_hist = [r for r in hist if r > 0]
            score = round(sum(rank_weight(r) for r in hist), 2)
            account_lens.append({
                "name": acct_by_id[aid]["canonical_name"],
                "account_id": aid,
                "headimg_url": acct_by_id[aid].get("headimg_url") or None,
                "today_rank": latest_snap_ranks.get(aid),
                "today_prev": prev_ranks.get(aid),
                "score": score,
                "hit_days": display_day_count(hist, is_currently_visible=aid in latest_snap_ranks),
                "best_rank": min(ranks_in_hist) if ranks_in_hist else None,
                "article_count": len(acct_articles[aid].get(kw["keyword_text"], {})),
                "history": hist,
            })
        account_lens.sort(key=lambda a: (0 if a["today_rank"] else 1, a["today_rank"] or 9999, -a["score"]))

        unique_accounts = set(per_acct.keys())
        unique_articles = set()
        for s in snaps_by_kw.get(kid, []):
            for h in hits_by_snap.get(s["snapshot_id"], []):
                unique_articles.add(h["article_id"])

        coverage_days = sum(1 for r in history_best if r > 0)
        tracked_accounts = len(unique_accounts)
        article_count = len(unique_articles)

        kw_summaries.append({
            "keyword": kw["keyword_text"],
            "keyword_id": kid,
            "topic": kw_topic_by_id[kid],
            "keyword_bucket": kw_bucket_by_id[kid],
            "today_best": today_best,
            "today_count": today_count,
            "coverage_days": coverage_days,
            "tracked_accounts": tracked_accounts,
            "article_count": article_count,
            "latest_run": latest_run,
            "runs": runs_out,
            "history_best": history_best,
            "history_hits": history_hits,
            "accounts": account_lens,
            "heat_summary": heat_summary_for(kw["keyword_text"], heat),
        })

    # ── 热度·广度·丰度 评分 ──
    max_wso = heat.max_wso_with_est
    max_dso = max(
        (hs.get("dso", {}).get("month_cover_count", 0) for k in kw_summaries if (hs := k["heat_summary"]).get("dso")),
        default=0,
    )
    max_acct = max((k["tracked_accounts"] for k in kw_summaries), default=1)
    max_art = max((k["article_count"] for k in kw_summaries), default=1)

    for k in kw_summaries:
        k["kw_score"] = _compute_kw_score(
            k["heat_summary"], k["tracked_accounts"], k["article_count"],
            max_wso, max_dso, max_acct, max_art,
        )

    kw_summaries.sort(
        key=lambda k: (
            -k["kw_score"]["total"],
            k["keyword"],
        )
    )

    result.keywords = kw_summaries
    result.kw_latest_ranks_map = kw_latest_ranks_map
    result.kw_prev_ranks_map = kw_prev_ranks_map
    result.max_dso = max_dso
    result.max_acct = max_acct
    result.max_art = max_art
    return result


__all__ = ["KeywordSummaryResult", "build_keyword_summaries"]
