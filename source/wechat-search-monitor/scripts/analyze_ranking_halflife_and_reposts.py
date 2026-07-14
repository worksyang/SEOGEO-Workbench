#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None


def pctile(values: list[float], p: float) -> float | None:
    xs = sorted(x for x in values if x is not None and not math.isnan(x))
    if not xs:
        return None
    if len(xs) == 1:
        return round(xs[0], 3)
    idx = (len(xs) - 1) * p
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return round(xs[lo], 3)
    return round(xs[lo] * (hi - idx) + xs[hi] * (idx - lo), 3)


def normalize_title(title: str | None) -> str:
    return re.sub(r"\s+", "", title or "").lower()


def title_similarity(a: str | None, b: str | None) -> float:
    aa = normalize_title(a)
    bb = normalize_title(b)
    if not aa or not bb:
        return 0.0
    return SequenceMatcher(None, aa, bb).ratio()


def compact(text: str | None, n: int = 70) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


class Analyzer:
    def __init__(self) -> None:
        self.snapshots = load_json(ROOT / "normalized" / "snapshots.json")
        self.hits = load_json(ROOT / "normalized" / "ranking_hits.json")
        self.articles = {a["article_id"]: a for a in load_json(ROOT / "normalized" / "articles.json")}
        self.accounts = {a["account_id"]: a for a in load_json(ROOT / "normalized" / "accounts.json")}
        from app.repositories.keyword_registry_repo import KeywordRegistryRepository
        self.keywords = {
            k["keyword_id"]: k
            for k in KeywordRegistryRepository(ROOT / "data/state/app.db").list_keywords()
        }
        self.next_steps = load_json(ROOT / "临时产物" / "260704_negative_signal_next_steps.json")

        self.snap_by_id = {s["snapshot_id"]: s for s in self.snapshots if s.get("status") == "success"}
        self.snap_dt = {sid: parse_dt(s["captured_at"]) for sid, s in self.snap_by_id.items()}
        self.snap_kw = {sid: s["keyword_id"] for sid, s in self.snap_by_id.items()}

        self.hits_by_article: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.hits_by_article_keyword: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for hit in self.hits:
            sid = hit.get("snapshot_id")
            snap = self.snap_by_id.get(sid)
            t = self.snap_dt.get(sid)
            if not snap or not t:
                continue
            row = {
                **hit,
                "captured_at": snap["captured_at"],
                "captured_dt": t,
                "snapshot_date": snap.get("snapshot_date"),
                "keyword_id": snap.get("keyword_id"),
                "keyword": self.keywords.get(snap.get("keyword_id"), {}).get("keyword_text") or snap.get("keyword_id"),
            }
            self.hits_by_article[hit["article_id"]].append(row)
            self.hits_by_article_keyword[(hit["article_id"], snap["keyword_id"])].append(row)
        for rows in self.hits_by_article.values():
            rows.sort(key=lambda x: x["captured_dt"])
        for rows in self.hits_by_article_keyword.values():
            rows.sort(key=lambda x: x["captured_dt"])

    def account_name(self, account_id: str | None) -> str:
        return self.accounts.get(account_id or "", {}).get("canonical_name") or account_id or ""

    def article_title(self, article_id: str | None) -> str:
        return self.articles.get(article_id or "", {}).get("title") or ""

    def article_pub(self, article_id: str | None) -> datetime | None:
        return parse_dt(self.articles.get(article_id or "", {}).get("published_at"))

    def halflife_rows(self, rank_limit: int) -> list[dict[str, Any]]:
        rows = []
        for article_id, hits in self.hits_by_article.items():
            pub = self.article_pub(article_id)
            if not pub:
                continue
            qualified = [h for h in hits if int(h.get("rank") or 999) <= rank_limit]
            if not qualified:
                continue
            first = min(qualified, key=lambda x: x["captured_dt"])
            last = max(qualified, key=lambda x: x["captured_dt"])
            article = self.articles.get(article_id, {})
            age_days = (last["captured_dt"] - pub).total_seconds() / 86400
            first_age_days = (first["captured_dt"] - pub).total_seconds() / 86400
            top_span_days = (last["captured_dt"] - first["captured_dt"]).total_seconds() / 86400
            if age_days < 0:
                continue
            rows.append({
                "article_id": article_id,
                "title": article.get("title"),
                "account_id": article.get("account_id"),
                "account": self.account_name(article.get("account_id")),
                "published_at": article.get("published_at"),
                "first_top_age_days": round(first_age_days, 3),
                "last_top_age_days": round(age_days, 3),
                "top_span_days": round(top_span_days, 3),
                "top_hit_count": len(qualified),
                "keyword_count": len({h["keyword_id"] for h in qualified}),
                "best_rank": min(int(h.get("rank") or 999) for h in qualified),
                "last_top_at": last["captured_at"],
            })
        rows.sort(key=lambda x: x["last_top_age_days"])
        return rows

    def summarize_halflife(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        ages = [r["last_top_age_days"] for r in rows]
        spans = [r["top_span_days"] for r in rows]
        buckets = {
            "<=1d": sum(1 for x in ages if x <= 1),
            "<=3d": sum(1 for x in ages if x <= 3),
            "<=7d": sum(1 for x in ages if x <= 7),
            "<=14d": sum(1 for x in ages if x <= 14),
            "<=30d": sum(1 for x in ages if x <= 30),
            ">30d": sum(1 for x in ages if x > 30),
        }
        return {
            "article_count": len(rows),
            "p50_days": pctile(ages, 0.5),
            "p80_days": pctile(ages, 0.8),
            "p90_days": pctile(ages, 0.9),
            "visibility_span_p50_days": pctile(spans, 0.5),
            "visibility_span_p80_days": pctile(spans, 0.8),
            "median_top_hit_count": round(median([r["top_hit_count"] for r in rows]), 3) if rows else None,
            "buckets": buckets,
        }

    def repost_rows(self) -> list[dict[str, Any]]:
        rows = []
        from analyze_negative_signal_next import Analyzer as NegativeSignalAnalyzer

        neg = NegativeSignalAnalyzer()
        candidates = []
        for event in neg.hard_drop_pairs():
            replacement = neg.replacement_for(event)
            if replacement.get("replacement_type") == "same_account_update":
                candidates.append({**event, "replacement": replacement})
        seen = set()
        for event in candidates:
            repl = event.get("replacement") or {}
            if repl.get("replacement_type") != "same_account_update":
                continue
            key = (event.get("article_id"), repl.get("article_id"), event.get("keyword_id"))
            if key in seen:
                continue
            seen.add(key)
            old_id = event.get("article_id")
            new_id = repl.get("article_id")
            keyword_id = event.get("keyword_id")
            old_title = event.get("title") or self.article_title(old_id)
            new_title = repl.get("title") or self.article_title(new_id)
            new_hits = self.hits_by_article_keyword.get((new_id, keyword_id), [])
            top5_hits = [h for h in new_hits if int(h.get("rank") or 999) <= 5]
            top10_hits = [h for h in new_hits if int(h.get("rank") or 999) <= 10]
            first_top5 = min(top5_hits, key=lambda h: h["captured_dt"]) if top5_hits else None
            last_top5 = max(top5_hits, key=lambda h: h["captured_dt"]) if top5_hits else None
            first_top10 = min(top10_hits, key=lambda h: h["captured_dt"]) if top10_hits else None
            last_top10 = max(top10_hits, key=lambda h: h["captured_dt"]) if top10_hits else None
            sim = title_similarity(old_title, new_title)
            if normalize_title(old_title) == normalize_title(new_title):
                title_change_type = "exact_same"
            elif sim >= 0.72:
                title_change_type = "minor_edit"
            else:
                title_change_type = "major_rewrite"
            rows.append({
                "keyword_id": keyword_id,
                "keyword": event.get("keyword"),
                "account_id": event.get("account_id"),
                "account": event.get("account"),
                "old_article_id": old_id,
                "new_article_id": new_id,
                "old_title": old_title,
                "new_title": new_title,
                "old_prev_rank": event.get("prev_rank"),
                "new_replacement_rank": repl.get("rank"),
                "title_similarity": round(sim, 3),
                "title_change_type": title_change_type,
                "returned_top5": bool(top5_hits),
                "returned_top10": bool(top10_hits),
                "top5_span_days": round((last_top5["captured_dt"] - first_top5["captured_dt"]).total_seconds() / 86400, 3) if first_top5 and last_top5 else 0,
                "top10_span_days": round((last_top10["captured_dt"] - first_top10["captured_dt"]).total_seconds() / 86400, 3) if first_top10 and last_top10 else 0,
                "top5_hit_count": len(top5_hits),
                "top10_hit_count": len(top10_hits),
                "best_new_rank": min((int(h.get("rank") or 999) for h in new_hits), default=None),
            })
        rows.sort(key=lambda x: (not x["returned_top5"], -x["top5_span_days"], -x["title_similarity"]))
        return rows

    def summarize_reposts(self, rows: list[dict[str, Any]], top5_span_p50: float | None) -> dict[str, Any]:
        by_type = {}
        for t in sorted({r["title_change_type"] for r in rows}):
            xs = [r for r in rows if r["title_change_type"] == t]
            spans = [r["top5_span_days"] for r in xs if r["returned_top5"]]
            by_type[t] = {
                "count": len(xs),
                "returned_top5_count": sum(1 for r in xs if r["returned_top5"]),
                "returned_top5_pct": round(sum(1 for r in xs if r["returned_top5"]) / len(xs) * 100, 1) if xs else 0,
                "median_top5_span_days": pctile(spans, 0.5),
            }
        spans = [r["top5_span_days"] for r in rows if r["returned_top5"]]
        comparable = sum(1 for r in rows if r["returned_top5"] and top5_span_p50 is not None and r["top5_span_days"] >= top5_span_p50)
        return {
            "sample_count": len(rows),
            "returned_top5_count": sum(1 for r in rows if r["returned_top5"]),
            "returned_top5_pct": round(sum(1 for r in rows if r["returned_top5"]) / len(rows) * 100, 1) if rows else 0,
            "returned_top10_count": sum(1 for r in rows if r["returned_top10"]),
            "returned_top10_pct": round(sum(1 for r in rows if r["returned_top10"]) / len(rows) * 100, 1) if rows else 0,
            "median_top5_span_days": pctile(spans, 0.5),
            "p80_top5_span_days": pctile(spans, 0.8),
            "new_life_ge_normal_top5_span_p50_count": comparable,
            "normal_top5_visibility_span_p50_days": top5_span_p50,
            "by_title_change_type": by_type,
        }

    def run(self) -> dict[str, Any]:
        top5_rows = self.halflife_rows(5)
        top10_rows = self.halflife_rows(10)
        top5 = self.summarize_halflife(top5_rows)
        top10 = self.summarize_halflife(top10_rows)
        reposts = self.repost_rows()
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "halflife": {
                "top5": top5,
                "top10": top10,
                "top5_examples_long_lived": sorted(top5_rows, key=lambda r: r["last_top_age_days"], reverse=True)[:30],
                "top5_examples_short_lived": top5_rows[:30],
            },
            "reposts": {
                "summary": self.summarize_reposts(reposts, top5.get("visibility_span_p50_days")),
                "rows": reposts,
            },
        }
        dump_json(ROOT / "临时产物" / "260704_ranking_halflife_and_reposts.json", payload)
        (ROOT / "排名半衰期.md").write_text(self.render_halflife_doc(payload), encoding="utf-8")
        return payload

    def render_halflife_doc(self, payload: dict[str, Any]) -> str:
        top5 = payload["halflife"]["top5"]
        top10 = payload["halflife"]["top10"]
        lines = [
            "# 排名半衰期",
            "",
            f"- 生成时间：{payload['generated_at']}",
            "- 口径：以文章发布时间为起点，统计文章最后一次进入前 5 / 前 10 的时间差。",
            "- 注意：这是搜索可见性的经验寿命，不等于文章被处罚或删除。",
            "",
            "## 核心结论",
            "",
            f"- 前 5 样本：{top5['article_count']} 篇；**50% 的文章在 {top5['p50_days']} 天后不再进入前 5**，80% 在 {top5['p80_days']} 天后不再进入前 5。",
            f"- 前 10 样本：{top10['article_count']} 篇；**50% 的文章在 {top10['p50_days']} 天后不再进入前 10**，80% 在 {top10['p80_days']} 天后不再进入前 10。",
            f"- 作为“在榜维持天数”参照：前 5 文章从首次进前 5 到最后进前 5 的跨度中位数是 {top5['visibility_span_p50_days']} 天，P80 是 {top5['visibility_span_p80_days']} 天。",
            "",
            "## 寿命分布",
            "",
            "| 口径 | 样本 | 发布到最后在榜P50 | 发布到最后在榜P80 | P90 | 在榜跨度P50 | 在榜跨度P80 | <=1天 | <=3天 | <=7天 | <=14天 | <=30天 | >30天 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            self.bucket_line("前5", top5),
            self.bucket_line("前10", top10),
            "",
            "## 业务建议",
            "",
            f"1. **前 5 维护周期按 P50 之前安排**：如果要守住核心词，文章发布后约 {top5['p50_days']} 天前就应该准备更新稿或补充稿；若按在榜跨度看，中位可见窗口约 {top5['visibility_span_p50_days']} 天。",
            f"2. **前 10 代表更宽松的可见性寿命**：前 10 的 P80 是 {top10['p80_days']} 天，可作为长尾维护窗口。",
            "3. **不要把老文掉榜直接理解为处罚**：URL 探测没有发现违规/封号主因，老文死亡更像新鲜度竞争和搜索意图更新。",
            "4. **内容更新要有信息增量**：单纯复制标题不是稳妥策略；更应该补新数据、新产品节点、新对比表和适用人群。",
            "",
            "## 长寿文章样本",
            "",
            "| 寿命天数 | 账号 | 标题 | 最佳排名 | 命中关键词数 |",
            "|---:|---|---|---:|---:|",
        ]
        for row in payload["halflife"]["top5_examples_long_lived"][:20]:
            lines.append(
                f"| {row['last_top_age_days']} | {row['account']} | {compact(row['title'], 42)} | {row['best_rank']} | {row['keyword_count']} |"
            )
        lines.append("")
        return "\n".join(lines)

    def bucket_line(self, label: str, stat: dict[str, Any]) -> str:
        b = stat["buckets"]
        return (
            f"| {label} | {stat['article_count']} | {stat['p50_days']} | {stat['p80_days']} | {stat['p90_days']} | "
            f"{stat['visibility_span_p50_days']} | {stat['visibility_span_p80_days']} | "
            f"{b['<=1d']} | {b['<=3d']} | {b['<=7d']} | {b['<=14d']} | {b['<=30d']} | {b['>30d']} |"
        )


def main() -> None:
    payload = Analyzer().run()
    print("[ok] wrote 排名半衰期.md")
    print("[ok] wrote 临时产物/260704_ranking_halflife_and_reposts.json")
    print("[halflife top5]", payload["halflife"]["top5"])
    print("[halflife top10]", payload["halflife"]["top10"])
    print("[reposts]", payload["reposts"]["summary"])


if __name__ == "__main__":
    main()
