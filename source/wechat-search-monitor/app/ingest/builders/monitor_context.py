"""monitor_context.py — 监控派生层共享上下文。

封装原 `build_monitor_data()` 顶部散落的 7-8 个索引和 3 个窗口查询闭包，
让后续阶段（关键词汇总、账号汇总）能通过统一对象访问，避免闭包变量
跨 400 行流动的 Agent 误读风险。

行为契约与原 builder 完全一致：
- primary_snap_for 优先取 is_primary，否则取当日 captured_at 最早一条
- previous_primary_snap_before 取 ref_day 之前最近一个能查到主快照的天
- keyword_account_history 输出 {account_id: [rank × 15 天]}，每天取最优 rank
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from app.ingest.builders.monitor_scoring import WINDOW_DAYS
from app.keyword_bucket_resolver import DEFAULT_BUCKET
from app.topic_resolver import resolve_topic


@dataclass
class MonitorBuildContext:
    """监控派生层共享上下文。

    字段命名直接对应原 builder 内部的闭包变量名，便于 diff 阅读。
    `kw_topic_by_id` / `kw_bucket_by_id` 因为依赖 keyword_settings，
    在 build_context() 时一次性计算。
    """

    ent: dict
    keyword_settings: dict[str, dict]
    # 原始 ent 引用
    keywords: list[dict]
    snapshots: list[dict]
    hits: list[dict]
    accounts: list[dict]
    articles: list[dict]
    snapshot_terms: list[dict]
    # 索引
    kw_by_id: dict[str, dict] = field(default_factory=dict)
    acct_by_id: dict[str, dict] = field(default_factory=dict)
    art_by_id: dict[str, dict] = field(default_factory=dict)
    kw_topic_by_id: dict[str, str] = field(default_factory=dict)
    kw_bucket_by_id: dict[str, str] = field(default_factory=dict)
    # 快照/命中索引
    snaps_by_kw: dict[str, list[dict]] = field(default_factory=dict)
    hits_by_snap: dict[str, list[dict]] = field(default_factory=dict)
    # 窗口
    window_dates: list[date] = field(default_factory=list)

    def primary_snap_for(self, keyword_id: str, day: date) -> Optional[dict]:
        """某关键词在某一天的主快照。优先 is_primary，否则取当日最早一条。"""
        group = [s for s in self.snaps_by_kw.get(keyword_id, []) if s["snapshot_date"] == day.isoformat()]
        if not group:
            return None
        primaries = [s for s in group if s["is_primary"]]
        if primaries:
            return primaries[0]
        return min(group, key=lambda s: s["captured_at"])

    def previous_primary_snap_before(self, keyword_id: str, ref_day: date) -> Optional[dict]:
        """某关键词在 ref_day 之前最近一个能查到主快照的天的主快照。"""
        prior_days = sorted(
            {s["snapshot_date"] for s in self.snaps_by_kw.get(keyword_id, []) if s["snapshot_date"] < ref_day.isoformat()},
            reverse=True,
        )
        for day_str in prior_days:
            day = datetime.fromisoformat(day_str).date()
            ps = self.primary_snap_for(keyword_id, day)
            if ps:
                return ps
        return None

    def keyword_account_history(self, keyword_id: str) -> dict[str, list[int]]:
        """某关键词在 15 天窗口内、每个账号每天的最优 rank。

        返回 {account_id: [rank × WINDOW_DAYS]}，0 表示该天未上榜。
        """
        per_acct: dict[str, list[int]] = defaultdict(lambda: [0] * WINDOW_DAYS)
        for idx, day in enumerate(self.window_dates):
            ps = self.primary_snap_for(keyword_id, day)
            if not ps:
                continue
            for h in self.hits_by_snap.get(ps["snapshot_id"], []):
                cur = per_acct[h["account_id"]][idx]
                if cur == 0 or h["rank"] < cur:
                    per_acct[h["account_id"]][idx] = h["rank"]
        return per_acct


def build_monitor_context(ent: dict, keyword_settings: dict[str, dict] | None = None) -> MonitorBuildContext:
    """从 ent 构造共享上下文。"""
    keyword_settings = keyword_settings or {}
    keywords = ent["keywords"]
    snapshots = ent["snapshots"]
    hits = ent["ranking_hits"]
    accounts = ent["accounts"]
    articles = ent["articles"]
    snapshot_terms = ent.get("snapshot_terms", [])

    kw_by_id = {k["keyword_id"]: k for k in keywords}
    acct_by_id = {a["account_id"]: a for a in accounts}
    art_by_id = {a["article_id"]: a for a in articles}
    kw_topic_by_id = {
        k["keyword_id"]: resolve_topic(k["keyword_text"], keyword_settings.get(k["keyword_id"], {}).get("topic"))
        for k in keywords
    }
    kw_bucket_by_id = {
        k["keyword_id"]: (
            keyword_settings.get(k["keyword_id"], {}).get("keyword_bucket")
            or DEFAULT_BUCKET
        )
        for k in keywords
    }

    snaps_by_kw: dict[str, list[dict]] = defaultdict(list)
    for s in snapshots:
        snaps_by_kw[s["keyword_id"]].append(s)
    for v in snaps_by_kw.values():
        v.sort(key=lambda s: s["captured_at"], reverse=True)

    hits_by_snap: dict[str, list[dict]] = defaultdict(list)
    for h in hits:
        hits_by_snap[h["snapshot_id"]].append(h)
    for v in hits_by_snap.values():
        v.sort(key=lambda h: h["rank"])

    if not snapshots:
        end_date = datetime.now().date()
    else:
        end_date = max(datetime.fromisoformat(s["captured_at"]).date() for s in snapshots)
    window_dates = [(end_date - timedelta(days=WINDOW_DAYS - 1 - i)) for i in range(WINDOW_DAYS)]

    return MonitorBuildContext(
        ent=ent,
        keyword_settings=keyword_settings,
        keywords=keywords,
        snapshots=snapshots,
        hits=hits,
        accounts=accounts,
        articles=articles,
        snapshot_terms=snapshot_terms,
        kw_by_id=kw_by_id,
        acct_by_id=acct_by_id,
        art_by_id=art_by_id,
        kw_topic_by_id=kw_topic_by_id,
        kw_bucket_by_id=kw_bucket_by_id,
        snaps_by_kw=snaps_by_kw,
        hits_by_snap=hits_by_snap,
        window_dates=window_dates,
    )


__all__ = ["MonitorBuildContext", "build_monitor_context"]
