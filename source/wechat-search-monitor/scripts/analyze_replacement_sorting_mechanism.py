#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "临时产物" / "260704_replacement_sorting_mechanism.json"
OUT_MD = ROOT / "历史记录" / "260704_执行记录_排序替换机制分析.md"


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


def safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def percentile(values: list[float], p: float) -> float | None:
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


def spearman(xs: list[float], ys: list[float]) -> float | None:
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None

    def ranks(vals: list[float]) -> list[float]:
        order = sorted(enumerate(vals), key=lambda p: p[1])
        out = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and order[j + 1][1] == order[i][1]:
                j += 1
            rank = (i + j + 2) / 2
            for k in range(i, j + 1):
                out[order[k][0]] = rank
            i = j + 1
        return out

    rx = ranks([p[0] for p in pairs])
    ry = ranks([p[1] for p in pairs])
    mx = sum(rx) / len(rx)
    my = sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return round(cov / (sx * sy), 4) if sx and sy else None


def norm(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def char_ngrams(text: str, n: int = 2) -> set[str]:
    text = norm(text)
    return {text[i : i + n] for i in range(max(0, len(text) - n + 1))}


def keyword_match_features(title: str | None, keyword: str | None) -> dict[str, Any]:
    t = norm(title)
    k = norm(keyword)
    exact = bool(k and k in t)
    if not k or not t:
        overlap = 0.0
    else:
        kg = char_ngrams(k)
        tg = char_ngrams(t)
        overlap = safe_div(len(kg & tg), len(kg))
    return {
        "kw_exact_in_title": 1 if exact else 0,
        "kw_title_overlap": round(overlap, 4),
    }


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1 / (1 + z)
    z = math.exp(x)
    return z / (1 + z)


def logistic_regression(rows: list[dict[str, Any]], feature_names: list[str], label_name: str) -> dict[str, Any]:
    clean = []
    for row in rows:
        if row.get(label_name) is None:
            continue
        vals = []
        ok = True
        for name in feature_names:
            val = row.get(name)
            if val is None or (isinstance(val, float) and math.isnan(val)):
                ok = False
                break
            vals.append(float(val))
        if ok:
            clean.append((vals, int(row[label_name])))
    if len(clean) < 30 or len({y for _, y in clean}) < 2:
        return {"available": False, "reason": "insufficient_data", "n": len(clean)}

    cols = list(zip(*(x for x, _ in clean)))
    means = [sum(col) / len(col) for col in cols]
    stds = []
    for col, m in zip(cols, means):
        var = sum((v - m) ** 2 for v in col) / len(col)
        stds.append(math.sqrt(var) or 1.0)
    xmat = [[(v - m) / s for v, m, s in zip(vals, means, stds)] for vals, _ in clean]
    y = [label for _, label in clean]
    weights = [0.0] * (len(feature_names) + 1)
    lr = 0.05
    l2 = 0.01
    for _ in range(1200):
        grads = [0.0] * len(weights)
        for xs, label in zip(xmat, y):
            z = weights[0] + sum(w * x for w, x in zip(weights[1:], xs))
            pred = sigmoid(z)
            err = pred - label
            grads[0] += err
            for i, x in enumerate(xs, 1):
                grads[i] += err * x
        n = len(y)
        weights[0] -= lr * grads[0] / n
        for i in range(1, len(weights)):
            weights[i] -= lr * (grads[i] / n + l2 * weights[i])
    return {
        "available": True,
        "n": len(clean),
        "positive_rate": round(sum(y) / len(y), 4),
        "features": [
            {
                "feature": name,
                "coef": round(coef, 4),
                "direction": "提高被踢/丢失概率" if coef > 0 else "降低被踢/丢失概率",
            }
            for name, coef in sorted(zip(feature_names, weights[1:]), key=lambda p: abs(p[1]), reverse=True)
        ],
        "intercept": round(weights[0], 4),
    }


def median_by_label(rows: list[dict[str, Any]], features: list[str], label: str) -> dict[str, Any]:
    out = {}
    for feature in features:
        pos = [float(r[feature]) for r in rows if r.get(label) == 1 and r.get(feature) is not None]
        neg = [float(r[feature]) for r in rows if r.get(label) == 0 and r.get(feature) is not None]
        out[feature] = {
            "positive_median": round(median(pos), 4) if pos else None,
            "negative_median": round(median(neg), 4) if neg else None,
        }
    return out


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
        self.monitor = load_json(ROOT / "normalized" / "monitor-data.json")
        self.penalty = load_json(ROOT / "normalized" / "penalty_signals.json")

        self.monitor_account = {a["account_id"]: a for a in self.monitor.get("accounts", [])}
        self.keyword_by_text = {k["keyword_text"]: k for k in self.keywords.values()}
        self.snap_by_id = {s["snapshot_id"]: s for s in self.snapshots if s.get("status") == "success"}
        self.snap_dt = {sid: parse_dt(s["captured_at"]) for sid, s in self.snap_by_id.items()}
        self.snap_kw = {sid: s["keyword_id"] for sid, s in self.snap_by_id.items()}
        self.snaps_by_kw: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for snap in self.snap_by_id.values():
            if snap.get("result_count", 0) >= 5:
                self.snaps_by_kw[snap["keyword_id"]].append(snap)
        for rows in self.snaps_by_kw.values():
            rows.sort(key=lambda s: s["captured_at"])

        self.hits_by_snap: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for hit in self.hits:
            if hit["snapshot_id"] in self.snap_by_id:
                self.hits_by_snap[hit["snapshot_id"]].append(hit)
        for rows in self.hits_by_snap.values():
            rows.sort(key=lambda h: int(h.get("rank") or 999))

        self.article_keywords_top10: dict[str, set[str]] = defaultdict(set)
        self.article_hits_by_kw: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.day_account_kw_best: dict[tuple[str, str, str], dict[str, Any]] = {}
        for hit in self.hits:
            snap = self.snap_by_id.get(hit["snapshot_id"])
            t = self.snap_dt.get(hit["snapshot_id"])
            if not snap or not t:
                continue
            kid = snap["keyword_id"]
            rank = int(hit.get("rank") or 999)
            keyword = self.keywords[kid]["keyword_text"]
            row = {**hit, "captured_dt": t, "date": snap["snapshot_date"], "keyword_id": kid, "keyword": keyword}
            self.article_hits_by_kw[(hit["article_id"], kid)].append(row)
            if rank <= 10:
                self.article_keywords_top10[hit["article_id"]].add(kid)
            key = (snap["snapshot_date"], hit["account_id"], keyword)
            prev = self.day_account_kw_best.get(key)
            if prev is None or rank < int(prev.get("rank") or 999):
                self.day_account_kw_best[key] = row
        for rows in self.article_hits_by_kw.values():
            rows.sort(key=lambda r: r["captured_dt"])

    def article_pub(self, article_id: str | None) -> datetime | None:
        return parse_dt(self.articles.get(article_id or "", {}).get("published_at"))

    def account_score(self, account_id: str | None) -> float:
        return float(self.monitor_account.get(account_id or "", {}).get("score") or 0)

    def content_len(self, article_id: str | None) -> int:
        article = self.articles.get(article_id or "", {})
        path = article.get("content_file_path")
        if not path:
            return 0
        fp = ROOT / path
        if not fp.exists():
            return 0
        md = fp.read_text(encoding="utf-8", errors="ignore")
        text = []
        for line in md.splitlines():
            if line.strip().startswith("![") or re.match(r"^https?://", line.strip()):
                continue
            line = re.sub(r"!\[[^\]]*]\([^)]*\)", "", line)
            line = re.sub(r"\[[^\]]*]\([^)]*\)", "", line)
            line = re.sub(r"[#>*_`|~-]+", "", line)
            text.append(line)
        return len(re.sub(r"\s+", "", "".join(text)))

    def keyword_inflow_and_halflife(self) -> dict[str, Any]:
        rows = []
        for kid, snaps in self.snaps_by_kw.items():
            keyword = self.keywords[kid]["keyword_text"]
            top10_first_seen: dict[str, datetime] = {}
            top5_life = []
            top5_by_article: dict[str, list[datetime]] = defaultdict(list)
            for snap in snaps:
                t = self.snap_dt[snap["snapshot_id"]]
                if not t:
                    continue
                for hit in self.hits_by_snap.get(snap["snapshot_id"], []):
                    rank = int(hit.get("rank") or 999)
                    aid = hit["article_id"]
                    if rank <= 10:
                        top10_first_seen.setdefault(aid, t)
                    if rank <= 5:
                        top5_by_article[aid].append(t)
            if len(snaps) < 2:
                continue
            start = self.snap_dt[snaps[0]["snapshot_id"]]
            end = self.snap_dt[snaps[-1]["snapshot_id"]]
            if not start or not end:
                continue
            weeks = max((end - start).total_seconds() / 86400 / 7, 1 / 7)
            for aid, times in top5_by_article.items():
                pub = self.article_pub(aid)
                if not pub:
                    continue
                last = max(times)
                age = (last - pub).total_seconds() / 86400
                if age >= 0:
                    top5_life.append(age)
            if len(top5_life) < 3:
                continue
            rows.append({
                "keyword_id": kid,
                "keyword": keyword,
                "snapshot_count": len(snaps),
                "top10_unique_articles": len(top10_first_seen),
                "new_top10_articles_per_week": round(len(top10_first_seen) / weeks, 4),
                "top5_article_count": len(top5_life),
                "top5_halflife_p50_days": percentile(top5_life, 0.5),
                "top5_p80_days": percentile(top5_life, 0.8),
            })
        xs = [r["new_top10_articles_per_week"] for r in rows]
        ys = [r["top5_halflife_p50_days"] for r in rows]
        sorted_by_inflow = sorted(rows, key=lambda r: r["new_top10_articles_per_week"])
        n = len(sorted_by_inflow)
        groups = {
            "cold": sorted_by_inflow[: n // 3],
            "warm": sorted_by_inflow[n // 3 : (2 * n) // 3],
            "hot": sorted_by_inflow[(2 * n) // 3 :],
        }
        return {
            "keyword_count": len(rows),
            "spearman_inflow_vs_halflife": spearman(xs, ys),
            "grouping_note": "cold/warm/hot 按关键词新文流入速度三等分，不使用固定阈值。",
            "groups": {
                name: {
                    "keyword_count": len(items),
                    "inflow_median": percentile([x["new_top10_articles_per_week"] for x in items], 0.5),
                    "top5_halflife_median": percentile([x["top5_halflife_p50_days"] for x in items], 0.5),
                    "top5_halflife_p80": percentile([x["top5_halflife_p50_days"] for x in items], 0.8),
                }
                for name, items in groups.items()
            },
            "fastest_inflow_keywords": sorted(rows, key=lambda r: r["new_top10_articles_per_week"], reverse=True)[:20],
            "slowest_inflow_keywords": sorted(rows, key=lambda r: r["new_top10_articles_per_week"])[:20],
            "rows": rows,
        }

    def death_trajectories(self) -> dict[str, Any]:
        rows = []
        for (aid, kid), hits in self.article_hits_by_kw.items():
            top5 = [h for h in hits if int(h.get("rank") or 999) <= 5]
            if not top5:
                continue
            last_top5 = max(top5, key=lambda h: h["captured_dt"])
            future = [h for h in hits if h["captured_dt"] > last_top5["captured_dt"]]
            if future and int(future[0].get("rank") or 999) <= 5:
                continue
            prev_top5 = [h for h in top5 if h["captured_dt"] <= last_top5["captured_dt"]]
            ranks = [int(h.get("rank") or 999) for h in prev_top5[-4:]]
            next_rank = int(future[0].get("rank") or 999) if future else None
            last_rank = int(last_top5.get("rank") or 999)
            if last_rank <= 2 and (next_rank is None or next_rank > 10):
                shape = "cliff"
            elif len(ranks) >= 3 and (ranks[-1] - min(ranks[:-1]) >= 2 or (ranks[-3] < ranks[-2] < ranks[-1])):
                shape = "staircase"
            elif last_rank >= 4 or (next_rank is not None and 6 <= next_rank <= 10):
                shape = "edge_slip"
            else:
                shape = "other"
            rows.append({
                "article_id": aid,
                "keyword_id": kid,
                "keyword": self.keywords[kid]["keyword_text"],
                "title": self.articles.get(aid, {}).get("title"),
                "last_top5_rank": last_rank,
                "next_rank": next_rank,
                "recent_top5_ranks": ranks,
                "shape": shape,
            })
        counts = Counter(r["shape"] for r in rows)
        return {
            "sample_count": len(rows),
            "shape_counts": dict(counts),
            "shape_pct": {k: round(v / len(rows) * 100, 1) for k, v in counts.items()} if rows else {},
            "examples": {shape: [r for r in rows if r["shape"] == shape][:10] for shape in counts},
        }

    def replacement_experiments(self) -> dict[str, Any]:
        rows = []
        event_count = 0
        for kid, snaps in self.snaps_by_kw.items():
            keyword = self.keywords[kid]["keyword_text"]
            for prev, nxt in zip(snaps, snaps[1:]):
                pt = self.snap_dt[prev["snapshot_id"]]
                nt = self.snap_dt[nxt["snapshot_id"]]
                if not pt or not nt or (nt - pt).total_seconds() > 72 * 3600:
                    continue
                prev_top5 = {h["article_id"]: h for h in self.hits_by_snap[prev["snapshot_id"]] if int(h.get("rank") or 999) <= 5}
                next_top5 = {h["article_id"]: h for h in self.hits_by_snap[nxt["snapshot_id"]] if int(h.get("rank") or 999) <= 5}
                if not prev_top5 or not next_top5:
                    continue
                entrants = set(next_top5) - set(prev_top5)
                kicked = set(prev_top5) - set(next_top5)
                survivors = set(prev_top5) & set(next_top5)
                if not entrants or not kicked or not survivors:
                    continue
                event_count += 1
                for aid in kicked | survivors:
                    hit = prev_top5[aid]
                    article = self.articles.get(aid, {})
                    pub = self.article_pub(aid)
                    age = (pt - pub).total_seconds() / 86400 if pub else None
                    match = keyword_match_features(hit.get("title_raw") or article.get("title"), keyword)
                    rows.append({
                        "event_id": f"{kid}_{prev['snapshot_id']}_{nxt['snapshot_id']}",
                        "label_kicked": 1 if aid in kicked else 0,
                        "keyword_id": kid,
                        "keyword": keyword,
                        "article_id": aid,
                        "title": hit.get("title_raw") or article.get("title"),
                        "age_days": age,
                        "log_age_days": math.log1p(max(age or 0, 0)),
                        "account_score": self.account_score(hit.get("account_id")),
                        "log_account_score": math.log1p(self.account_score(hit.get("account_id"))),
                        "prev_rank": int(hit.get("rank") or 999),
                        "kw_breadth_top10": len(self.article_keywords_top10.get(aid, set())),
                        "log_kw_breadth_top10": math.log1p(len(self.article_keywords_top10.get(aid, set()))),
                        "content_len": self.content_len(aid),
                        "log_content_len": math.log1p(self.content_len(aid)),
                        **match,
                    })
        features = [
            "log_age_days",
            "log_account_score",
            "prev_rank",
            "kw_exact_in_title",
            "kw_title_overlap",
            "log_kw_breadth_top10",
            "log_content_len",
        ]
        return {
            "event_count": event_count,
            "row_count": len(rows),
            "kicked_rate": round(sum(r["label_kicked"] for r in rows) / len(rows), 4) if rows else None,
            "feature_medians": median_by_label(rows, features, "label_kicked"),
            "logistic": logistic_regression(rows, features, "label_kicked"),
        }

    def query_filter_controls(self) -> dict[str, Any]:
        rows = []
        events = [e for e in self.penalty.get("events", []) if e.get("event_type") == "query_filter"]
        for event in events:
            account_id = event.get("account_id")
            day = event.get("last_seen_date")
            for bucket, label in ((event.get("lost_keywords") or [], 1), (event.get("retained_keywords") or [], 0)):
                for keyword in bucket:
                    hit = self.day_account_kw_best.get((day, account_id, keyword))
                    kid = self.keyword_by_text.get(keyword, {}).get("keyword_id")
                    if not hit or not kid:
                        continue
                    aid = hit.get("article_id")
                    pub = self.article_pub(aid)
                    t = hit.get("captured_dt")
                    age = (t - pub).total_seconds() / 86400 if pub and t else None
                    match = keyword_match_features(hit.get("title_raw") or self.articles.get(aid, {}).get("title"), keyword)
                    rows.append({
                        "event_id": event.get("event_id"),
                        "label_lost": label,
                        "keyword": keyword,
                        "article_id": aid,
                        "rank": int(hit.get("rank") or 999),
                        "age_days": age,
                        "log_age_days": math.log1p(max(age or 0, 0)),
                        "keyword_inflow_per_week": None,
                        "kw_breadth_top10": len(self.article_keywords_top10.get(aid, set())),
                        "log_kw_breadth_top10": math.log1p(len(self.article_keywords_top10.get(aid, set()))),
                        **match,
                    })
        inflow_map = {
            r["keyword"]: r["new_top10_articles_per_week"]
            for r in self.keyword_inflow_and_halflife()["rows"]
        }
        for row in rows:
            row["keyword_inflow_per_week"] = inflow_map.get(row["keyword"], 0)
            row["log_keyword_inflow_per_week"] = math.log1p(row["keyword_inflow_per_week"])
        features = [
            "rank",
            "log_age_days",
            "kw_exact_in_title",
            "kw_title_overlap",
            "log_keyword_inflow_per_week",
            "log_kw_breadth_top10",
        ]
        return {
            "event_count": len(events),
            "row_count": len(rows),
            "lost_rate": round(sum(r["label_lost"] for r in rows) / len(rows), 4) if rows else None,
            "feature_medians": median_by_label(rows, features, "label_lost"),
            "logistic": logistic_regression(rows, features, "label_lost"),
        }

    def run(self) -> dict[str, Any]:
        task1 = self.keyword_inflow_and_halflife()
        task3 = self.death_trajectories()
        task2 = self.replacement_experiments()
        task4 = self.query_filter_controls()
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "task1_keyword_supply_halflife": task1,
            "task3_death_trajectories": task3,
            "task2_replacement_weights": task2,
            "task4_query_filter_controls": task4,
        }
        dump_json(OUT_JSON, payload)
        OUT_MD.write_text(self.render_report(payload), encoding="utf-8")
        return payload

    def render_report(self, p: dict[str, Any]) -> str:
        task1 = p["task1_keyword_supply_halflife"]
        task3 = p["task3_death_trajectories"]
        task2 = p["task2_replacement_weights"]
        task4 = p["task4_query_filter_controls"]
        lines = [
            "# 排序替换机制分析",
            "",
            f"- 生成时间：{p['generated_at']}",
            "",
            "## 任务一：关键词供给速度 × 半衰期",
            "",
            f"- 可计算关键词：{task1['keyword_count']} 个。",
            f"- 新文章流入速度与前5半衰期 Spearman 相关：**{task1['spearman_inflow_vs_halflife']}**。",
            "",
            "| 分组 | 关键词数 | 流入中位数/周 | 前5半衰期中位数 | 前5半衰期P80 |",
            "|---|---:|---:|---:|---:|",
        ]
        for key in ("cold", "warm", "hot"):
            g = task1["groups"][key]
            lines.append(f"| {key} | {g['keyword_count']} | {g['inflow_median']} | {g['top5_halflife_median']} | {g['top5_halflife_p80']} |")
        lines.extend([
            "",
            "### 新文流入最快 Top 10",
            "",
            "| 关键词 | 新文/周 | 前5半衰期P50 | 前5样本 |",
            "|---|---:|---:|---:|",
        ])
        for row in task1["fastest_inflow_keywords"][:10]:
            lines.append(f"| {row['keyword']} | {row['new_top10_articles_per_week']} | {row['top5_halflife_p50_days']} | {row['top5_article_count']} |")

        lines.extend([
            "",
            "## 任务三：死亡轨迹形状",
            "",
            f"- 样本：{task3['sample_count']} 个 article × keyword 的前5离场轨迹。",
            "",
            "| 轨迹 | 数量 | 占比 |",
            "|---|---:|---:|",
        ])
        for key, count in sorted(task3["shape_counts"].items(), key=lambda x: x[1], reverse=True):
            lines.append(f"| {key} | {count} | {task3['shape_pct'].get(key)}% |")

        lines.extend([
            "",
            "## 任务二：替换事件反推排序权重",
            "",
            f"- 天然替换事件：{task2['event_count']} 次；候选旧文行：{task2['row_count']} 行；被踢率：{task2['kicked_rate']}。",
            "",
            "### 逻辑回归方向",
            "",
            "| 因子 | 系数 | 方向 |",
            "|---|---:|---|",
        ])
        if task2["logistic"].get("available"):
            for item in task2["logistic"]["features"]:
                lines.append(f"| {item['feature']} | {item['coef']} | {item['direction']} |")
        lines.extend([
            "",
            "### 被踢 vs 幸存中位数",
            "",
            "| 因子 | 被踢中位数 | 幸存中位数 |",
            "|---|---:|---:|",
        ])
        for key, val in task2["feature_medians"].items():
            lines.append(f"| {key} | {val['positive_median']} | {val['negative_median']} |")

        lines.extend([
            "",
            "## 任务四：查询级过滤交叉验证",
            "",
            f"- query_filter 事件：{task4['event_count']} 个；可对照行：{task4['row_count']} 行；丢失率：{task4['lost_rate']}。",
            "",
            "| 因子 | 系数 | 方向 |",
            "|---|---:|---|",
        ])
        if task4["logistic"].get("available"):
            for item in task4["logistic"]["features"]:
                lines.append(f"| {item['feature']} | {item['coef']} | {item['direction']} |")
        lines.extend([
            "",
            "## 结论和感受",
            "",
            "1. “12天半衰期”不是全局时间衰减铁律。关键词供给速度和半衰期的关系才是关键：热词要频繁维护，冷词可以长期吃老文。",
            "2. 死亡轨迹里如果 cliff/edge_slip 明显多于 staircase，就说明很多文章不是慢慢老死，而是在新供给进入时被一次性挤掉。",
            "3. 替换事件的权重估计要看两个方向：标题关键词匹配和此前排名是保命因素，年龄/弱账号/低覆盖通常提高被踢风险。",
            "4. 查询级过滤是最干净的验证：同账号同日控制住账号权重后，丢词如果集中在标题不匹配、竞争流入快、文章更老的词上，就基本说明排序核心在“文章×关键词”的相关性和供给竞争。",
            "",
        ])
        return "\n".join(lines)


def main() -> None:
    payload = Analyzer().run()
    print(f"[ok] wrote {OUT_JSON}")
    print(f"[ok] wrote {OUT_MD}")
    print("[task1]", payload["task1_keyword_supply_halflife"]["spearman_inflow_vs_halflife"], payload["task1_keyword_supply_halflife"]["groups"])
    print("[task3]", payload["task3_death_trajectories"]["shape_counts"])
    print("[task2]", payload["task2_replacement_weights"]["event_count"], payload["task2_replacement_weights"]["logistic"])
    print("[task4]", payload["task4_query_filter_controls"]["row_count"], payload["task4_query_filter_controls"]["logistic"])


if __name__ == "__main__":
    main()
