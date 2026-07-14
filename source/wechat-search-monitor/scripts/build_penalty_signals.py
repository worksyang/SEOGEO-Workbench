#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
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


def to_date(value: str | None) -> str | None:
    if not value:
        return None
    return value[:10]


def days_between(a: str, b: str) -> int:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days


def bucket_n(n: int) -> str:
    if n <= 4:
        return str(n)
    if n <= 7:
        return "5-7"
    if n <= 10:
        return "8-10"
    if n <= 20:
        return "11-20"
    return "21+"


def rank_bucket(rank: int | None) -> str:
    if rank is None:
        return "unknown"
    if rank <= 3:
        return "1-3"
    if rank <= 5:
        return "4-5"
    if rank <= 10:
        return "6-10"
    return "11+"


def confidence_from_p(p_value: float) -> int:
    if p_value <= 0:
        return 100
    return max(0, min(100, round((-math.log10(max(p_value, 1e-12)) / 6) * 100)))


def safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def normalize_title(title: str | None) -> str:
    return re.sub(r"\s+", "", title or "").lower()


def tokenize_title(text: str) -> list[str]:
    text = re.sub(r"\s+", "", text)
    terms: list[str] = []
    terms.extend(re.findall(r"[A-Za-z0-9]+(?:\.[0-9]+)?", text))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]+", text))
    for n in (2, 3, 4):
        for i in range(max(0, len(chinese) - n + 1)):
            terms.append(chinese[i:i + n])
    return terms


@dataclass(frozen=True)
class Transition:
    from_date: str
    to_date: str

    @property
    def gap_days(self) -> int:
        return days_between(self.from_date, self.to_date)


