"""Monitor keyword summaries — 每个关键词的完整派生指标。

**前端契约（monitor.js renderKeywordDetail 必需字段）**：
- keyword / keyword_id / topic / keyword_bucket / is_pinned / pin_order / note / enabled
- today_best / today_count / coverage_days / tracked_accounts / article_count
- latest_run { run_at, run_at_short, date, time, trigger_type, status, result_count }
- runs[]              ← 去重后的快照，每个 (keyword, 5min) 窗口一个
    - id, date, captured_at, is_primary, trigger_type, status
    - result_count, best_rank, top3_count, top10_count
    - articles[]       ← 完整笔记详情
    - terms { suggestions[], related[] }
    - dedup_merged_count  ← 合并的原始快照数
- accounts[]
- kw_score { heat, richness, breadth, total, has_heat }
- keyword_read_delta (XHS 无阅读代理 → insufficient_data)
- history_best / history_hits (15 天序列)
- is_relevant_count / relevance_stats
- keyword_heat_metric (可见互动热度派生模型)
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.ingest.builders.monitor_scoring import WINDOW_DAYS
from app.ingest.builders.monitor_heat import HeatContext
from app.ingest.builders.monitor_keyword_heat import build_keyword_heat_metric
from app.ingest.builders.monitor_context import MonitorContext
from app.ingest.common import parse_captured_at_iso


@dataclass
class _KeywordSummaryResult:
    keywords: list[dict]


def _safe_dt(s):
    if not s:
        return None
    try:
        return parse_captured_at_iso(s)
    except Exception:
        return None


def _load_config_buckets() -> tuple[dict[str, str], dict[str, str]]:
    """从 data/config/keywords.json 读 text/group_id → bucket 映射。"""
    text_to_bucket: dict[str, str] = {}
    kid_to_bucket: dict[str, str] = {}
    try:
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        kw_path = project_root / "data" / "config" / "keywords.json"
        if kw_path.exists():
            payload = json.loads(kw_path.read_text(encoding="utf-8"))
            for group in payload.get("groups", []):
                label = group.get("label") or "未分类"
                for kw in group.get("keywords", []):
                    text_to_bucket[kw.get("keyword_text", "").strip()] = label
                    kid_to_bucket[kw.get("keyword_id", "")] = label
    except Exception:
        pass
    return text_to_bucket, kid_to_bucket


def _run_signature(rank_to_article: dict[int, str]) -> str:
    """给定 {rank: article_id} 序列，返回稳定签名（同结果同签名）。"""
    items = sorted(rank_to_article.items())
    raw = "|".join(f"{r}:{a[:24]}" for r, a in items)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]


def _normalize_dt(snap: dict) -> datetime | None:
    return _safe_dt(snap.get("captured_at"))


def build_keyword_summaries(ctx: MonitorContext, heat: HeatContext) -> _KeywordSummaryResult:
    keywords_out: list[dict] = []
    today_date = ctx.window_end.date().isoformat()
    yesterday_date = (ctx.window_end.date() - timedelta(days=1)).isoformat()

    cfg_lookup: dict[str, dict] = {k["keyword_id"]: k for k in ctx.keywords}
    text_to_bucket, kid_to_bucket = _load_config_buckets()

    DEDUP_WINDOW_MINUTES = 5  # 同关键词 5 分钟内同结果视为同一次抓取

    for kid, snaps in ctx.snap_by_keyword.items():
        snaps_sorted = sorted(snaps, key=lambda s: s.get("captured_at", ""))

        # ── 两段式 dedup ──
        # 阶段 1: 同分钟 dedup（同 captured_at 完全相同 → 合并）
        # 阶段 2: 5 分钟窗口内同结果 dedup（同 article_id + rank 序列）
        by_minute: dict[str, list[dict]] = defaultdict(list)
        for snap in snaps_sorted:
            cap = snap.get("captured_at", "")
            minute_key = cap[:16] if len(cap) >= 16 else cap
            by_minute[minute_key].append(snap)

        primary_snaps: list[dict] = []
        for minute_key, group in by_minute.items():
            group_sorted = sorted(group, key=lambda s: s.get("captured_at", ""))
            primary_snaps.extend(group_sorted[:1])  # 每分钟只取第一条作为基础

        # 阶段 2: 5 分钟窗口 + 同结果签名
        windowed: list[dict] = []
        for snap in primary_snaps:
            cap = _normalize_dt(snap)
            if cap is None:
                windowed.append(snap)
                continue
            sid = snap.get("snapshot_id", "")
            hits = ctx.hits_by_snapshot.get(sid, [])
            rank_to_art = {h.get("rank"): h.get("article_id", "") for h in hits if h.get("rank") is not None}
            sig = _run_signature(rank_to_art)
            merged = False
            for existing in windowed:
                if abs((_normalize_dt(existing) - cap).total_seconds()) > DEDUP_WINDOW_MINUTES * 60:
                    continue
                ex_sid = existing.get("snapshot_id", "")
                ex_hits = ctx.hits_by_snapshot.get(ex_sid, [])
                ex_rank_to_art = {h.get("rank"): h.get("article_id", "") for h in ex_hits if h.get("rank") is not None}
                ex_sig = _run_signature(ex_rank_to_art)
                if ex_sig == sig:
                    # 同结果 → 合并到 existing
                    existing["_dedup_merged_count"] = existing.get("_dedup_merged_count", 1) + 1
                    if not existing.get("captured_at", "") or snap.get("captured_at", "") > existing.get("captured_at", ""):
                        existing["_captured_at"] = snap.get("captured_at")
                    merged = True
                    break
            if not merged:
                snap["_dedup_merged_count"] = 1
                windowed.append(snap)
        primary_snaps = windowed

        # ── 切窗口 ──
        today_snaps = [s for s in primary_snaps if s.get("snapshot_date") == today_date]
        yesterday_snaps = [s for s in primary_snaps if s.get("snapshot_date") == yesterday_date]
        distinct_days = {s.get("snapshot_date") for s in primary_snaps if s.get("snapshot_date")}
        coverage_days = len(distinct_days)

        art_idx = {a.get("article_id"): a for a in ctx.articles if a.get("article_id")}
        acct_idx = {a.get("account_id"): a for a in ctx.accounts if a.get("account_id")}

        # ── 构造 runs[] + 每条 run 的 articles[] ──
        runs_out: list[dict] = []
        today_best_rank = None
        today_top3 = 0
        today_top10 = 0
        today_hits_count = 0
        latest_run = None
        relevant_today = 0
        relevant_total = 0

        for snap in primary_snaps:
            sid = snap.get("snapshot_id", "")
            captured_at = snap.get("captured_at", "")
            snap_date = snap.get("snapshot_date", "")
            hits = ctx.hits_by_snapshot.get(sid, [])
            ranks = sorted([h.get("rank") for h in hits if h.get("rank")])

            best_rank = ranks[0] if ranks else None
            top3_count = sum(1 for r in ranks if r <= 3)
            top10_count = sum(1 for r in ranks if r <= 10)

            articles_list = []
            for h in hits:
                artid = h.get("article_id", "")
                art = art_idx.get(artid, {})
                acct = acct_idx.get(h.get("account_id", ""), {})
                hit_days = len({
                    s.get("snapshot_date") for s in primary_snaps
                    for hh in ctx.hits_by_snapshot.get(s.get("snapshot_id", ""), [])
                    if hh.get("article_id") == artid
                })
                articles_list.append({
                    "article_id": artid,
                    "title": h.get("title_raw") or art.get("title") or "",
                    "url": h.get("url_raw") or art.get("raw_url") or art.get("normalized_url") or "",
                    "content_path": artid,
                    "account": h.get("account_name_raw") or acct.get("canonical_name") or "",
                    "account_id": h.get("account_id", ""),
                    "account_headimg": acct.get("headimg_url") or "",
                    "rank": h.get("rank"),
                    "cover_url": art.get("cover_url"),
                    "work_type": art.get("work_type"),
                    "published_at": h.get("published_at_raw") or art.get("published_at"),
                    "hit_days": hit_days,
                    "liked_count": art.get("liked_count"),
                    "collected_count": art.get("collected_count"),
                    "comment_count": art.get("comment_count"),
                    "shared_count": art.get("shared_count"),
                    "read_count": art.get("read_count"),
                    "like_count": art.get("liked_count"),
                    "is_relevant": art.get("is_relevant", True),
                    "relevance_score": art.get("relevance_score", 1.0),
                })
            articles_list.sort(key=lambda x: (x.get("rank") or 999))
            relevant_count = sum(1 for a in articles_list if a["is_relevant"])

            cap_dt = _safe_dt(captured_at)
            run_date = snap_date
            run_time = cap_dt.strftime("%H:%M") if cap_dt else ""

            run_obj = {
                "id": sid,
                "date": run_date,
                "time": run_time,
                "captured_at": captured_at,
                "is_primary": bool(snap.get("is_primary")),
                "trigger_type": snap.get("trigger_type", "manual"),
                "status": snap.get("status", "success"),
                "result_count": len(articles_list),
                "best_rank": best_rank,
                "top3_count": top3_count,
                "top10_count": top10_count,
                "articles": articles_list,
                "terms": {"suggestions": [], "related": []},
                "dedup_merged_count": snap.get("_dedup_merged_count", 1),
                "relevant_count": relevant_count,
            }
            runs_out.append(run_obj)

            if snap_date == today_date:
                if best_rank is not None:
                    if today_best_rank is None or best_rank < today_best_rank:
                        today_best_rank = best_rank
                    today_top3 += top3_count
                    today_top10 += top10_count
                    today_hits_count += len(articles_list)
                relevant_today += relevant_count

            relevant_total += relevant_count

        runs_out.sort(key=lambda r: r.get("captured_at") or "", reverse=True)

        # ── 15 日历史 best / hits 序列 ──
        history_best: list[int] = []
        history_hits: list[int] = []
        for day in ctx.window_dates:
            day_runs = [r for r in runs_out if r.get("date") == day]
            best_day_rank = None
            hits_day = 0
            for r in day_runs:
                hits_day += r.get("result_count", 0)
                if r.get("best_rank") is not None and (best_day_rank is None or r["best_rank"] < best_day_rank):
                    best_day_rank = r["best_rank"]
            history_best.append(best_day_rank if best_day_rank is not None else 0)
            history_hits.append(hits_day)

        # ── 关联账号 / 文章聚合 ──
        kw_hits = ctx.hits_by_keyword.get(kid, [])
        tracked_accounts = len({h.get("account_id") for h in kw_hits if h.get("account_id")})
        article_ids = {h.get("article_id") for h in kw_hits if h.get("article_id")}
        article_count = len(article_ids)

        # ── 每账号聚合 ──
        acct_stats: dict[str, dict] = {}
        latest_run_obj = runs_out[0] if runs_out else None
        prev_run_obj = runs_out[1] if len(runs_out) >= 2 else None
        for h in kw_hits:
            aid = h.get("account_id", "")
            if not aid:
                continue
            entry = acct_stats.setdefault(aid, {
                "account_id": aid,
                "account_name": h.get("account_name_raw") or acct_idx.get(aid, {}).get("canonical_name", ""),
                "headimg_url": acct_idx.get(aid, {}).get("headimg_url"),
                "hit_count": 0,
                "best_rank": None,
                "hit_days": 0,
                "today_rank": None,
                "today_prev": None,
                "latest_seen_at": None,
            })
            r = h.get("rank")
            if r is not None and (entry["best_rank"] is None or r < entry["best_rank"]):
                entry["best_rank"] = r
            entry["hit_count"] += 1
            snap = next((s for s in primary_snaps if s.get("snapshot_id") == h.get("snapshot_id")), None)
            if snap and (entry["latest_seen_at"] is None or snap.get("captured_at", "") > entry["latest_seen_at"]):
                entry["latest_seen_at"] = snap.get("captured_at")

        for aid, entry in acct_stats.items():
            entry["hit_days"] = len({
                s.get("snapshot_date") for s in primary_snaps
                for hh in ctx.hits_by_snapshot.get(s.get("snapshot_id", ""), [])
                if hh.get("account_id") == aid
            })

        if latest_run_obj:
            today_ranks = defaultdict(list)
            for art in latest_run_obj.get("articles", []):
                if art.get("account_id"):
                    today_ranks[art["account_id"]].append(art.get("rank"))
            for aid, ranks in today_ranks.items():
                if aid in acct_stats:
                    acct_stats[aid]["today_rank"] = min(r for r in ranks if r)

        if prev_run_obj:
            prev_ranks = defaultdict(list)
            for art in prev_run_obj.get("articles", []):
                if art.get("account_id"):
                    prev_ranks[art["account_id"]].append(art.get("rank"))
            for aid, ranks in prev_ranks.items():
                if aid in acct_stats:
                    acct_stats[aid]["today_prev"] = min(r for r in ranks if r)

        accounts_top = sorted(
            acct_stats.values(),
            key=lambda x: (-x["hit_count"], x["best_rank"] if x["best_rank"] else 999),
        )[:10]

        # ── kw_score ──
        breadth = min(20.0, math.sqrt(max(1, tracked_accounts)) * 4.0 + math.sqrt(max(1, article_count)) * 1.5)
        richness = min(60.0, coverage_days * 2.5 + article_count * 1.2 + tracked_accounts * 1.5)
        heat_score = 0.0
        kw_score_total = round(richness + breadth + heat_score, 1)

        # ── 顶部最近抓取时间 ──
        last_snap = runs_out[0] if runs_out else None
        if last_snap:
            cap = last_snap.get("captured_at") or ""
            cap_dt = _safe_dt(cap)
            run_date = last_snap.get("date", "")
            run_time = last_snap.get("time", "")
            run_at_short = f"{run_date} {run_time}".strip() if run_date or run_time else cap[:16].replace("T", " ")
            latest_run = {
                "run_at": cap,
                "run_at_short": run_at_short,
                "date": run_date,
                "time": run_time,
                "trigger_type": last_snap.get("trigger_type"),
                "status": last_snap.get("status"),
                "result_count": last_snap.get("result_count", 0),
            }

        # ── move_summary ──
        yesterday_hits = sum(r.get("result_count", 0) for r in yesterday_snaps)
        primary_type = None
        if today_snaps and yesterday_snaps:
            if today_hits_count > yesterday_hits:
                primary_type = "up"
            elif today_hits_count < yesterday_hits:
                primary_type = "down"
        elif today_snaps and not yesterday_snaps:
            primary_type = "new"

        # ── keyword_read_delta（XHS 无阅读代理）──
        keyword_read_delta = {
            "status": "insufficient_data",
            "reason": "xhs_no_reading_proxy",
            "read_delta_estimated": None,
            "trend_label": "观察中",
            "confidence_score": 0,
            "confidence_level": "insufficient",
        }

        # ── 配置覆盖 ──
        cfg = cfg_lookup.get(kid, {})
        kw_text = (cfg.get("keyword_text") if cfg else None) or kid
        bucket = kid_to_bucket.get(kid) or text_to_bucket.get(kw_text.strip()) or "未分类"

        # ── 相关性统计 ──
        is_relevant_count = sum(1 for r in runs_out for a in r["articles"] if a["is_relevant"])
        relevance_stats = {
            "total": sum(r["result_count"] for r in runs_out),
            "is_relevant_count": is_relevant_count,
            "is_irrelevant_count": sum(r["result_count"] for r in runs_out) - is_relevant_count,
            "method": "title_desc_product_token_match_v2",
        }

        keywords_out.append({
            "keyword_id": kid,
            "keyword": kw_text,
            "topic": kw_text,
            "keyword_bucket": bucket,
            "note": cfg.get("note") or "",
            "enabled": cfg.get("enabled", True),
            "today_best": today_best_rank,
            "today_count": today_hits_count,
            "yesterday_hits": yesterday_hits,
            "coverage_days": coverage_days,
            "tracked_accounts": tracked_accounts,
            "article_count": article_count,
            "latest_run": latest_run,
            "runs": runs_out,
            "history_best": history_best,
            "history_hits": history_hits,
            "accounts": accounts_top,
            "heat_summary": {
                "available": False,
                "method": heat.method,
            },
            "kw_score": {
                "total": kw_score_total,
                "heat": heat_score,
                "breadth": round(breadth, 1),
                "richness": round(richness, 1),
                "has_heat": heat.has_heat,
                "wso_val": None,
                "dso_val": None,
            },
            "keyword_read_delta": keyword_read_delta,
            "move_summary": {
                "primary_type": primary_type,
                "secondary_type": None,
                "primary_count": 0,
                "secondary_count": 0,
            },
            "is_pinned": False,
            "pin_order": None,
            "is_relevant_count": is_relevant_count,
            "relevance_stats": relevance_stats,
            "today_relevant_count": relevant_today,
            "keyword_heat_metric": build_keyword_heat_metric(runs_out, ctx.window_dates),
        })

    return _KeywordSummaryResult(keywords=keywords_out)
