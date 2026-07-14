#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def normalize_title(title: str | None) -> str:
    return re.sub(r"\s+", "", title or "").lower()


def is_placeholder_url(url: str | None) -> bool:
    return not url or str(url).startswith("placeholder://")


def date_part(value: str | None) -> str | None:
    if not value:
        return None
    return value[:10]


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value[:10])


def days_between(a: str | None, b: str | None) -> int | None:
    da = parse_date(a)
    db = parse_date(b)
    if not da or not db:
        return None
    return (db - da).days


def safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, a: str, b: str) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra != rb:
            self.parent[rb] = ra


class AccountAliasBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.accounts = load_json(root / "normalized" / "accounts.json")
        self.articles = load_json(root / "normalized" / "articles.json")
        self.hits = load_json(root / "normalized" / "ranking_hits.json")
        self.snapshots = load_json(root / "normalized" / "snapshots.json")
        self.monitor = load_json(root / "normalized" / "monitor-data.json")
        self.rpa_checks = self._load_rpa_checks()

        self.account_name = {
            a["account_id"]: a.get("canonical_name") or a["account_id"]
            for a in self.accounts
        }
        self.account_seen = {
            a["account_id"]: {
                "first_seen_at": a.get("first_seen_at"),
                "last_seen_at": a.get("last_seen_at"),
            }
            for a in self.accounts
        }
        self.account_headimg = {
            a["account_id"]: a.get("headimg_url")
            for a in self.accounts
            if a.get("headimg_url")
        }
        self.snap_by_id = {
            s["snapshot_id"]: s
            for s in self.snapshots
            if s.get("status") == "success"
        }
        self.article_by_id = {a["article_id"]: a for a in self.articles}
        self.article_title_key = {
            a["article_id"]: normalize_title(a.get("title"))
            for a in self.articles
        }
        self.monitor_by_account = {
            a.get("account_id"): a
            for a in self.monitor.get("accounts", [])
        }

        self.account_titles: dict[str, set[str]] = defaultdict(set)
        self.account_urls: dict[str, set[str]] = defaultdict(set)
        self.account_articles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.title_accounts: dict[str, set[str]] = defaultdict(set)
        self.url_accounts: dict[str, set[str]] = defaultdict(set)
        self.headimg_accounts: dict[str, set[str]] = defaultdict(set)
        self.account_title_rank_points: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self._build_indexes()

    def _load_rpa_checks(self) -> dict[str, Any]:
        path = self.root / "data" / "state" / "penalty_rpa_title_checks.json"
        if not path.exists():
            return {"checks": []}
        return load_json(path)

    def _build_indexes(self) -> None:
        for account_id, headimg_url in self.account_headimg.items():
            self.headimg_accounts[headimg_url].add(account_id)

        for article in self.articles:
            account_id = article.get("account_id")
            title_key = normalize_title(article.get("title"))
            if not account_id or not title_key:
                continue
            self.account_titles[account_id].add(title_key)
            self.title_accounts[title_key].add(account_id)
            self.account_articles[account_id].append(article)
            url = article.get("normalized_url")
            if not is_placeholder_url(url):
                self.account_urls[account_id].add(url)
                self.url_accounts[url].add(account_id)

        for hit in self.hits:
            snap = self.snap_by_id.get(hit.get("snapshot_id"))
            if not snap:
                continue
            title_key = self.article_title_key.get(hit.get("article_id")) or normalize_title(hit.get("title_raw"))
            account_id = hit.get("account_id")
            if not account_id or not title_key:
                continue
            self.account_title_rank_points[(account_id, title_key)].append({
                "date": snap.get("snapshot_date"),
                "keyword_id": snap.get("keyword_id"),
                "rank": int(hit.get("rank") or 999),
            })

    def rank_continuity(self, old_id: str, new_id: str, shared_titles: set[str]) -> dict[str, Any]:
        deltas = []
        samples = []
        for title_key in shared_titles:
            old_points = sorted(self.account_title_rank_points.get((old_id, title_key), []), key=lambda p: (p["date"], p["rank"]))
            new_points = sorted(self.account_title_rank_points.get((new_id, title_key), []), key=lambda p: (p["date"], p["rank"]))
            if not old_points or not new_points:
                continue
            old_last_day = max(p["date"] for p in old_points if p.get("date"))
            new_first_day = min(p["date"] for p in new_points if p.get("date"))
            old_best = min(p["rank"] for p in old_points if p["date"] == old_last_day)
            new_best = min(p["rank"] for p in new_points if p["date"] == new_first_day)
            delta = new_best - old_best
            deltas.append(abs(delta))
            title = next((a.get("title") for a in self.account_articles[old_id] if normalize_title(a.get("title")) == title_key), title_key)
            samples.append({
                "title": title,
                "old_last_date": old_last_day,
                "old_best_rank": old_best,
                "new_first_date": new_first_day,
                "new_best_rank": new_best,
                "rank_delta": delta,
            })
        return {
            "rank_sample_count": len(deltas),
            "avg_abs_rank_delta": round(sum(deltas) / len(deltas), 3) if deltas else None,
            "samples": sorted(samples, key=lambda x: abs(x["rank_delta"]))[:10],
        }

    def candidate_score(
        self,
        old_id: str,
        new_id: str,
        shared_titles: set[str],
        shared_urls: set[str],
        shared_headimg: bool,
        gap_days: int | None,
        rank_continuity: dict[str, Any],
        rpa_refuted_deindex: bool,
    ) -> tuple[int, str]:
        old_title_count = len(self.account_titles.get(old_id, set()))
        new_title_count = len(self.account_titles.get(new_id, set()))
        overlap_min = safe_div(len(shared_titles), min(old_title_count, new_title_count))
        score = 0.0
        score += min(40, len(shared_titles) * 5)
        score += min(24, len(shared_urls) * 8)
        if shared_headimg:
            score += 42
        score += min(20, overlap_min * 35)
        if gap_days is not None:
            if 0 <= gap_days <= 21:
                score += 20
            elif -3 <= gap_days < 0 or 22 <= gap_days <= 45:
                score += 10
            elif 46 <= gap_days <= 90:
                score += 4
        avg_delta = rank_continuity.get("avg_abs_rank_delta")
        if avg_delta is not None:
            if avg_delta <= 2:
                score += 10
            elif avg_delta <= 5:
                score += 5
        if SequenceMatcher(None, self.account_name.get(old_id, ""), self.account_name.get(new_id, "")).ratio() >= 0.5:
            score += 3
        if rpa_refuted_deindex:
            score = max(score, 96)

        confidence = max(0, min(100, round(score)))
        if rpa_refuted_deindex:
            status = "rpa_confirmed_alias"
        elif shared_headimg and confidence >= 82 and (shared_titles or shared_urls) and gap_days is not None and -3 <= gap_days <= 45:
            status = "strong_alias"
        elif shared_headimg and confidence >= 72 and gap_days is not None and -3 <= gap_days <= 45:
            status = "likely_alias"
        elif confidence >= 82 and len(shared_titles) >= 3 and (shared_urls or overlap_min >= 0.35):
            status = "strong_alias"
        elif confidence >= 68 and len(shared_titles) >= 3:
            status = "likely_alias"
        else:
            status = "weak_candidate"
        return confidence, status

    def rpa_alias_pairs(self) -> set[tuple[str, str]]:
        by_name = {v: k for k, v in self.account_name.items()}
        pairs = set()
        checks = self.rpa_checks.get("checks", []) if isinstance(self.rpa_checks, dict) else []
        for check in checks:
            if check.get("interpretation") != "exact_title_found_under_sibling_account":
                continue
            old_id = by_name.get(check.get("target_account_name"))
            new_id = by_name.get(check.get("exact_account_name"))
            if old_id and new_id and old_id != new_id:
                pairs.add((old_id, new_id))
        return pairs

    def build_candidates(self, min_shared_titles: int, max_gap_days: int) -> list[dict[str, Any]]:
        pair_shared_titles: dict[tuple[str, str], set[str]] = defaultdict(set)
        for title_key, accounts in self.title_accounts.items():
            if len(accounts) < 2 or len(accounts) > 20:
                continue
            ids = sorted(accounts)
            for i, old_id in enumerate(ids):
                for new_id in ids[i + 1:]:
                    pair_shared_titles[(old_id, new_id)].add(title_key)

        pair_shared_urls: dict[tuple[str, str], set[str]] = defaultdict(set)
        for url, accounts in self.url_accounts.items():
            if len(accounts) < 2 or len(accounts) > 10:
                continue
            ids = sorted(accounts)
            for i, old_id in enumerate(ids):
                for new_id in ids[i + 1:]:
                    pair_shared_urls[(old_id, new_id)].add(url)

        pair_shared_headimg: dict[tuple[str, str], str] = {}
        for headimg_url, accounts in self.headimg_accounts.items():
            if len(accounts) < 2 or len(accounts) > 5:
                continue
            ids = sorted(accounts)
            for i, old_id in enumerate(ids):
                for new_id in ids[i + 1:]:
                    pair_shared_headimg[(old_id, new_id)] = headimg_url

        rpa_pairs = self.rpa_alias_pairs()
        candidates = []
        seen_directed: set[tuple[str, str]] = set()
        candidate_pairs = set(pair_shared_titles) | set(pair_shared_urls) | set(pair_shared_headimg)
        for pair in sorted(candidate_pairs):
            shared_titles = pair_shared_titles.get(pair, set())
            a, b = pair
            for old_id, new_id in ((a, b), (b, a)):
                if (old_id, new_id) in seen_directed:
                    continue
                seen_directed.add((old_id, new_id))
                old_last = self.account_seen.get(old_id, {}).get("last_seen_at")
                new_first = self.account_seen.get(new_id, {}).get("first_seen_at")
                gap = days_between(date_part(old_last), date_part(new_first))
                if gap is None:
                    continue
                rpa_refuted = (old_id, new_id) in rpa_pairs
                shared_urls = self.account_urls.get(old_id, set()) & self.account_urls.get(new_id, set())
                shared_headimg_url = (
                    self.account_headimg.get(old_id)
                    if self.account_headimg.get(old_id)
                    and self.account_headimg.get(old_id) == self.account_headimg.get(new_id)
                    else None
                )
                shared_headimg = bool(shared_headimg_url)
                if not rpa_refuted and (gap < -3 or gap > max_gap_days):
                    continue
                if not rpa_refuted and not shared_headimg and len(shared_titles) < min_shared_titles:
                    continue
                old_count = len(self.account_titles.get(old_id, set()))
                new_count = len(self.account_titles.get(new_id, set()))
                overlap_min = safe_div(len(shared_titles), min(old_count, new_count))
                if not rpa_refuted and not shared_headimg and not shared_urls and overlap_min < 0.22:
                    continue
                rank_info = self.rank_continuity(old_id, new_id, shared_titles)
                confidence, status = self.candidate_score(
                    old_id=old_id,
                    new_id=new_id,
                    shared_titles=shared_titles,
                    shared_urls=shared_urls,
                    shared_headimg=shared_headimg,
                    gap_days=gap,
                    rank_continuity=rank_info,
                    rpa_refuted_deindex=rpa_refuted,
                )
                if status == "weak_candidate":
                    continue
                shared_title_samples = []
                for title_key in sorted(shared_titles):
                    old_article = next((a for a in self.account_articles[old_id] if normalize_title(a.get("title")) == title_key), None)
                    new_article = next((a for a in self.account_articles[new_id] if normalize_title(a.get("title")) == title_key), None)
                    shared_title_samples.append({
                        "title": (old_article or new_article or {}).get("title") or title_key,
                        "old_first_seen_at": (old_article or {}).get("first_seen_at"),
                        "old_last_seen_at": (old_article or {}).get("last_seen_at"),
                        "new_first_seen_at": (new_article or {}).get("first_seen_at"),
                        "new_last_seen_at": (new_article or {}).get("last_seen_at"),
                        "same_url": (
                            bool(old_article and new_article)
                            and not is_placeholder_url(old_article.get("normalized_url"))
                            and old_article.get("normalized_url") == new_article.get("normalized_url")
                        ),
                    })
                candidates.append({
                    "alias_id": f"alias_{old_id}_{new_id}",
                    "status": status,
                    "confidence": confidence,
                    "source_account_id": old_id,
                    "source_account_name": self.account_name.get(old_id, old_id),
                    "target_account_id": new_id,
                    "target_account_name": self.account_name.get(new_id, new_id),
                    "source_last_seen_at": old_last,
                    "target_first_seen_at": new_first,
                    "gap_days": gap,
                    "shared_title_count": len(shared_titles),
                    "shared_url_count": len(shared_urls),
                    "shared_headimg": shared_headimg,
                    "shared_headimg_url": shared_headimg_url,
                    "source_title_count": old_count,
                    "target_title_count": new_count,
                    "title_overlap_min": round(overlap_min, 4),
                    "name_similarity": round(SequenceMatcher(None, self.account_name.get(old_id, ""), self.account_name.get(new_id, "")).ratio(), 4),
                    "rank_continuity": rank_info,
                    "shared_titles": shared_title_samples[:30],
                    "shared_urls": sorted(shared_urls)[:20],
                    "evidence_flags": {
                        "rpa_refuted_deindex": rpa_refuted,
                        "temporal_handoff": gap >= 0,
                        "has_shared_url": bool(shared_urls),
                        "has_shared_headimg": shared_headimg,
                    },
                })
        candidates.sort(key=lambda x: (x["confidence"], x["shared_title_count"], x["shared_url_count"]), reverse=True)
        return candidates

    def build_groups(self, aliases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        uf = UnionFind()
        for account_id in self.account_name:
            uf.find(account_id)
        for alias in aliases:
            if alias["status"] in {"rpa_confirmed_alias", "strong_alias", "likely_alias"}:
                uf.union(alias["source_account_id"], alias["target_account_id"])
        groups_map: dict[str, list[str]] = defaultdict(list)
        for account_id in self.account_name:
            groups_map[uf.find(account_id)].append(account_id)
        groups = []
        for root_id, ids in groups_map.items():
            if len(ids) < 2:
                continue
            ids.sort(key=lambda aid: self.account_seen.get(aid, {}).get("first_seen_at") or "")
            latest_id = max(ids, key=lambda aid: self.account_seen.get(aid, {}).get("last_seen_at") or "")
            groups.append({
                "logical_account_id": f"logical_{root_id}",
                "canonical_account_id": latest_id,
                "canonical_account_name": self.account_name.get(latest_id, latest_id),
                "account_ids": ids,
                "account_names": [self.account_name.get(aid, aid) for aid in ids],
                "first_seen_at": min((self.account_seen.get(aid, {}).get("first_seen_at") or "" for aid in ids), default=None),
                "last_seen_at": max((self.account_seen.get(aid, {}).get("last_seen_at") or "" for aid in ids), default=None),
            })
        groups.sort(key=lambda g: (len(g["account_ids"]), g["last_seen_at"] or ""), reverse=True)
        return groups

    def build_score_impact(self, aliases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for alias in aliases:
            if alias["status"] not in {"rpa_confirmed_alias", "strong_alias", "likely_alias"}:
                continue
            target = self.monitor_by_account.get(alias["target_account_id"])
            source = self.monitor_by_account.get(alias["source_account_id"])
            if not target and not source:
                continue
            rows.append({
                "source_account_id": alias["source_account_id"],
                "source_account_name": alias["source_account_name"],
                "target_account_id": alias["target_account_id"],
                "target_account_name": alias["target_account_name"],
                "alias_confidence": alias["confidence"],
                "target_score": (target or {}).get("score"),
                "target_timeliness_score": (target or {}).get("timeliness_score"),
                "target_current_streak": (target or {}).get("current_streak"),
                "target_hit_days": (target or {}).get("hit_days"),
                "target_article_count": (target or {}).get("article_count"),
                "source_score": (source or {}).get("score"),
                "source_current_streak": (source or {}).get("current_streak"),
                "combined_score_rough": round(float((target or {}).get("score") or 0) + float((source or {}).get("score") or 0), 2),
                "risk": "blackhorse_alias_risk" if target and not source else "score_split_risk",
            })
        rows.sort(key=lambda x: (x["target_score"] or 0, x["alias_confidence"]), reverse=True)
        return rows

    def build(self, min_shared_titles: int, max_gap_days: int) -> dict[str, Any]:
        aliases = self.build_candidates(min_shared_titles=min_shared_titles, max_gap_days=max_gap_days)
        groups = self.build_groups(aliases)
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source_files": {
                "accounts": "normalized/accounts.json",
                "articles": "normalized/articles.json",
                "ranking_hits": "normalized/ranking_hits.json",
                "snapshots": "normalized/snapshots.json",
                "monitor": "normalized/monitor-data.json",
                "rpa_checks": "data/state/penalty_rpa_title_checks.json",
            },
            "parameters": {
                "min_shared_titles": min_shared_titles,
                "max_gap_days": max_gap_days,
            },
            "summary": {
                "alias_count": len(aliases),
                "status_counts": dict(__import__("collections").Counter(a["status"] for a in aliases)),
                "group_count": len(groups),
                "score_impact_count": len(self.build_score_impact(aliases)),
            },
            "aliases": aliases,
            "logical_account_groups": groups,
            "score_impact": self.build_score_impact(aliases),
        }