class PenaltySignalBuilder:
    def __init__(self, root: Path, min_coverage: int, overlap_threshold: float) -> None:
        self.root = root
        self.min_coverage = min_coverage
        self.overlap_threshold = overlap_threshold
        self.snapshots = load_json(root / "normalized" / "snapshots.json")
        self.hits = load_json(root / "normalized" / "ranking_hits.json")
        self.accounts = load_json(root / "normalized" / "accounts.json")
        self.articles = load_json(root / "normalized" / "articles.json")
        from app.repositories.keyword_registry_repo import KeywordRegistryRepository
        self.keywords = KeywordRegistryRepository(
            root / "data/state/app.db"
        ).list_keywords()
        self.rpa_checks = self._load_rpa_checks()
        self.account_aliases = self._load_account_aliases()
        self.aliases_by_source = self._build_alias_index()

        self.snap_by_id = {
            s["snapshot_id"]: s
            for s in self.snapshots
            if s.get("status") == "success"
        }
        self.account_name = {
            a["account_id"]: a.get("canonical_name") or a["account_id"]
            for a in self.accounts
        }
        self.keyword_text = {
            k["keyword_id"]: k.get("keyword_text") or k["keyword_id"]
            for k in self.keywords
        }
        self.article_by_id = {a["article_id"]: a for a in self.articles}
        self.articles_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for article in self.articles:
            self.articles_by_account[article.get("account_id")].append(article)
        for rows in self.articles_by_account.values():
            rows.sort(key=lambda x: x.get("published_at") or x.get("first_seen_at") or "", reverse=True)

        self.covered_keywords_by_day: dict[str, set[str]] = defaultdict(set)
        self.day_account_keywords: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.day_account_articles: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.day_account_hits: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.day_account_titles: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.day_article_keywords: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.day_article_account: dict[str, dict[str, str]] = defaultdict(dict)
        self.day_kw_account_best_rank: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
        self.day_kw_article_best_rank: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
        self.day_kw_rank_rows: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        self.rank_history: dict[tuple[str, str, str], list[tuple[str, int]]] = defaultdict(list)

        self._build_indexes()

    def _build_indexes(self) -> None:
        for snap in self.snap_by_id.values():
            self.covered_keywords_by_day[snap["snapshot_date"]].add(snap["keyword_id"])

        for hit in self.hits:
            snap = self.snap_by_id.get(hit.get("snapshot_id"))
            if not snap:
                continue
            day = snap["snapshot_date"]
            keyword_id = snap["keyword_id"]
            account_id = hit["account_id"]
            article_id = hit["article_id"]
            rank = int(hit["rank"])

            self.day_account_keywords[day][account_id].add(keyword_id)
            self.day_account_articles[day][account_id].add(article_id)
            self.day_account_hits[day][account_id] += 1
            self.day_account_titles[day][account_id].add(hit.get("title_raw") or "")
            self.day_article_keywords[day][article_id].add(keyword_id)
            self.day_article_account[day][article_id] = account_id

            prev_account_rank = self.day_kw_account_best_rank[day][keyword_id].get(account_id)
            if prev_account_rank is None or rank < prev_account_rank:
                self.day_kw_account_best_rank[day][keyword_id][account_id] = rank

            prev_article_rank = self.day_kw_article_best_rank[day][keyword_id].get(article_id)
            if prev_article_rank is None or rank < prev_article_rank:
                self.day_kw_article_best_rank[day][keyword_id][article_id] = rank

            self.day_kw_rank_rows[day][keyword_id].append({
                "rank": rank,
                "account_id": account_id,
                "account_name": self.account_name.get(account_id, account_id),
                "article_id": article_id,
                "title": hit.get("title_raw") or "",
            })
            self.rank_history[(account_id, article_id, keyword_id)].append((day, rank))

        for day_map in self.day_kw_rank_rows.values():
            for rows in day_map.values():
                rows.sort(key=lambda r: (r["rank"], r["account_name"], r["title"]))

        for key, rows in list(self.rank_history.items()):
            dedup: dict[str, int] = {}
            for day, rank in rows:
                dedup[day] = min(rank, dedup.get(day, rank))
            self.rank_history[key] = sorted(dedup.items())

    def _load_rpa_checks(self) -> dict[str, Any]:
        path = self.root / "data" / "state" / "penalty_rpa_title_checks.json"
        if not path.exists():
            return {"status": "not_completed", "checks": []}
        payload = load_json(path)
        checks = payload.get("checks", []) if isinstance(payload, dict) else []
        by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for check in checks:
            by_account[str(check.get("target_account_name") or "")].append(check)
        return {
            "status": "completed",
            "generated_at": payload.get("generated_at"),
            "batch_id": payload.get("batch_id"),
            "server": payload.get("server"),
            "checks": checks,
            "by_account": dict(by_account),
        }

    def _load_account_aliases(self) -> dict[str, Any]:
        path = self.root / "normalized" / "account_aliases.json"
        if not path.exists():
            return {
                "status": "not_available",
                "path": "normalized/account_aliases.json",
                "aliases": [],
                "logical_account_groups": [],
                "score_impact": [],
            }
        payload = load_json(path)
        payload["status"] = "loaded"
        payload["path"] = "normalized/account_aliases.json"
        return payload

    def _build_alias_index(self) -> dict[str, list[dict[str, Any]]]:
        trusted_statuses = {"rpa_confirmed_alias", "strong_alias", "likely_alias"}
        by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for alias in self.account_aliases.get("aliases", []):
            if alias.get("status") not in trusted_statuses:
                continue
            source_id = alias.get("source_account_id")
            if source_id:
                by_source[source_id].append(alias)
        for rows in by_source.values():
            rows.sort(key=lambda a: (a.get("confidence", 0), a.get("shared_title_count", 0)), reverse=True)
        return dict(by_source)

    @property
    def comparable_dates(self) -> list[str]:
        return [
            d for d in sorted(self.covered_keywords_by_day)
            if len(self.covered_keywords_by_day[d]) >= self.min_coverage
        ]

    @property
    def transitions(self) -> list[Transition]:
        dates = self.comparable_dates
        return [Transition(dates[i], dates[i + 1]) for i in range(len(dates) - 1)]

    def observed_overlap(self, keywords: set[str], day: str) -> float:
        return safe_div(len(keywords & self.covered_keywords_by_day[day]), len(keywords))

    def account_survival_miss_rate(self, account_id: str, before_date: str, rank: int | None) -> float:
        opportunities = 0
        losses = 0
        rb = rank_bucket(rank)
        for tr in self.transitions:
            if tr.from_date >= before_date:
                break
            from_ranks = self.day_kw_account_best_rank[tr.from_date]
            to_accounts = self.day_kw_account_best_rank[tr.to_date]
            for keyword_id, ranks_by_account in from_ranks.items():
                if keyword_id not in self.covered_keywords_by_day[tr.to_date]:
                    continue
                prior_rank = ranks_by_account.get(account_id)
                if prior_rank is None or rank_bucket(prior_rank) != rb:
                    continue
                opportunities += 1
                if account_id not in to_accounts.get(keyword_id, {}):
                    losses += 1
        if opportunities >= 5:
            return (losses + 1) / (opportunities + 2)
        return self.global_miss_rate(before_date, rb)

    def global_miss_rate(self, before_date: str, rb: str) -> float:
        opportunities = 0
        losses = 0
        for tr in self.transitions:
            if tr.from_date >= before_date:
                break
            from_ranks = self.day_kw_account_best_rank[tr.from_date]
            to_accounts = self.day_kw_account_best_rank[tr.to_date]
            for keyword_id, ranks_by_account in from_ranks.items():
                if keyword_id not in self.covered_keywords_by_day[tr.to_date]:
                    continue
                for account_id, prior_rank in ranks_by_account.items():
                    if rank_bucket(prior_rank) != rb:
                        continue
                    opportunities += 1
                    if account_id not in to_accounts.get(keyword_id, {}):
                        losses += 1
        return (losses + 1) / (opportunities + 2)

    def zero_probability(self, account_id: str, from_date: str, to_date: str, keywords: set[str]) -> float:
        covered = keywords & self.covered_keywords_by_day[to_date]
        if not covered:
            return 1.0
        p = 1.0
        for keyword_id in covered:
            rank = self.day_kw_account_best_rank[from_date].get(keyword_id, {}).get(account_id)
            p *= self.account_survival_miss_rate(account_id, from_date, rank)
        return max(min(p, 1.0), 1e-12)

    def future_presence(self, account_id: str, after_date: str) -> list[tuple[str, int]]:
        return [
            (day, len(self.day_account_keywords[day].get(account_id, set())))
            for day in self.comparable_dates
            if day > after_date
        ]

    def latest_articles(self, account_id: str, before_date: str, limit: int = 6) -> list[dict[str, Any]]:
        rows = []
        for article in self.articles_by_account.get(account_id, []):
            published_day = to_date(article.get("published_at"))
            first_seen_day = to_date(article.get("first_seen_at"))
            if (published_day and published_day <= before_date) or (not published_day and first_seen_day and first_seen_day <= before_date):
                rows.append({
                    "article_id": article.get("article_id"),
                    "title": article.get("title"),
                    "published_at": article.get("published_at"),
                    "first_seen_at": article.get("first_seen_at"),
                    "last_seen_at": article.get("last_seen_at"),
                    "content_status": article.get("content_status"),
                    "content_file_path": article.get("content_file_path"),
                })
            if len(rows) >= limit:
                break
        return rows

    def rank_slope(self, account_id: str, article_ids: set[str], keyword_ids: set[str], last_date: str) -> dict[str, Any]:
        slopes = []
        traces = []
        for article_id in article_ids:
            for keyword_id in keyword_ids:
                history = [(d, r) for d, r in self.rank_history.get((account_id, article_id, keyword_id), []) if d <= last_date]
                if len(history) < 2:
                    continue
                recent = history[-4:]
                delta = recent[-1][1] - recent[0][1]
                slopes.append(delta / max(1, len(recent) - 1))
                traces.append({
                    "article_id": article_id,
                    "keyword_id": keyword_id,
                    "keyword": self.keyword_text.get(keyword_id, keyword_id),
                    "history": [{"date": d, "rank": r} for d, r in recent],
                    "delta": delta,
                })
        if not slopes:
            return {"avg_slope": None, "deteriorating_traces": []}
        deteriorating = [t for t in traces if t["delta"] >= 3]
        return {
            "avg_slope": round(sum(slopes) / len(slopes), 3),
            "deteriorating_trace_count": len(deteriorating),
            "deteriorating_traces": deteriorating[:8],
        }

    def vacancy_fill(self, account_id: str, from_date: str, to_date: str, keyword_ids: set[str]) -> list[dict[str, Any]]:
        out = []
        for keyword_id in sorted(keyword_ids, key=lambda k: self.keyword_text.get(k, k)):
            if keyword_id not in self.covered_keywords_by_day[to_date]:
                continue
            prior_rank = self.day_kw_account_best_rank[from_date].get(keyword_id, {}).get(account_id)
            if prior_rank is None:
                continue
            previous_rows = self.day_kw_rank_rows[from_date].get(keyword_id, [])
            next_rows = self.day_kw_rank_rows[to_date].get(keyword_id, [])
            prior_next_rank = None
            for row in previous_rows:
                if row["rank"] > prior_rank:
                    prior_next_rank = row["rank"]
                    prior_next_account = row["account_id"]
                    prior_next_name = row["account_name"]
                    prior_next_title = row["title"]
                    break
            filled_by = next((r for r in next_rows if r["rank"] == prior_rank), None)
            sequential = False
            if prior_next_rank is not None:
                new_rank = self.day_kw_account_best_rank[to_date].get(keyword_id, {}).get(prior_next_account)
                sequential = new_rank is not None and new_rank <= prior_rank
            out.append({
                "keyword_id": keyword_id,
                "keyword": self.keyword_text.get(keyword_id, keyword_id),
                "vacated_rank": prior_rank,
                "filled_by": filled_by,
                "prior_next": {
                    "rank": prior_next_rank,
                    "account_id": prior_next_account if prior_next_rank is not None else None,
                    "account_name": prior_next_name if prior_next_rank is not None else None,
                    "title": prior_next_title if prior_next_rank is not None else None,
                },
                "sequential_fill": sequential,
            })
        return out

    def sibling_accounts(self, account_id: str) -> list[dict[str, Any]]:
        name = self.account_name.get(account_id, account_id)
        own_titles = {normalize_title(a.get("title")) for a in self.articles_by_account.get(account_id, [])}
        own_titles.discard("")
        rows = []
        for other_id, other_name in self.account_name.items():
            if other_id == account_id:
                continue
            name_score = SequenceMatcher(None, name, other_name).ratio()
            shared_titles = []
            if own_titles:
                for article in self.articles_by_account.get(other_id, []):
                    title_key = normalize_title(article.get("title"))
                    if title_key and title_key in own_titles:
                        shared_titles.append(article.get("title"))
            if name_score >= 0.62 or shared_titles:
                rows.append({
                    "account_id": other_id,
                    "account_name": other_name,
                    "name_similarity": round(name_score, 3),
                    "shared_titles": shared_titles[:5],
                    "latest_seen_at": max((a.get("last_seen_at") or "" for a in self.articles_by_account.get(other_id, [])), default=None),
                    "still_seen_after_target": None,
                })
        rows.sort(key=lambda r: (len(r["shared_titles"]), r["name_similarity"]), reverse=True)
        return rows[:10]

    def identity_check(self, account_id: str, zero_date: str) -> dict[str, Any]:
        if self.account_aliases.get("status") != "loaded":
            return {
                "identity_check_done": False,
                "status": "not_run",
                "reason": "account_aliases.json is not available; suspected_deindex must stay watch until alias check runs.",
            }

        matches = []
        for alias in self.aliases_by_source.get(account_id, []):
            target_id = alias.get("target_account_id")
            target_presence = [
                {"date": day, "keyword_count": len(self.day_account_keywords[day].get(target_id, set()))}
                for day in self.comparable_dates
                if day >= zero_date and len(self.day_account_keywords[day].get(target_id, set())) > 0
            ]
            matches.append({
                "alias_id": alias.get("alias_id"),
                "status": alias.get("status"),
                "confidence": alias.get("confidence"),
                "source_account_id": alias.get("source_account_id"),
                "source_account_name": alias.get("source_account_name"),
                "target_account_id": alias.get("target_account_id"),
                "target_account_name": alias.get("target_account_name"),
                "gap_days": alias.get("gap_days"),
                "shared_title_count": alias.get("shared_title_count"),
                "shared_url_count": alias.get("shared_url_count"),
                "target_seen_after_zero": bool(target_presence),
                "target_presence_after_zero": target_presence[:8],
            })

        if matches:
            return {
                "identity_check_done": True,
                "status": "alias_found",
                "matched_aliases": matches,
                "note": "Trusted account alias detected; treat zero event as identity/name split before considering deindexing.",
            }
        return {
            "identity_check_done": True,
            "status": "no_alias_found",
            "matched_aliases": [],
        }

    def baseline(self) -> dict[str, Any]:
        buckets: dict[str, Counter] = defaultdict(Counter)
        revival_delays = Counter()
        revival_examples = []
        for tr in self.transitions:
            for account_id, kws in self.day_account_keywords[tr.from_date].items():
                if not kws:
                    continue
                overlap = self.observed_overlap(kws, tr.to_date)
                if overlap < self.overlap_threshold:
                    continue
                n = len(kws)
                b = bucket_n(n)
                buckets[b]["opportunities"] += 1
                if account_id not in self.day_account_keywords[tr.to_date]:
                    buckets[b]["zero_next"] += 1
                    future = self.future_presence(account_id, tr.to_date)
                    returned = next(((d, c) for d, c in future if c > 0), None)
                    if returned:
                        delay = days_between(tr.to_date, returned[0])
                        buckets[b]["returned_later"] += 1
                        revival_delays[str(delay)] += 1
                        if len(revival_examples) < 20:
                            revival_examples.append({
                                "account_id": account_id,
                                "account_name": self.account_name.get(account_id, account_id),
                                "from_date": tr.from_date,
                                "zero_date": tr.to_date,
                                "return_date": returned[0],
                                "return_keywords": returned[1],
                                "previous_keywords": n,
                            })
                    else:
                        buckets[b]["stayed_zero"] += 1

        by_bucket = []
        for b in sorted(buckets, key=lambda x: (int(x.split("-")[0].replace("+", "")) if x[0].isdigit() else 999, x)):
            c = buckets[b]
            opp = c["opportunities"]
            zero = c["zero_next"]
            by_bucket.append({
                "previous_keyword_bucket": b,
                "opportunities": opp,
                "zero_next": zero,
                "zero_rate": round(safe_div(zero, opp), 4),
                "returned_later": c["returned_later"],
                "return_rate_among_zero": round(safe_div(c["returned_later"], zero), 4),
                "stayed_zero": c["stayed_zero"],
            })

        return {
            "transitions": [{"from_date": t.from_date, "to_date": t.to_date, "gap_days": t.gap_days} for t in self.transitions],
            "coverage_by_date": [{"date": d, "keyword_count": len(self.covered_keywords_by_day[d])} for d in self.comparable_dates],
            "by_previous_keyword_bucket": by_bucket,
            "revival_delay_distribution_days": dict(sorted(revival_delays.items(), key=lambda kv: int(kv[0]))),
            "revival_examples": revival_examples,
        }

    def classify_events(self, min_previous_keywords: int) -> list[dict[str, Any]]:
        events = []
        for tr in self.transitions:
            # Account and article disappearance events.
            for account_id, kws in self.day_account_keywords[tr.from_date].items():
                if len(kws) < min_previous_keywords:
                    continue
                overlap = self.observed_overlap(kws, tr.to_date)
                if overlap < self.overlap_threshold:
                    continue
                next_kws = self.day_account_keywords[tr.to_date].get(account_id, set())
                from_articles = self.day_account_articles[tr.from_date].get(account_id, set())
                next_articles = self.day_account_articles[tr.to_date].get(account_id, set())
                future = self.future_presence(account_id, tr.to_date)
                future_seen_days = sum(1 for _, count in future if count > 0)
                sustained_zero_days = sum(1 for _, count in future if count == 0)
                p_one_day = self.zero_probability(account_id, tr.from_date, tr.to_date, kws)
                p_sustained = max(p_one_day ** max(1, min(6, sustained_zero_days + 1)), 1e-12)

                if not next_kws:
                    if future_seen_days > 0:
                        signature = "recovered_zero"
                        status = "recovered"
                    elif len(from_articles) >= 2 and future_seen_days == 0 and len(future) >= 2:
                        signature = "account_deindex"
                        status = "suspected_deindex"
                    elif len(from_articles) >= 2:
                        signature = "account_watch"
                        status = "watch"
                    elif len(from_articles) == 1:
                        signature = "undetermined_single_article_or_account"
                        status = "watch"
                    else:
                        signature = "unknown_zero"
                        status = "watch"
                    slope = self.rank_slope(account_id, from_articles, kws, tr.from_date)
                    vacancy = self.vacancy_fill(account_id, tr.from_date, tr.to_date, kws)
                    siblings = self.sibling_accounts(account_id)
                    for sibling in siblings:
                        presence = [
                            (d, len(self.day_account_keywords[d].get(sibling["account_id"], set())))
                            for d in self.comparable_dates
                            if d > tr.to_date
                        ]
                        sibling["still_seen_after_target"] = any(count > 0 for _, count in presence)
                    identity = self.identity_check(account_id, tr.to_date)
                    events.append({
                        "event_id": f"pen_{account_id}_{tr.from_date}_{tr.to_date}",
                        "event_type": signature,
                        "penalty_status": status,
                        "penalty_confidence": confidence_from_p(p_sustained),
                        "p_zero_one_day": p_one_day,
                        "p_zero_sustained": p_sustained,
                        "account_id": account_id,
                        "account_name": self.account_name.get(account_id, account_id),
                        "last_seen_date": tr.from_date,
                        "zero_date": tr.to_date,
                        "gap_days": tr.gap_days,
                        "previous_keyword_count": len(kws),
                        "previous_article_count": len(from_articles),
                        "previous_hit_count": self.day_account_hits[tr.from_date][account_id],
                        "next_keyword_count": len(next_kws),
                        "future_comparable_days": len(future),
                        "future_seen_days": future_seen_days,
                        "sustained_zero_days": sustained_zero_days,
                        "coverage_overlap": round(overlap, 4),
                        "previous_keywords": [self.keyword_text.get(k, k) for k in sorted(kws, key=lambda x: self.keyword_text.get(x, x))],
                        "last_titles": sorted(self.day_account_titles[tr.from_date][account_id]),
                        "latest_articles": self.latest_articles(account_id, tr.from_date),
                        "rank_slope": slope,
                        "vacancy_fill": vacancy,
                        "sibling_accounts": siblings,
                        "identity_check_done": identity["identity_check_done"],
                        "identity_check_result": identity,
                        "rpa_confirmation": {
                            "status": "unverified",
                            "note": "Only RPA/manual exact-title search can promote this event to confirmed.",
                        },
                    })
                    continue

                lost = kws - next_kws
                retained = kws & next_kws
                if len(lost) >= 2 and retained:
                    lost_covered = lost & self.covered_keywords_by_day[tr.to_date]
                    if len(lost_covered) >= 2:
                        p_partial = self.zero_probability(account_id, tr.from_date, tr.to_date, lost_covered)
                        events.append({
                            "event_id": f"qfilter_{account_id}_{tr.from_date}_{tr.to_date}",
                            "event_type": "query_filter",
                            "penalty_status": "watch",
                            "penalty_confidence": confidence_from_p(p_partial),
                            "p_lost_keywords": p_partial,
                            "account_id": account_id,
                            "account_name": self.account_name.get(account_id, account_id),
                            "last_seen_date": tr.from_date,
                            "zero_date": tr.to_date,
                            "previous_keyword_count": len(kws),
                            "lost_keyword_count": len(lost_covered),
                            "retained_keyword_count": len(retained),
                            "lost_keywords": [self.keyword_text.get(k, k) for k in sorted(lost_covered, key=lambda x: self.keyword_text.get(x, x))],
                            "retained_keywords": [self.keyword_text.get(k, k) for k in sorted(retained, key=lambda x: self.keyword_text.get(x, x))],
                            "last_titles": sorted(self.day_account_titles[tr.from_date][account_id]),
                        })

                # Article-level removal while the same account remains visible.
                for article_id in from_articles:
                    article_kws = self.day_article_keywords[tr.from_date].get(article_id, set())
                    if len(article_kws) < 2:
                        continue
                    if self.observed_overlap(article_kws, tr.to_date) < self.overlap_threshold:
                        continue
                    if article_id not in self.day_article_keywords[tr.to_date] and next_articles:
                        events.append({
                            "event_id": f"article_{article_id}_{tr.from_date}_{tr.to_date}",
                            "event_type": "article_removed",
                            "penalty_status": "watch",
                            "penalty_confidence": confidence_from_p(self.zero_probability(account_id, tr.from_date, tr.to_date, article_kws)),
                            "account_id": account_id,
                            "account_name": self.account_name.get(account_id, account_id),
                            "article_id": article_id,
                            "article_title": self.article_by_id.get(article_id, {}).get("title"),
                            "last_seen_date": tr.from_date,
                            "zero_date": tr.to_date,
                            "previous_keyword_count": len(article_kws),
                            "same_account_remaining_article_count": len(next_articles),
                            "previous_keywords": [self.keyword_text.get(k, k) for k in sorted(article_kws, key=lambda x: self.keyword_text.get(x, x))],
                        })

        events.sort(key=lambda e: (e.get("penalty_confidence", 0), e.get("previous_keyword_count", 0), e.get("previous_article_count", 0)), reverse=True)
        self.apply_identity_checks(events)
        self.apply_rpa_checks(events)
        events.sort(key=lambda e: (e.get("penalty_confidence", 0), e.get("previous_keyword_count", 0), e.get("previous_article_count", 0)), reverse=True)
        return events

    def apply_identity_checks(self, events: list[dict[str, Any]]) -> None:
        zero_event_types = {
            "account_deindex",
            "account_watch",
            "recovered_zero",
            "undetermined_single_article_or_account",
            "unknown_zero",
        }
        for event in events:
            if event.get("event_type") not in zero_event_types:
                continue
            identity = event.get("identity_check_result") or {}
            if not event.get("identity_check_done"):
                if event.get("penalty_status") == "suspected_deindex":
                    event["event_type"] = "account_watch"
                    event["penalty_status"] = "watch"
                    event["rpa_confirmation"] = {
                        "status": "blocked_by_missing_identity_check",
                        "note": "Alias detection has not run; this event cannot enter suspected_deindex.",
                    }
                continue
            if identity.get("status") == "alias_found":
                event["event_type"] = "account_identity_shift"
                event["penalty_status"] = "normal"
                event["penalty_confidence"] = 0
                event["p_zero_sustained"] = 1.0
                event["rpa_confirmation"] = {
                    "status": "identity_refuted_deindex",
                    "note": "Account alias detection found a trusted successor account; treat this as name/entity split unless RPA later disproves it.",
                }

    def apply_rpa_checks(self, events: list[dict[str, Any]]) -> None:
        by_account = self.rpa_checks.get("by_account") or {}
        if not by_account:
            return
        for event in events:
            checks = by_account.get(event.get("account_name"))
            if not checks:
                continue
            targeted = [c for c in checks if c.get("target_event_id")]
            if targeted:
                checks = [c for c in targeted if c.get("target_event_id") == event.get("event_id")]
                if not checks:
                    continue
            event["rpa_confirmation"] = {
                "status": "checked",
                "checked_at": self.rpa_checks.get("generated_at"),
                "batch_id": self.rpa_checks.get("batch_id"),
                "title_checks": checks,
            }
            last_titles = set(event.get("last_titles") or [])
            relevant = [c for c in checks if c.get("query_title") in last_titles]
            if not relevant:
                relevant = checks
            found_exact = [c for c in relevant if c.get("exact_found")]
            found_under_other = [
                c for c in found_exact
                if c.get("exact_account_name") and c.get("exact_account_name") != event.get("account_name")
            ]
            all_relevant_found = bool(relevant) and len(found_exact) == len(relevant)
            none_relevant_found = bool(relevant) and not found_exact

            if all_relevant_found and found_under_other:
                event["event_type"] = "account_identity_shift"
                event["penalty_status"] = "normal"
                event["penalty_confidence"] = 0
                event["p_zero_sustained"] = 1.0
                event["rpa_confirmation"]["status"] = "refuted_deindex"
                event["rpa_confirmation"]["note"] = "Exact titles are still indexed under another account name; this is likely account identity/name split, not deindexing."
            elif none_relevant_found:
                event["rpa_confirmation"]["status"] = "exact_titles_not_found"
                event["rpa_confirmation"]["note"] = "Exact-title search did not find the checked titles; keep detector status unless sustained evidence is strong enough to confirm."

    def differential_terms(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        punished_titles = []
        control_titles = []
        event_dates = {event["last_seen_date"] for event in events if event["event_type"] in {"account_deindex", "account_watch", "undetermined_single_article_or_account"}}
        punished_accounts = {event["account_id"] for event in events if event["event_type"] in {"account_deindex", "account_watch", "undetermined_single_article_or_account"}}
        for event in events:
            if event["event_type"] in {"account_deindex", "account_watch", "undetermined_single_article_or_account"}:
                punished_titles.extend(event.get("last_titles", []))
        for day in event_dates:
            for account_id, titles in self.day_account_titles[day].items():
                if account_id in punished_accounts:
                    continue
                control_titles.extend(titles)

        punished = Counter()
        control = Counter()
        for title in punished_titles:
            punished.update(set(tokenize_title(title)))
        for title in control_titles:
            control.update(set(tokenize_title(title)))
        punished_total = sum(punished.values())
        control_total = sum(control.values())
        vocab = set(punished) | set(control)
        scored = []
        for term in vocab:
            if punished[term] < 1:
                continue
            odds_p = (punished[term] + 0.5) / (punished_total + len(vocab))
            odds_c = (control[term] + 0.5) / (control_total + len(vocab))
            scored.append({
                "term": term,
                "log_odds": round(math.log(odds_p / odds_c), 4),
                "punished_count": punished[term],
                "control_count": control[term],
            })
        scored.sort(key=lambda x: (x["log_odds"], x["punished_count"]), reverse=True)
        return scored[:80]

    def build_account_table(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        priority = {
            "confirmed": 5,
            "suspected_deindex": 4,
            "watch": 3,
            "recovered": 2,
            "normal": 1,
        }
        identity_shift_accounts = {
            event["account_id"]
            for event in events
            if event.get("event_type") == "account_identity_shift"
        }
        by_account: dict[str, dict[str, Any]] = {}
        for account_id, name in self.account_name.items():
            by_account[account_id] = {
                "account_id": account_id,
                "account_name": name,
                "penalty_status": "normal",
                "penalty_confidence": 0,
                "evidence": None,
                "last_titles": [],
            }
        for event in events:
            account_id = event["account_id"]
            if account_id in identity_shift_accounts and event.get("event_type") != "account_identity_shift":
                continue
            status = event.get("penalty_status", "watch")
            if event.get("future_seen_days", 0) > 0:
                status = "recovered"
            current = by_account.setdefault(account_id, {
                "account_id": account_id,
                "account_name": self.account_name.get(account_id, account_id),
                "penalty_status": "normal",
                "penalty_confidence": 0,
                "evidence": None,
                "last_titles": [],
            })
            if event.get("event_type") == "account_identity_shift":
                should_replace = True
            else:
                should_replace = (
                    priority.get(status, 0) > priority.get(current["penalty_status"], 0)
                    or event.get("penalty_confidence", 0) > current["penalty_confidence"]
                )
            if should_replace:
                current["penalty_status"] = status
                current["penalty_confidence"] = event.get("penalty_confidence", 0)
                current["evidence"] = {
                    "event_id": event.get("event_id"),
                    "event_type": event.get("event_type"),
                    "last_seen_date": event.get("last_seen_date"),
                    "zero_date": event.get("zero_date"),
                    "previous_keyword_count": event.get("previous_keyword_count"),
                    "previous_article_count": event.get("previous_article_count"),
                    "sustained_zero_days": event.get("sustained_zero_days"),
                    "rank_slope": event.get("rank_slope"),
                    "identity_check_done": event.get("identity_check_done"),
                    "identity_check_result": event.get("identity_check_result"),
                    "rpa_confirmation": event.get("rpa_confirmation"),
                }
                current["last_titles"] = event.get("last_titles", [])
        return sorted(by_account.values(), key=lambda r: (priority.get(r["penalty_status"], 0), r["penalty_confidence"]), reverse=True)

    def build(self, min_previous_keywords: int) -> dict[str, Any]:
        baseline = self.baseline()
        events = self.classify_events(min_previous_keywords)
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source_files": {
                "snapshots": "normalized/snapshots.json",
                "ranking_hits": "normalized/ranking_hits.json",
                "accounts": "normalized/accounts.json",
                "articles": "normalized/articles.json",
                "keywords": "data/state/app.db:keyword_registry",
                "account_aliases": self.account_aliases.get("path"),
            },
            "parameters": {
                "min_coverage": self.min_coverage,
                "overlap_threshold": self.overlap_threshold,
                "min_previous_keywords": min_previous_keywords,
                "trusted_alias_statuses": ["rpa_confirmed_alias", "strong_alias", "likely_alias"],
            },
            "summary": {
                "comparable_date_count": len(self.comparable_dates),
                "transition_count": len(self.transitions),
                "event_count": len(events),
                "event_type_counts": dict(Counter(e["event_type"] for e in events)),
                "status_counts": dict(Counter(e["penalty_status"] for e in events)),
                "identity_check": {
                    "status": self.account_aliases.get("status"),
                    "alias_count": len(self.account_aliases.get("aliases", [])),
                    "trusted_alias_source_count": len(self.aliases_by_source),
                    "event_checked_count": sum(1 for e in events if e.get("identity_check_done")),
                    "alias_refuted_event_count": sum(
                        1
                        for e in events
                        if (e.get("identity_check_result") or {}).get("status") == "alias_found"
                    ),
                },
            },
            "baseline": baseline,
            "events": events,
            "account_penalty_table": self.build_account_table(events),
            "differential_terms": self.differential_terms(events),
            "account_aliases": {
                "status": self.account_aliases.get("status"),
                "generated_at": self.account_aliases.get("generated_at"),
                "summary": self.account_aliases.get("summary", {}),
                "aliases": self.account_aliases.get("aliases", []),
                "score_impact": self.account_aliases.get("score_impact", []),
            },
            "rpa_title_checks": {
                "status": self.rpa_checks.get("status", "not_completed"),
                "generated_at": self.rpa_checks.get("generated_at"),
                "batch_id": self.rpa_checks.get("batch_id"),
                "server": self.rpa_checks.get("server"),
                "exact_titles_to_check": [
                    "友邦新品活然人生，专坑A8？",
                    "媒体点名567骗局！买港险千万不要这样做！",
                    "最全解析！港险货币转换，没你想得那么好",
                    "港险实用篇！趸交 or 5年交？哪种更划算？",
                    "仅剩 30 天关停！安盛盛利 II 2 年缴 7.30 永久停售，短缴美元储蓄通道即将关闭",
                    "限时窗口期！2026 年 7月 15 家港险优惠大汇总",
                ],
                "checks": self.rpa_checks.get("checks", []),
            },
        }


