from __future__ import annotations

from app.ingest.builders.monitor_builder import (
    angle_bonus,
    bucket_breadth_bonus,
    continuity_multiplier,
    extra_article_bonus,
    rank_weight,
    topic_breadth_bonus,
)


def build_account_score_formula_context() -> dict:
    weights = [{"rank": rank, "weight": round(rank_weight(rank), 2)} for rank in range(1, 11)]

    example_hits = [
        {
            "date": "2026-06-06",
            "topic": "财富盈活",
            "bucket": "热门单品 / 单品对比词 / 提领成交词",
            "article": "文章 A",
            "keywords": ["友邦财富盈活", "财富盈活环宇", "财富盈活 提取"],
            "ranks": [4, 7, 9],
            "counted_as": "还是同一篇文章，同一天只给 1 次主分；跨了 3 个搜索类目，只给小额角度 bonus。",
        },
        {
            "date": "2026-06-06",
            "topic": "财富盈活",
            "bucket": "热门单品",
            "article": "文章 B",
            "keywords": ["AIA 财富盈活"],
            "ranks": [8],
            "counted_as": "同一天同 topic 的第 2 篇不同文章，不再给满第二次 rank 分，只给内容厚度加分。",
        },
        {
            "date": "2026-06-07",
            "topic": "财富盈活",
            "bucket": "风控审查词",
            "article": "文章 C",
            "keywords": ["财富盈活 缺点"],
            "ranks": [5],
            "counted_as": "跨天继续上榜，会按新的 1 天重新记分，而且因为更近，时间权重更高。",
        },
        {
            "date": "2026-06-08",
            "topic": "信守明天",
            "bucket": "热门单品",
            "article": "文章 D",
            "keywords": ["保诚信守明天"],
            "ranks": [6],
            "counted_as": "这是新产品 topic，会带来新的产品广度。",
        },
    ]

    wrong_old_example = [
        {"keyword": "友邦财富盈活", "rank": 4, "weight": round(rank_weight(4), 2)},
        {"keyword": "财富盈活环宇", "rank": 7, "weight": round(rank_weight(7), 2)},
        {"keyword": "财富盈活 提取", "rank": 9, "weight": round(rank_weight(9), 2)},
        {"keyword": "财富盈活 缺点", "rank": 5, "weight": round(rank_weight(5), 2)},
        {"keyword": "AIA 财富盈活", "rank": 8, "weight": round(rank_weight(8), 2)},
    ]
    wrong_old_score = round(sum(item["weight"] for item in wrong_old_example), 2)
    wrong_new_score = round(rank_weight(4) + angle_bonus(3), 2)

    day_topic_cells = [
        {
            "cell": "2026-06-06 × 财富盈活",
            "best_rank": 4,
            "best_weight": round(rank_weight(4), 2),
            "bucket_count": 3,
            "bucket_bonus": round(angle_bonus(3), 2),
            "distinct_articles": 2,
            "article_bonus": round(extra_article_bonus(2), 2),
            "time_weight": 0.82,
            "day_topic_score": round(rank_weight(4) + angle_bonus(3) + extra_article_bonus(2), 2),
        },
        {
            "cell": "2026-06-07 × 财富盈活",
            "best_rank": 5,
            "best_weight": round(rank_weight(5), 2),
            "bucket_count": 1,
            "bucket_bonus": round(angle_bonus(1), 2),
            "distinct_articles": 1,
            "article_bonus": round(extra_article_bonus(1), 2),
            "time_weight": 1.0,
            "day_topic_score": round(rank_weight(5) + angle_bonus(1) + extra_article_bonus(1), 2),
        },
        {
            "cell": "2026-06-08 × 信守明天",
            "best_rank": 6,
            "best_weight": round(rank_weight(6), 2),
            "bucket_count": 1,
            "bucket_bonus": round(angle_bonus(1), 2),
            "distinct_articles": 1,
            "article_bonus": round(extra_article_bonus(1), 2),
            "time_weight": 1.0,
            "day_topic_score": round(rank_weight(6) + angle_bonus(1) + extra_article_bonus(1), 2),
        },
    ]
    for item in day_topic_cells:
        item["weighted_score"] = round(item["day_topic_score"] * item["time_weight"], 2)
        item["reason"] = (
            f"主分看最好名次；同一篇文章跨 {item['bucket_count']} 个类目只给 {item['bucket_bonus']:.2f} "
            f"的角度 bonus；第 {item['distinct_articles']} 篇文章以后只给内容厚度 bonus。"
        )

    base_score = round(sum(item["weighted_score"] for item in day_topic_cells), 2)
    recent_hit_days = 3
    current_streak = 3
    hit_days = 3
    longest_streak = 3
    topic_count = 2
    bucket_count = 3
    continuity = continuity_multiplier(recent_hit_days, current_streak)
    topic_bonus = topic_breadth_bonus(topic_count)
    bucket_bonus = bucket_breadth_bonus(bucket_count)
    final_score = round(base_score * continuity + topic_bonus + bucket_bonus, 2)

    term_explanations = [
        {
            "name": "topic",
            "meaning": "就是产品词。它回答的是：这个账号打中了几个产品方向。",
            "example": "友邦财富盈活、AIA 财富盈活，都会归到 topic = 财富盈活。",
        },
        {
            "name": "keyword_bucket",
            "meaning": "就是搜索意图类别。它回答的是：这个账号打中了几个搜索角度。",
            "example": "热门单品、单品对比词、提领成交词、保费融资词。",
        },
        {
            "name": "best_rank",
            "meaning": "同一天同主题里，只认最强的一次主曝光。",
            "example": "同一篇文章当天打到第 4、第 7、第 9 名，只认第 4 名。",
        },
        {
            "name": "recent_hit_days",
            "meaning": "最近 7 天里，这个账号真正有命中的天数。",
            "example": "这是为了让 7 天没上榜的老号自然掉下去。",
        },
        {
            "name": "current_streak",
            "meaning": "从今天往回数，连续命中了几天。",
            "example": "黑马如果连着 3 天爆发，这里就会明显变大。",
        },
        {
            "name": "topic_count / bucket_count",
            "meaning": "前者看产品广度，后者看搜索角度广度。",
            "example": "会 10 个产品比会 5 个产品更强；但奖励会递减，不会无限放大。",
        },
    ]

    no_double_count_cases = [
        "同一篇文章，同一天，打中多个相似关键词，不再吃满多次 rank 分。",
        "同一篇文章跨多个搜索类目，只给小额 angle bonus，不再每个类目都吃满主分。",
        "同一天被抓取多次，仍然只认当天主快照里的有效表现。",
    ]

    yes_count_cases = [
        "跨天继续上榜，会按新的 1 天继续记分，而且越近越重。",
        "同一天同 topic 下出现第 2 篇、第 3 篇不同文章，会拿到递减的内容厚度加分。",
        "最近 7 天覆盖更多产品、更多搜索类目，会拿到加法型广度奖励。",
        "最近连着 2 到 3 天都很强的黑马，会因为 current_streak 快速冲上来。",
    ]

    formula_lines = [
        "topic_day_raw_score = rank_weight(best_rank) + angle_bonus(primary_article_bucket_count) + extra_article_bonus(distinct_articles)",
        "weighted_day_score = topic_day_raw_score × recency_weight(day_age)",
        "base_score = sum(all weighted_day_score)",
        "continuity = f(recent_hit_days, current_streak)",
        "final_account_score = base_score × continuity + topic_breadth_bonus + bucket_breadth_bonus",
    ]

    return {
        "weights": weights,
        "wrong_old_example": wrong_old_example,
        "wrong_old_score": wrong_old_score,
        "wrong_new_score": wrong_new_score,
        "term_explanations": term_explanations,
        "example_hits": example_hits,
        "day_topic_cells": day_topic_cells,
        "base_score": base_score,
        "hit_days": hit_days,
        "recent_hit_days": recent_hit_days,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "topic_count": topic_count,
        "bucket_count": bucket_count,
        "continuity": continuity,
        "breadth": round(topic_bonus + bucket_bonus, 2),
        "topic_bonus": topic_bonus,
        "bucket_bonus": bucket_bonus,
        "final_score": final_score,
        "formula_lines": formula_lines,
        "no_double_count_cases": no_double_count_cases,
        "yes_count_cases": yes_count_cases,
    }
