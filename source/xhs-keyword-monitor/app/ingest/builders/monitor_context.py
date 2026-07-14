"""Monitor 派生层上下文 — 把 entities 预索引为窗口查询用的字典。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.ingest.builders.monitor_scoring import WINDOW_DAYS


@dataclass
class MonitorContext:
    keywords: list[dict]
    snapshots: list[dict]
    snapshot_terms: list[dict]
    accounts: list[dict]
    articles: list[dict]
    ranking_hits: list[dict]
    metric_observations: list[dict]
    keyword_settings: dict[str, dict] = field(default_factory=dict)
    window_dates: list[str] = field(default_factory=list)
    window_end: datetime = None

    # ── 索引（构建时填充） ──
    snap_by_keyword: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    hits_by_snapshot: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    hits_by_keyword: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    hits_by_account: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    hits_by_article: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    articles_by_id: dict[str, dict] = field(default_factory=dict)
    accounts_by_id: dict[str, dict] = field(default_factory=dict)


def _safe_parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def build_monitor_context(ent: dict, keyword_settings: dict[str, dict] | None = None) -> MonitorContext:
    keyword_settings = keyword_settings or {}

    # 决定 window_end = 最新快照时间或今天
    snaps = ent.get("snapshots", [])
    latest = None
    for s in snaps:
        dt = _safe_parse(s.get("captured_at"))
        if dt and (latest is None or dt > latest):
            latest = dt
    window_end = latest or datetime.now()

    window_dates = [
        (window_end - timedelta(days=WINDOW_DAYS - 1 - i)).date().isoformat()
        for i in range(WINDOW_DAYS)
    ]

    ctx = MonitorContext(
        keywords=list(ent.get("keywords", [])),
        snapshots=list(snaps),
        snapshot_terms=list(ent.get("snapshot_terms", [])),
        accounts=list(ent.get("accounts", [])),
        articles=list(ent.get("articles", [])),
        ranking_hits=list(ent.get("ranking_hits", [])),
        metric_observations=list(ent.get("note_metric_observations", [])),
        keyword_settings=keyword_settings,
        window_dates=window_dates,
        window_end=window_end,
    )

    # ── 索引填充 ──
    snap_by_id = {s["snapshot_id"]: s for s in ctx.snapshots}
    for hit in ctx.ranking_hits:
        snap = snap_by_id.get(hit.get("snapshot_id", ""))
        if not snap:
            continue
        kid = snap.get("keyword_id", "")
        aid = hit.get("account_id", "")
        artid = hit.get("article_id", "")
        sid = hit.get("snapshot_id", "")
        if kid:
            ctx.hits_by_keyword[kid].append(hit)
        if aid:
            ctx.hits_by_account[aid].append(hit)
        if artid:
            ctx.hits_by_article[artid].append(hit)
        if sid:
            ctx.hits_by_snapshot[sid].append(hit)

    for s in ctx.snapshots:
        kid = s.get("keyword_id", "")
        if kid:
            ctx.snap_by_keyword[kid].append(s)

    ctx.articles_by_id = {a.get("article_id"): a for a in ctx.articles if a.get("article_id")}
    ctx.accounts_by_id = {a.get("account_id"): a for a in ctx.accounts if a.get("account_id")}

    return ctx
