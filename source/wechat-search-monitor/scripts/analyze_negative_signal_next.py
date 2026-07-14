#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "临时产物" / "260704_negative_signal_next_steps.json"
OUT_MD = ROOT / "临时产物" / "260704_negative_signal_next_steps.md"


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


def safe_median(values: list[float | int | None]) -> float | None:
    xs = [x for x in values if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return round(float(median(xs)), 3) if xs else None


def safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def pct(num: float, den: float) -> float:
    return round(safe_div(num, den) * 100, 1)


def normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def sim(a: str | None, b: str | None) -> float:
    aa = normalize_text(a)
    bb = normalize_text(b)
    if not aa or not bb:
        return 0.0
    return SequenceMatcher(None, aa, bb).ratio()


def compact(text: str | None, n: int = 80) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def count_terms(text: str | None, terms: list[str]) -> int:
    text = text or ""
    return sum(text.count(term) for term in terms)


def read_content_text(article: dict[str, Any]) -> tuple[str, int]:
    path = article.get("content_file_path")
    if not path:
        return "", 0
    fp = ROOT / path
    if not fp.exists():
        return "", 0
    md = fp.read_text(encoding="utf-8", errors="ignore")
    image_count = sum(1 for line in md.splitlines() if line.strip().startswith("!["))
    lines = []
    for line in md.splitlines():
        if line.strip().startswith("![") or re.match(r"^https?://", line.strip()):
            continue
        line = re.sub(r"!\[[^\]]*]\([^)]*\)", "", line)
        line = re.sub(r"\[[^\]]*]\([^)]*\)", "", line)
        line = re.sub(r"[#>*_`|~-]+", "", line)
        lines.append(line)
    return re.sub(r"\s+", "", "".join(lines)), image_count


def title_similarity_max(titles: list[str]) -> float:
    if len(titles) < 2:
        return 0.0
    best = 0.0
    for i, left in enumerate(titles):
        for right in titles[i + 1 :]:
            best = max(best, sim(left, right))
    return round(best, 3)


class Analyzer:
    def __init__(self) -> None:
        from app.repositories.keyword_registry_repo import KeywordRegistryRepository
        self.keywords = {
            x["keyword_id"]: x
            for x in KeywordRegistryRepository(ROOT / "data/state/app.db").list_keywords()
        }
        self.accounts = {x["account_id"]: x for x in load_json(ROOT / "normalized" / "accounts.json")}
        self.articles = {x["article_id"]: x for x in load_json(ROOT / "normalized" / "articles.json")}
        self.snapshots = load_json(ROOT / "normalized" / "snapshots.json")
        self.hits = load_json(ROOT / "normalized" / "ranking_hits.json")
        self.monitor = load_json(ROOT / "normalized" / "monitor-data.json")
        self.aliases = load_json(ROOT / "normalized" / "account_aliases.json")
        self.probe_results = load_json(ROOT / "临时产物" / "260704_negative_probe_results.json")
        self.b_events = load_json(ROOT / "临时产物" / "260704_B_account_events.json")

        self.monitor_account = {
            x["account_id"]: x
            for x in self.monitor.get("accounts", [])
        }
        self.snap_by_id = {x["snapshot_id"]: x for x in self.snapshots if x.get("status") == "success"}
        self.snap_dt = {sid: parse_dt(s["captured_at"]) for sid, s in self.snap_by_id.items()}
        self.snap_kw = {sid: s["keyword_id"] for sid, s in self.snap_by_id.items()}
        self.snaps_by_kw: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for snap in self.snap_by_id.values():
            if snap.get("result_count", 0) >= 3:
                self.snaps_by_kw[snap["keyword_id"]].append(snap)
        for rows in self.snaps_by_kw.values():
            rows.sort(key=lambda x: x["captured_at"])

        self.hits_by_snap: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for hit in self.hits:
            if hit["snapshot_id"] in self.snap_by_id:
                self.hits_by_snap[hit["snapshot_id"]].append(hit)
        for rows in self.hits_by_snap.values():
            rows.sort(key=lambda x: int(x.get("rank") or 999))

        self.appearances: dict[tuple[str, str], list[datetime]] = defaultdict(list)
        for hit in self.hits:
            sid = hit["snapshot_id"]
            kid = self.snap_kw.get(sid)
            t = self.snap_dt.get(sid)
            if kid and t:
                self.appearances[(kid, hit["article_id"])].append(t)
        for rows in self.appearances.values():
            rows.sort()

    def account_name(self, account_id: str | None) -> str:
        if not account_id:
            return ""
        return self.accounts.get(account_id, {}).get("canonical_name") or account_id

    def account_score(self, account_id: str | None) -> float:
        if not account_id:
            return 0.0
        return float(self.monitor_account.get(account_id, {}).get("score") or 0)

    def article_pub_dt(self, article_id: str | None) -> datetime | None:
        if not article_id:
            return None
        article = self.articles.get(article_id, {})
        return parse_dt(article.get("published_at"))

    def hard_drop_pairs(self) -> list[dict[str, Any]]:
        events: dict[tuple[str, str], dict[str, Any]] = {}
        top5_history: dict[tuple[str, str], list[datetime]] = defaultdict(list)
        for kid, snaps in self.snaps_by_kw.items():
            for prev, nxt in zip(snaps, snaps[1:]):
                pt = parse_dt(prev.get("captured_at"))
                nt = parse_dt(nxt.get("captured_at"))
                if not pt or not nt or (nt - pt).total_seconds() > 72 * 3600:
                    continue
                prev_rows = {h["article_id"]: h for h in self.hits_by_snap[prev["snapshot_id"]]}
                next_rows = {h["article_id"]: h for h in self.hits_by_snap[nxt["snapshot_id"]]}
                for aid, hit in prev_rows.items():
                    if int(hit.get("rank") or 999) > 5:
                        continue
                    key = (kid, aid)
                    previous_top5_count = len(top5_history[key])
                    top5_history[key].append(pt)
                    if aid in next_rows:
                        continue
                    future = [
                        t for t in self.appearances[key]
                        if nt < t <= nt + timedelta(days=7)
                    ]
                    if future:
                        continue
                    if key in events:
                        continue
                    article = self.articles.get(aid, {})
                    pub = self.article_pub_dt(aid)
                    age_days = round((pt - pub).total_seconds() / 86400, 3) if pub else None
                    events[key] = {
                        "keyword_id": kid,
                        "keyword": self.keywords.get(kid, {}).get("keyword_text") or kid,
                        "article_id": aid,
                        "account_id": hit.get("account_id"),
                        "account": self.account_name(hit.get("account_id")),
                        "title": hit.get("title_raw") or article.get("title") or "",
                        "summary": hit.get("summary_raw") or article.get("summary") or "",
                        "prev_snapshot_id": prev["snapshot_id"],
                        "next_snapshot_id": nxt["snapshot_id"],
                        "prev_time": prev["captured_at"],
                        "next_time": nxt["captured_at"],
                        "prev_rank": int(hit.get("rank") or 999),
                        "published_at": article.get("published_at"),
                        "age_days": age_days,
                        "previous_top5_count": previous_top5_count,
                    }
        return list(events.values())

    def replacement_for(self, event: dict[str, Any]) -> dict[str, Any]:
        original_pub = self.article_pub_dt(event["article_id"])
        original_score = self.account_score(event["account_id"])
        best: dict[str, Any] | None = None
        for row in self.hits_by_snap.get(event["next_snapshot_id"], []):
            aid = row["article_id"]
            if aid == event["article_id"]:
                continue
            article = self.articles.get(aid, {})
            pub = self.article_pub_dt(aid)
            rank_delta = abs(int(row.get("rank") or 999) - int(event["prev_rank"]))
            title_sim = sim(event["title"], row.get("title_raw") or article.get("title"))
            summary_sim = sim(event.get("summary"), row.get("summary_raw") or article.get("summary"))
            newer = bool(pub and original_pub and pub > original_pub)
            position_score = max(0.0, 1 - min(rank_delta, 6) / 6)
            score = title_sim * 0.5 + summary_sim * 0.15 + position_score * 0.2
            if newer:
                score += 0.1
            if row.get("account_id") == event["account_id"]:
                score += 0.08
            candidate = {
                "article_id": aid,
                "account_id": row.get("account_id"),
                "account": self.account_name(row.get("account_id")),
                "title": row.get("title_raw") or article.get("title") or "",
                "rank": int(row.get("rank") or 999),
                "rank_delta": rank_delta,
                "published_at": article.get("published_at"),
                "is_newer": newer,
                "same_account": row.get("account_id") == event["account_id"],
                "title_sim": round(title_sim, 3),
                "summary_sim": round(summary_sim, 3),
                "score": round(score, 3),
                "account_score": self.account_score(row.get("account_id")),
                "original_account_score": original_score,
                "replacer_lower_score": self.account_score(row.get("account_id")) < original_score,
            }
            if not best or candidate["score"] > best["score"]:
                best = candidate
        if not best:
            return {"replacement_type": "no_next_rows"}
        if best["same_account"] and best["is_newer"]:
            kind = "same_account_update"
        elif best["is_newer"] and best["title_sim"] >= 0.72:
            kind = "near_duplicate_fresh_copy"
        elif best["is_newer"] and (best["title_sim"] >= 0.35 or best["rank_delta"] <= 2):
            kind = "competitor_newer_article"
        elif best["title_sim"] < 0.22 and best["rank_delta"] <= 2:
            kind = "query_intent_shift_or_unrelated_replacement"
        else:
            kind = "unclear_replacement"
        best["replacement_type"] = kind
        return best

    def alias_impact(self) -> dict[str, Any]:
        rows = []
        for alias in self.aliases.get("aliases", []):
            src = alias.get("source_account_id")
            tgt = alias.get("target_account_id")
            src_m = self.monitor_account.get(src, {})
            tgt_m = self.monitor_account.get(tgt, {})
            merged_score = round(float(src_m.get("score") or 0) + float(tgt_m.get("score") or 0), 2)
            rows.append({
                "source_account_id": src,
                "source_account": alias.get("source_account_name"),
                "target_account_id": tgt,
                "target_account": alias.get("target_account_name"),
                "status": alias.get("status"),
                "confidence": alias.get("confidence"),
                "shared_title_count": alias.get("shared_title_count"),
                "shared_url_count": alias.get("shared_url_count"),
                "gap_days": alias.get("gap_days"),
                "source_score": round(float(src_m.get("score") or 0), 2),
                "target_score": round(float(tgt_m.get("score") or 0), 2),
                "merged_score_proxy": merged_score,
                "target_hit_days": tgt_m.get("hit_days"),
                "target_current_streak": tgt_m.get("current_streak"),
                "blackhorse_pollution_risk": bool((tgt_m.get("score") or 0) > 30 and (tgt_m.get("hit_days") or 0) >= 7),
            })
        return {
            "alias_count": len(rows),
            "confirmed_count": sum(1 for x in rows if x["status"] == "rpa_confirmed_alias"),
            "strong_count": sum(1 for x in rows if x["status"] == "strong_alias"),
            "likely_count": sum(1 for x in rows if x["status"] == "likely_alias"),
            "rows": rows,
        }

    def content_signal(self, article_id: str, title: str) -> dict[str, Any]:
        article = self.articles.get(article_id, {})
        body, img = read_content_text(article)
        text = title + "\n" + (article.get("summary") or "") + "\n" + body
        compliance = ["保费融资", "杠杆", "高杠杆", "收益承诺", "保证收益", "预期收益", "返佣", "自购", "违规", "合规", "监管", "处罚", "举报", "压力测试", "资产证明"]
        time_terms = ["限时", "倒计时", "最后", "窗口期", "停售", "退市", "关停", "即将", "优惠"]
        cta_terms = ["咨询", "联系", "扫码", "二维码", "私信", "预约", "福利", "优惠", "领取", "添加", "微信", "客服", "投保", "配置", "方案"]
        click_terms = ["王炸", "爆", "重磅", "必看", "天花板", "震撼", "疯狂", "刷屏", "真香", "逆天", "封神", "千万"]
        return {
            "text_len": len(body),
            "image_count": img,
            "compliance_terms": count_terms(text, compliance),
            "time_terms": count_terms(text, time_terms),
            "cta_terms": count_terms(text, cta_terms),
            "click_terms": count_terms(text, click_terms),
            "image_heavy": img >= 8 and len(body) < 2500,
            "thin": len(body) < 800 if body else None,
        }

    def summarize_old_stable_rows(self, enriched: list[dict[str, Any]]) -> dict[str, Any]:
        replacement_counts = Counter(x["replacement"].get("replacement_type") for x in enriched)
        newer_count = sum(1 for x in enriched if x["replacement"].get("is_newer"))
        lower_score_count = sum(1 for x in enriched if x["replacement"].get("replacer_lower_score"))
        same_account_count = sum(1 for x in enriched if x["replacement"].get("same_account"))
        return {
            "count": len(enriched),
            "age_days_median": safe_median([x.get("age_days") for x in enriched]),
            "previous_top5_count_median": safe_median([x.get("previous_top5_count") for x in enriched]),
            "replacement_type_counts": dict(replacement_counts),
            "newer_replacement_count": newer_count,
            "newer_replacement_pct": pct(newer_count, len(enriched)),
            "lower_score_replacement_count": lower_score_count,
            "lower_score_replacement_pct": pct(lower_score_count, len(enriched)),
            "same_account_replacement_count": same_account_count,
            "same_account_replacement_pct": pct(same_account_count, len(enriched)),
            "rows": enriched,
        }

    def analyze_old_stable_drops(self, hard_pairs: list[dict[str, Any]]) -> dict[str, Any]:
        old_stable = [
            x for x in hard_pairs
            if (x.get("age_days") is not None and x["age_days"] > 7 and x.get("previous_top5_count", 0) >= 2)
        ]
        enriched = []
        for event in old_stable:
            replacement = self.replacement_for(event)
            signal = self.content_signal(event["article_id"], event["title"])
            row = {
                **event,
                "replacement": replacement,
                "content_signal": signal,
            }
            enriched.append(row)
        enriched.sort(key=lambda x: (x.get("age_days") or 0, x.get("previous_top5_count") or 0), reverse=True)
        summary = self.summarize_old_stable_rows(enriched)
        core = [x for x in enriched if x.get("previous_top5_count", 0) >= 13]
        summary["core_threshold"] = {
            "age_days_gt": 7,
            "previous_top5_count_gte": 13,
            "note": "人工审查核心池；数量与上一轮 72 条老文暴毙口径同一量级。",
        }
        summary["core"] = self.summarize_old_stable_rows(core)
        return summary

    def analyze_c_mechanism(self, hard_pairs: list[dict[str, Any]]) -> dict[str, Any]:
        rows = []
        for event in hard_pairs:
            replacement = self.replacement_for(event)
            if replacement.get("replacement_type") in {
                "same_account_update",
                "near_duplicate_fresh_copy",
                "competitor_newer_article",
            }:
                rows.append({**event, "replacement": replacement})
        type_counts = Counter(x["replacement"]["replacement_type"] for x in rows)
        lower = sum(1 for x in rows if x["replacement"].get("replacer_lower_score"))
        same = sum(1 for x in rows if x["replacement"].get("same_account"))
        return {
            "count": len(rows),
            "replacement_type_counts": dict(type_counts),
            "lower_score_replacement_count": lower,
            "lower_score_replacement_pct": pct(lower, len(rows)),
            "same_account_update_count": same,
            "same_account_update_pct": pct(same, len(rows)),
            "age_days_median": safe_median([x.get("age_days") for x in rows]),
            "rows_sample": sorted(rows, key=lambda x: x["replacement"].get("score", 0), reverse=True)[:80],
        }

    def analyze_b_accounts(self) -> dict[str, Any]:
        rows = []
        articles_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for article in self.articles.values():
            articles_by_account[article.get("account_id")].append(article)
        for event in self.b_events:
            start = parse_dt(event.get("start"))
            end = parse_dt(event.get("end"))
            account_id = event["account_id"]
            window_articles = []
            if start and end:
                left = start - timedelta(days=3)
                right = end + timedelta(hours=1)
                for article in articles_by_account.get(account_id, []):
                    pub = parse_dt(article.get("published_at")) or parse_dt(article.get("first_seen_at"))
                    if pub and left <= pub <= right:
                        window_articles.append(article)
            titles = [x.get("title") or "" for x in window_articles]
            combined_signal = Counter()
            for article in window_articles:
                sig = self.content_signal(article["article_id"], article.get("title") or "")
                combined_signal.update({
                    "compliance_terms": sig["compliance_terms"],
                    "time_terms": sig["time_terms"],
                    "cta_terms": sig["cta_terms"],
                    "click_terms": sig["click_terms"],
                    "image_heavy_articles": 1 if sig["image_heavy"] else 0,
                    "thin_articles": 1 if sig["thin"] else 0,
                })
            gap = event.get("return_gap_days")
            if gap is None:
                return_bucket = "not_returned"
            elif gap <= 1:
                return_bucket = "0-1d_return"
            elif gap <= 3:
                return_bucket = "1-3d_return"
            elif gap <= 7:
                return_bucket = "3-7d_return"
            else:
                return_bucket = "7d_plus_or_not_observed"
            if return_bucket == "not_returned" or (gap is not None and gap > 7):
                interpretation = "弱号冲榜后未回归或观察窗不足，优先看是否主题断供/账号身份变化"
            elif gap <= 1:
                interpretation = "短时多词归零后快速回归，更像查询波动/新鲜度轮换，不像处罚"
            else:
                interpretation = "存在短期冷却感，值得继续观察"
            rows.append({
                **event,
                "return_bucket": return_bucket,
                "window_article_count": len(window_articles),
                "window_titles": titles[:8],
                "title_similarity_max_3d": title_similarity_max(titles),
                "content_signal_3d": dict(combined_signal),
                "interpretation": interpretation,
            })
        return {
            "event_count": len(rows),
            "return_bucket_counts": dict(Counter(x["return_bucket"] for x in rows)),
            "not_returned_or_7d_plus_count": sum(1 for x in rows if x["return_bucket"] in {"not_returned", "7d_plus_or_not_observed"}),
            "score_median_by_bucket": {
                bucket: safe_median([x.get("score") for x in rows if x["return_bucket"] == bucket])
                for bucket in sorted(set(x["return_bucket"] for x in rows))
            },
            "rows": rows,
        }

    def report_md(self, payload: dict[str, Any]) -> str:
        alias = payload["alias_impact"]
        old = payload["old_stable_drops"]
        core = old["core"]
        c = payload["c_mechanism"]
        b = payload["b_account_events"]

        lines = [
            "# 260704 排名消失后续分析",
            "",
            "> 核心判断：URL 探测已经基本排除“违规/删文是主因”；下一步主线应转向搜索寿命、更新替换和账号身份归并。",
            "",
            "## 1. 账号别名污染",
            "",
            f"- 别名链：{alias['alias_count']} 条；confirmed={alias['confirmed_count']}，strong={alias['strong_count']}，likely={alias['likely_count']}。",
        ]
        for row in alias["rows"]:
            lines.append(
                f"- {row['source_account']} -> {row['target_account']}：{row['status']}，置信度 {row['confidence']}，"
                f"共享标题/URL={row['shared_title_count']}/{row['shared_url_count']}，目标账号分 {row['target_score']}，"
                f"黑马污染风险={'是' if row['blackhorse_pollution_risk'] else '否'}。"
            )

        lines.extend([
            "",
            "## 2. 老文暴毙集合",
            "",
            f"- 宽口径：硬消失 pair 中，文章年龄 >7 天且此前至少 2 次前五在榜；当前得到 {old['count']} 条。",
            f"- 年龄中位数：{old['age_days_median']} 天；此前前五命中中位数：{old['previous_top5_count_median']} 次。",
            f"- 下一快照出现更新替换者：{old['newer_replacement_count']} 条，占 {old['newer_replacement_pct']}%。",
            f"- 替换者账号分低于原文：{old['lower_score_replacement_count']} 条，占 {old['lower_score_replacement_pct']}%。",
            f"- 同账号更新/重发：{old['same_account_replacement_count']} 条，占 {old['same_account_replacement_pct']}%。",
            "",
            f"- 核心人工池：文章年龄 >7 天且此前至少 13 次前五在榜；当前得到 {core['count']} 条。",
            f"- 核心池年龄中位数：{core['age_days_median']} 天；此前前五命中中位数：{core['previous_top5_count_median']} 次。",
            f"- 核心池更新替换：{core['newer_replacement_count']} 条，占 {core['newer_replacement_pct']}%；低分替换高分：{core['lower_score_replacement_count']} 条，占 {core['lower_score_replacement_pct']}%。",
            "",
            "| 替换类型 | 数量 |",
            "|---|---:|",
        ])
        for key, val in sorted(old["replacement_type_counts"].items(), key=lambda x: x[1], reverse=True):
            lines.append(f"| {key} | {val} |")

        lines.extend([
            "",
            "### 核心老文样本 Top 12",
            "",
            "| 年龄 | 词 | 原账号 | 原标题 | 替换类型 | 替换账号 | 替换标题 |",
            "|---:|---|---|---|---|---|---|",
        ])
        for row in core["rows"][:12]:
            repl = row["replacement"]
            lines.append(
                f"| {round(row.get('age_days') or 0, 1)} | {row['keyword']} | {row['account']} | {compact(row['title'], 30)} | "
                f"{repl.get('replacement_type','')} | {repl.get('account','')} | {compact(repl.get('title'), 30)} |"
            )

        lines.extend([
            "",
            "## 3. C 类替换机制",
            "",
            f"- 全部硬消失 pair 中，可识别为更新替换的有 {c['count']} 条。",
            f"- 低分账号顶掉高分原文：{c['lower_score_replacement_count']} 条，占 {c['lower_score_replacement_pct']}%。",
            f"- 同账号更新/重发：{c['same_account_update_count']} 条，占 {c['same_account_update_pct']}%。",
            "",
            "| 类型 | 数量 |",
            "|---|---:|",
        ])
        for key, val in sorted(c["replacement_type_counts"].items(), key=lambda x: x[1], reverse=True):
            lines.append(f"| {key} | {val} |")

        lines.extend([
            "",
            "## 4. B 类账号事件",
            "",
            f"- 账号事件：{b['event_count']} 个；7天后/未观察到回归：{b['not_returned_or_7d_plus_count']} 个。",
            "",
            "| 回归状态 | 数量 | 分数中位数 |",
            "|---|---:|---:|",
        ])
        for key, val in sorted(b["return_bucket_counts"].items()):
            lines.append(f"| {key} | {val} | {b['score_median_by_bucket'].get(key)} |")
        lines.extend([
            "",
            "### 未回归/长期未回归账号优先看",
            "",
            "| 账号 | 分数 | 词/文 | 回归 | 3天发文 | 标题相似度 | 判断 |",
            "|---|---:|---:|---|---:|---:|---|",
        ])
        watch_rows = [
            x for x in b["rows"]
            if x["return_bucket"] in {"not_returned", "7d_plus_or_not_observed"}
        ]
        watch_rows.sort(key=lambda x: (x.get("score") or 0), reverse=True)
        for row in watch_rows[:18]:
            lines.append(
                f"| {row['account']} | {round(row.get('score') or 0, 2)} | {row.get('kws')}/{row.get('articles')} | "
                f"{row['return_bucket']} | {row['window_article_count']} | {row['title_similarity_max_3d']} | {row['interpretation']} |"
            )

        lines.extend([
            "",
            "## 行动判断",
            "",
            "1. 先把 confirmed/strong alias 做榜单归并或污染提示；写入事实层前保留原账号身份，避免破坏原始抓取事实。",
            "2. 老文暴毙不是处罚主导，优先沉淀“第几天会老、被什么新内容替换”的寿命规则。",
            "3. 同账号重发占比仍低，不能直接定铁律；需要单独追踪重发后的恢复天数。",
            "4. B 类账号多数快速回归，未回归账号更像弱号出清；只把高分、长期未回归、跨多文多词的账号列入疑似降权观察池。",
        ])
        return "\n".join(lines) + "\n"

    def run(self) -> dict[str, Any]:
        hard_pairs = self.hard_drop_pairs()
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "hard_pair_count": len(hard_pairs),
            "alias_impact": self.alias_impact(),
            "old_stable_drops": self.analyze_old_stable_drops(hard_pairs),
            "c_mechanism": self.analyze_c_mechanism(hard_pairs),
            "b_account_events": self.analyze_b_accounts(),
        }
        dump_json(OUT_JSON, payload)
        OUT_MD.write_text(self.report_md(payload), encoding="utf-8")
        return payload


def main() -> None:
    payload = Analyzer().run()
    print(f"hard_pair_count={payload['hard_pair_count']}")
    print(f"old_stable_drops={payload['old_stable_drops']['count']}")
    print(f"c_replacements={payload['c_mechanism']['count']}")
    print(f"b_events={payload['b_account_events']['event_count']}")
    print(f"wrote {OUT_JSON}")
    print(f"wrote {OUT_MD}")


if __name__ == "__main__":
    main()