def render_report(data: dict[str, Any]) -> str:
    lines = [
        "# 账号改名考古报告",
        "",
        f"- 生成时间：{data['generated_at']}",
        f"- alias 数：{data['summary']['alias_count']}；逻辑账号组：{data['summary']['group_count']}",
        f"- 类型分布：{data['summary']['status_counts']}",
        "",
        "## 高置信改名链",
        "",
        "| 旧账号 | 新账号 | 状态 | 置信度 | 间隔天数 | 共享标题 | 共享URL | 同头像 | 排名连续性 |",
        "|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    for alias in data["aliases"][:40]:
        rank = alias.get("rank_continuity") or {}
        lines.append(
            f"| {alias['source_account_name']} | {alias['target_account_name']} | {alias['status']} | "
            f"{alias['confidence']} | {alias['gap_days']} | {alias['shared_title_count']} | "
            f"{alias['shared_url_count']} | {'是' if alias.get('shared_headimg') else '否'} | "
            f"n={rank.get('rank_sample_count')} avgΔ={rank.get('avg_abs_rank_delta')} |"
        )
    lines.extend([
        "",
        "## 黑马榜污染风险",
        "",
        "| 旧账号 | 新账号 | 新账号分 | 新账号命中天数 | 新账号连胜 | 风险 |",
        "|---|---|---:|---:|---:|---|",
    ])
    for row in data["score_impact"][:40]:
        lines.append(
            f"| {row['source_account_name']} | {row['target_account_name']} | "
            f"{row.get('target_score')} | {row.get('target_hit_days')} | {row.get('target_current_streak')} | {row['risk']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build account alias candidates from title/url/time continuity.")
    parser.add_argument("--min-shared-titles", type=int, default=3)
    parser.add_argument("--max-gap-days", type=int, default=45)
    parser.add_argument("--output", default="normalized/account_aliases.json")
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    builder = AccountAliasBuilder(PROJECT_ROOT)
    data = builder.build(min_shared_titles=args.min_shared_titles, max_gap_days=args.max_gap_days)
    output_path = PROJECT_ROOT / args.output
    dump_json(output_path, data)
    print(f"[ok] wrote {output_path}")
    print(f"[ok] aliases={data['summary']['alias_count']} statuses={data['summary']['status_counts']}")
    if args.report:
        report_path = PROJECT_ROOT / args.report
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_report(data), encoding="utf-8")
        print(f"[ok] wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