def render_report(data: dict[str, Any]) -> str:
    lines = []
    params = data["parameters"]
    summary = data["summary"]
    lines.append("# 惩罚信号检测报告")
    lines.append("")
    lines.append(f"- 生成时间：{data['generated_at']}")
    lines.append(f"- 可比日期：{summary['comparable_date_count']} 天；相邻可比转移：{summary['transition_count']} 组")
    lines.append(f"- 口径：单日覆盖关键词 >= {params['min_coverage']}，前后覆盖重叠 >= {params['overlap_threshold']:.0%}，消失前关键词 >= {params['min_previous_keywords']}")
    lines.append(f"- 事件数：{summary['event_count']}；类型分布：{summary['event_type_counts']}")
    identity = summary.get("identity_check", {})
    lines.append(
        f"- 身份校验：{identity.get('status')}；alias={identity.get('alias_count', 0)}；"
        f"已校验事件={identity.get('event_checked_count', 0)}；改名反驳={identity.get('alias_refuted_event_count', 0)}"
    )
    lines.append("")
    lines.append("## 自然流失基线")
    lines.append("")
    lines.append("| 前日关键词数 | 机会数 | 次日归零 | 归零率 | 后续复活 | 归零后复活率 | 持续归零 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for row in data["baseline"]["by_previous_keyword_bucket"]:
        lines.append(
            f"| {row['previous_keyword_bucket']} | {row['opportunities']} | {row['zero_next']} | "
            f"{row['zero_rate']:.2%} | {row['returned_later']} | {row['return_rate_among_zero']:.2%} | {row['stayed_zero']} |"
        )
    lines.append("")
    lines.append("## 账号级/文章级高置信事件")
    lines.append("")
    lines.append("| 账号 | 类型 | 状态 | 置信度 | 最后出现 -> 归零 | 前日关键词/文章 | 身份校验 | 后续 | 标题快照 |")
    lines.append("|---|---|---|---:|---|---:|---|---|---|")
    account_like_types = {
        "account_deindex",
        "account_watch",
        "account_identity_shift",
        "recovered_zero",
        "undetermined_single_article_or_account",
        "article_removed",
    }
    account_like_events = [e for e in data["events"] if e.get("event_type") in account_like_types]
    for event in account_like_events[:30]:
        titles = "；".join(event.get("last_titles", [])[:3])
        future = f"{event.get('future_seen_days', 0)}/{event.get('future_comparable_days', 0)} 天复现"
        identity_result = event.get("identity_check_result") or {}
        identity_label = identity_result.get("status", "-")
        if identity_result.get("matched_aliases"):
            alias = identity_result["matched_aliases"][0]
            identity_label = f"{identity_label}: {alias.get('target_account_name')}({alias.get('confidence')})"
        lines.append(
            f"| {event.get('account_name')} | {event.get('event_type')} | {event.get('penalty_status')} | "
            f"{event.get('penalty_confidence')} | {event.get('last_seen_date')} -> {event.get('zero_date')} | "
            f"{event.get('previous_keyword_count')}/{event.get('previous_article_count', '-')} | {identity_label} | {future} | {titles} |"
        )
    lines.append("")
    lines.append("## 改名/身份归并对惩罚判断的影响")
    lines.append("")
    aliases = data.get("account_aliases", {}).get("aliases", [])
    score_impact = data.get("account_aliases", {}).get("score_impact", [])
    lines.append("| 旧账号 | 新账号 | alias状态 | 置信度 | 共享标题/URL | 结论 |")
    lines.append("|---|---|---|---:|---:|---|")
    for alias in aliases[:20]:
        conclusion = "反驳除名候选" if alias.get("source_account_name") in {"刘萍karen"} else "影响账号身份与评分归并"
        lines.append(
            f"| {alias.get('source_account_name')} | {alias.get('target_account_name')} | "
            f"{alias.get('status')} | {alias.get('confidence')} | "
            f"{alias.get('shared_title_count')}/{alias.get('shared_url_count')} | {conclusion} |"
        )
    lines.append("")
    lines.append("## 黑马榜身份污染风险")
    lines.append("")
    lines.append("| 旧账号 | 新账号 | 新账号分 | 命中天数 | 连续命中 | 风险 |")
    lines.append("|---|---|---:|---:|---:|---|")
    for row in score_impact[:20]:
        lines.append(
            f"| {row.get('source_account_name')} | {row.get('target_account_name')} | "
            f"{row.get('target_score')} | {row.get('target_hit_days')} | "
            f"{row.get('target_current_streak')} | {row.get('risk')} |"
        )
    lines.append("")
    lines.append("## 查询级过滤样本")
    lines.append("")
    lines.append("| 账号 | 置信度 | 日期 | 丢失词数 | 保留词数 | 丢失关键词 |")
    lines.append("|---|---:|---|---:|---:|---|")
    query_events = [e for e in data["events"] if e.get("event_type") == "query_filter"]
    for event in query_events[:20]:
        lost = "、".join(event.get("lost_keywords", [])[:8])
        lines.append(
            f"| {event.get('account_name')} | {event.get('penalty_confidence')} | "
            f"{event.get('last_seen_date')} -> {event.get('zero_date')} | "
            f"{event.get('lost_keyword_count')} | {event.get('retained_keyword_count')} | {lost} |"
        )
    lines.append("")
    lines.append("## 差分高危词")
    lines.append("")
    lines.append("| 词 | log-odds | 消失标题计数 | 存活标题计数 |")
    lines.append("|---|---:|---:|---:|")
    for row in data["differential_terms"][:30]:
        lines.append(f"| {row['term']} | {row['log_odds']} | {row['punished_count']} | {row['control_count']} |")
    lines.append("")
    lines.append("## RPA 真值状态")
    lines.append("")
    rpa = data.get("rpa_title_checks", {})
    lines.append(f"- 状态：{rpa.get('status')}")
    lines.append(f"- 原因：{rpa.get('reason')}")
    lines.append("- 注意：只有 RPA/人工精确标题搜索能把 suspected_deindex 升级为 confirmed。")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build derived penalty/deindexing signals from normalized ranking data.")
    parser.add_argument("--min-coverage", type=int, default=70)
    parser.add_argument("--overlap-threshold", type=float, default=0.8)
    parser.add_argument("--min-previous-keywords", type=int, default=3)
    parser.add_argument("--output", default="normalized/penalty_signals.json")
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    builder = PenaltySignalBuilder(PROJECT_ROOT, args.min_coverage, args.overlap_threshold)
    data = builder.build(args.min_previous_keywords)
    output_path = PROJECT_ROOT / args.output
    dump_json(output_path, data)
    print(f"[ok] wrote {output_path}")
    print(f"[ok] events={data['summary']['event_count']} types={data['summary']['event_type_counts']}")

    if args.report:
        report_path = PROJECT_ROOT / args.report
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_report(data), encoding="utf-8")
        print(f"[ok] wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
