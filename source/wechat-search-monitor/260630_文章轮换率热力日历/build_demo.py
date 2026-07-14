"""导出“友邦财富盈活”最近 30 天的快照数据，供热力日历 Demo 使用。

产物：demo.json
- keyword: 关键词元数据
- runs: 最近 30 天每次快照的精简结构
- daily_rates: 相邻快照的轮换率派生结果
- articles: 用于寿命热力图展示的文章（已过滤“单日单次”噪音）
- article_presence / article_day_runs: 前端渲染寿命热力图所需矩阵

事实层：runs 直接来自 normalized/monitor-data.json，不改写原始快照
派生层：轮换率、稳定度、寿命矩阵、过滤规则全部在这里一次性算好
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "normalized" / "monitor-data.json"
DST = ROOT / "260630_文章轮换率热力日历" / "demo.json"


STABILITY_RANK = {"常驻": 3, "活跃": 2, "闪现": 1}


def article_key(article: dict) -> str:
    return article.get("article_id") or f"{article.get('title', '')}|{article.get('url', '')}"


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def longest_streak(days: list[str]) -> int:
    uniq_days = sorted(set(days))
    if not uniq_days:
        return 0

    best = 1
    current = 1
    for idx in range(1, len(uniq_days)):
        prev_day = parse_date(uniq_days[idx - 1])
        curr_day = parse_date(uniq_days[idx])
        if (curr_day - prev_day).days == 1:
            current += 1
        else:
            best = max(best, current)
            current = 1
    return max(best, current)


def classify_stability(*, day_ratio: float, run_ratio: float, max_streak: int) -> str:
    """按“按天覆盖 + 连续驻留 + 快照命中”三维口径分类。

    这里不再用旧版的“出现天数 / 快照数”错位口径，
    否则 31 天窗口里就算覆盖了 17 天，也会被 61 次快照稀释成 27.9%。
    """

    if day_ratio >= 0.32 and (max_streak >= 5 or run_ratio >= 0.30):
        return "常驻"
    if day_ratio >= 0.16 or max_streak >= 3 or run_ratio >= 0.18:
        return "活跃"
    return "闪现"


def main() -> None:
    with SRC.open() as f:
        data = json.load(f)

    target = next(
        (item for item in data["keywords"] if item.get("keyword") == "友邦财富盈活"),
        None,
    )
    if not target:
        raise SystemExit("未找到关键词：友邦财富盈活")

    all_runs = sorted(
        [run for run in target.get("runs", []) if run.get("articles")],
        key=lambda run: f"{run['date']} {run.get('time', '')}",
    )
    if not all_runs:
        raise SystemExit("该关键词没有任何快照")

    last_date = parse_date(all_runs[-1]["date"])
    cutoff = last_date - timedelta(days=30)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    window_runs = [run for run in all_runs if run["date"] >= cutoff_str]

    dates = [
        (cutoff + timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range((last_date - cutoff).days + 1)
    ]
    total_days = len(dates)
    total_runs = len(window_runs)
    day_snapshot_counts = Counter(run["date"] for run in window_runs)

    daily_rates = []
    for idx in range(1, total_runs):
        curr_ids = set(map(article_key, window_runs[idx]["articles"]))
        prev_ids = set(map(article_key, window_runs[idx - 1]["articles"]))
        if not curr_ids or not prev_ids:
            continue

        new_count = len(curr_ids - prev_ids)
        gone_count = len(prev_ids - curr_ids)
        rate = (new_count + gone_count) / (len(curr_ids) + len(prev_ids))
        daily_rates.append(
            {
                "date": window_runs[idx]["date"],
                "time": window_runs[idx]["time"],
                "curr_count": len(curr_ids),
                "prev_count": len(prev_ids),
                "same": len(curr_ids & prev_ids),
                "new": new_count,
                "gone": gone_count,
                "rate": round(rate, 4),
            }
        )

    avg_rate = sum(item["rate"] for item in daily_rates) / len(daily_rates) if daily_rates else 0

    article_meta: dict[str, dict] = {}
    article_day_runs: dict[str, Counter] = defaultdict(Counter)
    for run in window_runs:
        for article in run["articles"]:
            key = article_key(article)
            if key not in article_meta:
                article_meta[key] = {
                    "article_id": key,
                    "title": article.get("title", ""),
                    "account": article.get("account", ""),
                    "first_seen": run["date"],
                }

            article_day_runs[key][run["date"]] += 1
            article_meta[key]["last_seen"] = run["date"]
            article_meta[key]["account"] = article_meta[key].get("account") or article.get("account", "")
            article_meta[key]["latest_rank"] = article.get("rank")

    visible_articles = []
    hidden_singletons = 0
    for key, meta in article_meta.items():
        day_runs = article_day_runs[key]
        active_days = sorted(day_runs)
        day_count = len(active_days)
        run_appearances = sum(day_runs.values())
        day_ratio = day_count / total_days if total_days else 0
        run_ratio = run_appearances / total_runs if total_runs else 0
        max_streak = longest_streak(active_days)
        stability = classify_stability(
            day_ratio=day_ratio,
            run_ratio=run_ratio,
            max_streak=max_streak,
        )

        meta.update(
            {
                "appearances": day_count,
                "day_count": day_count,
                "run_appearances": run_appearances,
                "presence_ratio": round(day_ratio, 3),
                "day_presence_ratio": round(day_ratio, 3),
                "run_presence_ratio": round(run_ratio, 3),
                "longest_streak": max_streak,
                "stability": stability,
                "stability_rank": STABILITY_RANK[stability],
                "active_days": active_days,
            }
        )

        if day_count == 1 and run_appearances == 1:
            hidden_singletons += 1
            continue

        visible_articles.append(meta)

    articles_sorted = sorted(
        visible_articles,
        key=lambda meta: (
            -meta["stability_rank"],
            -meta["day_count"],
            -meta["run_appearances"],
            -meta["longest_streak"],
            -parse_date(meta["last_seen"]).timestamp(),
            meta["title"],
        ),
    )

    article_presence = {
        article["article_id"]: {
            day: 1 if day in article_day_runs[article["article_id"]] else 0
            for day in dates
        }
        for article in articles_sorted
    }
    article_day_runs_export = {
        article["article_id"]: dict(article_day_runs[article["article_id"]])
        for article in articles_sorted
    }
    stability_counts = Counter(article["stability"] for article in articles_sorted)

    runs_compact = [
        {
            "id": run["id"],
            "date": run["date"],
            "time": run["time"],
            "is_primary": run.get("is_primary", False),
            "trigger_type": run.get("trigger_type", "manual"),
            "result_count": run.get("result_count", len(run["articles"])),
            "article_ids": [article_key(article) for article in run["articles"]],
        }
        for run in window_runs
    ]

    out = {
        "generated_at": data.get("generated_at"),
        "window_days": 30,
        "window_start": cutoff_str,
        "window_end": last_date.strftime("%Y-%m-%d"),
        "keyword": {
            "text": target["keyword"],
            "keyword_id": target["keyword_id"],
        },
        "summary": {
            "snapshot_count": total_runs,
            "comparison_count": len(daily_rates),
            "avg_rate": round(avg_rate, 4),
            "avg_rate_pct": round(avg_rate * 100, 1),
            "distinct_articles": len(articles_sorted),
            "distinct_articles_total": len(article_meta),
            "hidden_singletons": hidden_singletons,
            "stability_counts": {
                "常驻": stability_counts.get("常驻", 0),
                "活跃": stability_counts.get("活跃", 0),
                "闪现": stability_counts.get("闪现", 0),
            },
            "level": (
                "内容活跃"
                if avg_rate >= 0.3
                else "内容稳态"
                if avg_rate >= 0.1
                else "内容固化"
            ),
        },
        "dates": dates,
        "day_snapshot_counts": dict(day_snapshot_counts),
        "runs": runs_compact,
        "daily_rates": daily_rates,
        "articles": articles_sorted,
        "article_presence": article_presence,
        "article_day_runs": article_day_runs_export,
    }

    DST.parent.mkdir(parents=True, exist_ok=True)
    with DST.open("w") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print(f"✅ 导出 {DST.relative_to(ROOT)}")
    print(f"   窗口: {out['window_start']} ~ {out['window_end']}（{out['window_days']} 天）")
    print(f"   快照: {out['summary']['snapshot_count']} 次 · 对比: {out['summary']['comparison_count']} 次")
    print(
        "   文章: "
        f"展示 {out['summary']['distinct_articles']} 篇 / 总计 {out['summary']['distinct_articles_total']} 篇"
        f" · 隐藏单日单次 {out['summary']['hidden_singletons']} 篇"
    )
    print(
        "   稳定度: "
        f"常驻 {out['summary']['stability_counts']['常驻']} · "
        f"活跃 {out['summary']['stability_counts']['活跃']} · "
        f"闪现 {out['summary']['stability_counts']['闪现']}"
    )
    print(f"   平均轮换率: {out['summary']['avg_rate_pct']}%")


if __name__ == "__main__":
    main()