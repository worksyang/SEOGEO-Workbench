from __future__ import annotations

from typing import Any


PROMOTED_ACCOUNT_NAMES = [
    "维港保典",
    "小冯妮儿聊港险",
    "kiki聊港险",
    "懂安Global",
]

DEMOTED_ACCOUNT_NAMES = [
    "Amy说港险",
    "中汇财富顾问",
    "友邦财F盈活",
]

TOPIC_EXAMPLE_SPECS = [
    {
        "topic": "财富盈活",
        "note": "产品主主题。不同问法、对比词、提取词都应折叠到这里。",
        "tokens": ["财富盈活", "AIA 财富盈活", "AIA Wealth", "友邦 财富盈活", "友邦财富盈活"],
    },
    {
        "topic": "环宇盈活",
        "note": "产品主主题。测评、缺陷、对比类词不应再拆成独立研究主题。",
        "tokens": ["环宇盈活", "友邦 盈活 对比 环宇"],
    },
    {
        "topic": "信守明天",
        "note": "分红实现率、收益、提领都可视为同一产品主题下的不同搜索意图。",
        "tokens": ["信守明天", "保诚信守明天"],
    },
    {
        "topic": "杠杆寿",
        "note": "这是专题型主题，不是单一产品。横评和各家公司杠杆寿词可以先归为一簇。",
        "tokens": ["杠杆寿"],
    },
]


def _enrich_account(item: dict[str, Any]) -> dict[str, Any]:
    history = item.get("history", [])
    hit_days = sum(1 for rank in history if rank > 0)
    article_count = max(int(item.get("article_count", 0)), 0)
    kw_count = max(int(item.get("kw_count", 0)), 0)
    score = round(float(item.get("score", 0)), 2)
    keyword_names = list(item.get("keywords", {}).keys())
    return {
        **item,
        "score": score,
        "hit_days": hit_days,
        "article_count": article_count,
        "kw_count": kw_count,
        "keyword_names": keyword_names,
        "keyword_preview": keyword_names[:6],
        "extra_keyword_count": max(len(keyword_names) - 6, 0),
    }


def _pick_accounts(accounts: list[dict[str, Any]], names: list[str]) -> list[dict[str, Any]]:
    by_name = {item["name"]: item for item in accounts}
    return [by_name[name] for name in names if name in by_name]


def _build_topic_examples(payload: dict[str, Any]) -> list[dict[str, Any]]:
    keyword_texts = [item["keyword"] for item in payload.get("keywords", [])]
    topic_examples: list[dict[str, Any]] = []
    for spec in TOPIC_EXAMPLE_SPECS:
        matched = sorted(
            [
                keyword
                for keyword in keyword_texts
                if any(token in keyword for token in spec["tokens"])
            ]
        )
        if not matched:
            continue
        topic_examples.append(
            {
                "topic": spec["topic"],
                "note": spec["note"],
                "keyword_count": len(matched),
                "keywords": matched[:8],
                "extra_count": max(len(matched) - 8, 0),
            }
        )
    return topic_examples


def build_account_score_analysis_context(payload: dict[str, Any]) -> dict[str, Any]:
    accounts = [_enrich_account(item) for item in payload.get("accounts", [])]
    promoted_accounts = _pick_accounts(accounts, PROMOTED_ACCOUNT_NAMES)
    demoted_accounts = _pick_accounts(accounts, DEMOTED_ACCOUNT_NAMES)
    topic_examples = _build_topic_examples(payload)

    field_plan = [
        {
            "field": "topic",
            "where": "加在 keyword 上，作为主分组值",
            "decision": "必加",
            "why": "topic 继续只负责产品词归并，不再让它顺带承担搜索意图分类。",
            "example": "财富盈活",
        },
        {
            "field": "keyword_bucket",
            "where": "加在 keyword 上，作为搜索意图分类",
            "decision": "必加",
            "why": "它专门负责压掉伪广度，让同一篇文章打中很多相似问法时不要被误判成覆盖很广。",
            "example": "热门单品 / 保费融资词 / 提领成交词",
        },
        {
            "field": "topic_registry",
            "where": "控制层配置，不属于抓取事实",
            "decision": "建议保留",
            "why": "topic 和 keyword_bucket 都是控制层判断，后面会不断微调，不能写死在解析层。",
            "example": "SQLite 表或 JSON 配置均可",
        },
    ]

    dedupe_rules = [
        {
            "scene": "同一篇文章，同一天，打中多个变体关键词",
            "rule": "合并为 1 次主分，只看它顺带覆盖了多少搜索类目",
            "impact": "这只是同一内容覆盖了多个问法，不代表研究价值翻倍。",
        },
        {
            "scene": "同一篇文章，同一天，被抓取多次",
            "rule": "合并为 1 次，取当天该主题里的最好名次",
            "impact": "这只是重复观测，不是账号能力新增。",
        },
        {
            "scene": "同一账号，同一天，同主题，发了两篇不同文章",
            "rule": "保留 1 次主曝光，再给递减的内容厚度加分",
            "impact": "不能全砍，否则低估内容厚度；也不能全额累计，否则会鼓励刷屏。",
        },
        {
            "scene": "同一篇文章，跨多天持续上榜",
            "rule": "按天保留",
            "impact": "这恰恰是持续性，应该被奖励。",
        },
    ]

    scoring_blocks = [
        {
            "title": "第一层：日主题曝光分",
            "formula": "account + date + topic 这一格里，取最佳 rank_weight(best_rank)",
            "desc": "这里继续沿用现有的排名权重逻辑，1 到 3 名偏时效，4 到 10 名更有价值。",
        },
        {
            "title": "第二层：同篇角度 bonus + 多文厚度 bonus",
            "formula": "topic_day_score = rank_weight(best_rank) + angle_bonus(primary_article_bucket_count) + extra_article_bonus(distinct_articles)",
            "desc": "同一篇文章跨多个 bucket 只给很小的角度 bonus；第 2 篇、第 3 篇不同文章给递减厚度 bonus。",
        },
        {
            "title": "第三层：持续性系数",
            "formula": "continuity = f(recent_hit_days, current_streak)",
            "desc": "持续性不再看全 15 天的历史功劳，而是更强调最近 7 天和当前连击。",
        },
        {
            "title": "第四层：广度加法奖励",
            "formula": "final_score = base_score × continuity + topic_breadth_bonus + bucket_breadth_bonus",
            "desc": "广度继续奖励，但不再整段乘上去，避免头部账号越强越夸张。",
        },
    ]

    implementation_points = [
        {
            "title": "主题先一对一",
            "desc": "V1 先约束为 1 个 keyword 只对应 1 个 primary topic。这样实现最稳，也最不容易熵增。",
        },
        {
            "title": "主题配置放控制层",
            "desc": "抓取结果是事实，主题归类是判断。两者不要混在一起，后续改主题不该重跑解析。",
        },
        {
            "title": "去重分两层看",
            "desc": "原子去重键可以是 account_id + date + topic + article_id；真正计分时，再聚合到 account_id + date + topic。",
        },
        {
            "title": "先把结构改对，再调系数",
            "desc": "当前最重要的不是把某个系数调到完美，而是先把 topic、bucket、时间衰减、加法广度这四层结构立住。",
        },
    ]

    return {
        "generated_at": payload.get("generated_at", ""),
        "window_days": payload.get("window_days", 0),
        "promoted_accounts": promoted_accounts,
        "demoted_accounts": demoted_accounts,
        "topic_examples": topic_examples,
        "field_plan": field_plan,
        "dedupe_rules": dedupe_rules,
        "scoring_blocks": scoring_blocks,
        "implementation_points": implementation_points,
    }
