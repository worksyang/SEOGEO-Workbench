"""微信旧辅助页面兼容渲染。

这些页面原本由 Flask/Jinja 在旧系统中服务端渲染。工作台不能把未渲染的
Jinja 源码直接作为 HTML 返回，因此这里保留原模板块并用 Hub/冻结数据构造
等价上下文；模板文件仍是 legacy_mirrors 的只读镜像。
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from content_hub.repositories.wechat_legacy import WechatLegacyRepository
from content_hub.adapters.wechat import WechatAdapter, WechatSourceError

_PAGE_ROOT = Path(__file__).resolve().parents[2] / "frontend"
_PAGE_NAMES = {
    "keyword-turnover": "keyword_turnover.html",
    "article-hit-detail": "article_hit_detail.html",
    "account-score-analysis": "account_score_analysis.html",
    "account-score-formula": "account_score_formula.html",
}


def _frontend_legacy_root(request: Request) -> Path:
    dist_root = Path(request.app.state.settings.frontend_dist) / "legacy" / "wechat"
    if dist_root.is_dir():
        return dist_root
    return _PAGE_ROOT / "public" / "legacy" / "wechat"


def _template_root(request: Request) -> Path:
    mirror_root = request.app.state.settings.workbench_root / "legacy_mirrors" / "wechat" / "source" / "templates"
    return mirror_root


def _payload(request: Request) -> dict:
    """优先使用已导入 Hub 投影，缺失时仅读取当前隔离冻结目录。"""
    try:
        value = WechatLegacyRepository(request.app.state.settings).full()
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    try:
        value = WechatAdapter(request.app.state.settings).local_json("normalized/monitor-data.json")
        return value if isinstance(value, dict) else {}
    except (WechatSourceError, OSError, ValueError, json.JSONDecodeError):
        return {}


def _accounts(payload: dict) -> list[dict]:
    raw = payload.get("accounts") or []
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("canonical_name") or item.get("account_name") or "").strip()
        if not name:
            continue
        history = item.get("history") if isinstance(item.get("history"), list) else []
        keywords = item.get("keywords") if isinstance(item.get("keywords"), dict) else {}
        result.append({
            **item,
            "name": name,
            "score": round(float(item.get("score") or item.get("account_score") or 0), 2),
            "hit_days": sum(1 for rank in history if isinstance(rank, (int, float)) and rank > 0),
            "article_count": max(int(item.get("article_count") or 0), 0),
            "kw_count": max(int(item.get("kw_count") or len(keywords)), 0),
        })
    return result


def _analysis_context(request: Request) -> dict:
    payload = _payload(request)
    accounts = _accounts(payload)
    promoted_names = {"维港保典", "小冯妮儿聊港险", "kiki聊港险", "懂安Global"}
    demoted_names = {"Amy说港险", "中汇财富顾问", "友邦财F盈活"}
    promoted = [item for item in accounts if item["name"] in promoted_names]
    demoted = [item for item in accounts if item["name"] in demoted_names]
    if not promoted:
        promoted = accounts[: min(2, len(accounts))]
    if not demoted:
        demoted = accounts[2: min(4, len(accounts))]
    keywords = []
    for item in payload.get("keywords") or []:
        if isinstance(item, dict):
            value = str(item.get("keyword") or "").strip()
            if value:
                keywords.append(value)
    topic_specs = [
        ("财富盈活", "产品主主题。不同问法、对比词、提取词都应折叠到这里。", ("财富盈活", "AIA 财富盈活", "AIA Wealth", "友邦")),
        ("环宇盈活", "产品主主题。测评、缺陷、对比类词不应再拆成独立研究主题。", ("环宇盈活", "友邦 盈活")),
        ("信守明天", "分红实现率、收益、提领都可视为同一产品主题下的不同搜索意图。", ("信守明天", "保诚信守明天")),
        ("杠杆寿", "专题型主题，横评和各家公司杠杆寿词先归为一簇。", ("杠杆寿",)),
    ]
    topic_examples = []
    for topic, note, tokens in topic_specs:
        matched = sorted({word for word in keywords if any(token in word for token in tokens)})
        if matched:
            topic_examples.append({"topic": topic, "note": note, "keyword_count": len(matched), "keywords": matched[:8], "extra_count": max(len(matched) - 8, 0)})
    field_plan = [
        {"field": "topic", "where": "加在 keyword 上，作为主分组值", "decision": "必加", "why": "topic 只负责产品词归并，不再让它顺带承担搜索意图分类。", "example": "财富盈活"},
        {"field": "keyword_bucket", "where": "加在 keyword 上，作为搜索意图分类", "decision": "必加", "why": "它专门压掉伪广度，避免同一篇文章打中相似问法时被误判为覆盖很广。", "example": "热门单品 / 保费融资词 / 提领成交词"},
        {"field": "topic_registry", "where": "控制层配置，不属于抓取事实", "decision": "建议保留", "why": "主题归类是控制层判断，后续调整不应重跑解析。", "example": "SQLite 表或 JSON 配置"},
    ]
    dedupe_rules = [
        {"scene": "同一篇文章，同一天，打中多个变体关键词", "rule": "合并为 1 次主分，只看顺带覆盖的搜索类目", "impact": "同一内容覆盖多个问法，不代表研究价值翻倍。"},
        {"scene": "同一篇文章，同一天，被抓取多次", "rule": "合并为 1 次，取当天主题里的最好名次", "impact": "重复观测不是账号能力新增。"},
        {"scene": "同一账号，同一天，同主题，发了两篇不同文章", "rule": "保留 1 次主曝光，再给递减内容厚度加分", "impact": "不全砍，也不全额累计。"},
        {"scene": "同一篇文章，跨多天持续上榜", "rule": "按天保留", "impact": "持续性应该被奖励。"},
    ]
    scoring_blocks = [
        {"title": "第一层：日主题曝光分", "formula": "account + date + topic 这一格里，取最佳 rank_weight(best_rank)", "desc": "沿用现有排名权重逻辑，1 到 3 名偏时效，4 到 10 名保留长期价值。"},
        {"title": "第二层：同篇角度 bonus + 多文厚度 bonus", "formula": "topic_day_score = rank_weight(best_rank) + angle_bonus(primary_article_bucket_count) + extra_article_bonus(distinct_articles)", "desc": "同一篇文章跨多个 bucket 只给小额角度 bonus；不同文章给递减厚度 bonus。"},
        {"title": "第三层：持续性系数", "formula": "continuity = f(recent_hit_days, current_streak)", "desc": "更强调最近 7 天和当前连击，而不是全窗口历史功劳。"},
        {"title": "第四层：广度加法奖励", "formula": "final_score = base_score × continuity + topic_breadth_bonus + bucket_breadth_bonus", "desc": "广度改成加法奖励，避免头部账号被乘法无限放大。"},
    ]
    implementation_points = [
        {"title": "主题先一对一", "desc": "V1 约束为 1 个 keyword 对应 1 个 primary topic，降低实现熵增。"},
        {"title": "主题配置放控制层", "desc": "抓取结果是事实，主题归类是判断，两者不混在解析层。"},
        {"title": "去重分两层看", "desc": "原子去重用 account_id + date + topic + article_id，再聚合到 account_id + date + topic。"},
        {"title": "先把结构改对，再调系数", "desc": "先立住 topic、bucket、时间衰减、加法广度四层结构。"},
    ]
    return {
        "generated_at": payload.get("generated_at") or "2026-07-16T00:00:00Z",
        "window_days": int(payload.get("window_days") or 15),
        "promoted_accounts": promoted,
        "demoted_accounts": demoted,
        "topic_examples": topic_examples,
        "field_plan": field_plan,
        "dedupe_rules": dedupe_rules,
        "scoring_blocks": scoring_blocks,
        "implementation_points": implementation_points,
    }


def _formula_context() -> dict:
    weights = [10.0, 8.2, 6.8, 5.6, 4.6, 3.7, 3.0, 2.4, 1.9, 1.5]
    example_hits = [
        {"date": "2026-06-06", "topic": "财富盈活", "bucket": "热门单品 / 单品对比词 / 提领成交词", "article": "文章 A", "keywords": ["友邦财富盈活", "财富盈活环宇", "财富盈活 提取"], "ranks": [4, 7, 9], "counted_as": "同一篇文章、同一天只给 1 次主分；跨 3 个类目只给小额角度 bonus。"},
        {"date": "2026-06-06", "topic": "财富盈活", "bucket": "热门单品", "article": "文章 B", "keywords": ["AIA 财富盈活"], "ranks": [8], "counted_as": "同一天同 topic 的第 2 篇文章只给内容厚度 bonus。"},
        {"date": "2026-06-07", "topic": "财富盈活", "bucket": "风控审查词", "article": "文章 C", "keywords": ["财富盈活 缺点"], "ranks": [5], "counted_as": "跨天继续上榜，按新的 1 天记分且更近更重。"},
        {"date": "2026-06-08", "topic": "信守明天", "bucket": "热门单品", "article": "文章 D", "keywords": ["保诚信守明天"], "ranks": [6], "counted_as": "新产品 topic 带来产品广度。"},
    ]
    cells = [
        ("2026-06-06 × 财富盈活", 4, 3, 2, 0.82),
        ("2026-06-07 × 财富盈活", 5, 1, 1, 1.0),
        ("2026-06-08 × 信守明天", 6, 1, 1, 1.0),
    ]
    day_topic_cells = []
    for cell, rank, bucket_count, article_count, time_weight in cells:
        angle = round(max(bucket_count - 1, 0) * 0.35, 2)
        article_bonus = round(max(article_count - 1, 0) * 0.5, 2)
        raw = round(weights[rank - 1] + angle + article_bonus, 2)
        day_topic_cells.append({"cell": cell, "best_rank": rank, "best_weight": weights[rank - 1], "bucket_count": bucket_count, "bucket_bonus": angle, "distinct_articles": article_count, "article_bonus": article_bonus, "day_topic_score": raw, "time_weight": time_weight, "weighted_score": round(raw * time_weight, 2), "reason": f"主分看最好名次；跨 {bucket_count} 个类目给 {angle:.2f} 角度 bonus；第 {article_count} 篇文章给 {article_bonus:.2f} 内容厚度 bonus。"})
    base_score = round(sum(item["weighted_score"] for item in day_topic_cells), 2)
    continuity, breadth = 1.12, 3.25
    final_score = round(base_score * continuity + breadth, 2)
    return {
        "weights": [{"rank": i + 1, "weight": value} for i, value in enumerate(weights)],
        "wrong_old_example": [{"keyword": word, "rank": rank, "weight": weights[rank - 1]} for word, rank in [("友邦财富盈活", 4), ("财富盈活环宇", 7), ("财富盈活 提取", 9), ("财富盈活 缺点", 5), ("AIA 财富盈活", 8)]],
        "wrong_old_score": round(sum(weights[i - 1] for i in [4, 7, 9, 5, 8]), 2),
        "wrong_new_score": round(weights[3] + 0.7, 2),
        "term_explanations": [
            {"name": "topic", "meaning": "产品词，回答账号打中了几个产品方向。", "example": "友邦财富盈活、AIA 财富盈活归到 topic = 财富盈活。"},
            {"name": "keyword_bucket", "meaning": "搜索意图类别，回答账号打中了几个搜索角度。", "example": "热门单品、单品对比词、提领成交词、保费融资词。"},
            {"name": "best_rank", "meaning": "同一天同主题只认最强的一次主曝光。", "example": "同一篇文章当天打到第 4、7、9 名，只认第 4 名。"},
            {"name": "recent_hit_days", "meaning": "最近 7 天真正有命中的天数。", "example": "让 7 天没上榜的老号自然回落。"},
            {"name": "current_streak", "meaning": "从今天往回数的连续命中天数。", "example": "黑马连续 3 天命中时明显抬头。"},
            {"name": "topic_count / bucket_count", "meaning": "产品广度与搜索角度广度。", "example": "奖励递减，不会无限放大。"},
        ],
        "example_hits": example_hits, "day_topic_cells": day_topic_cells, "base_score": base_score,
        "hit_days": 3, "recent_hit_days": 3, "current_streak": 3, "longest_streak": 3,
        "topic_count": 2, "bucket_count": 3, "continuity": continuity, "breadth": breadth,
        "topic_bonus": 1.75, "bucket_bonus": 1.5, "final_score": final_score,
        "formula_lines": ["topic_day_raw_score = rank_weight(best_rank) + angle_bonus(primary_article_bucket_count) + extra_article_bonus(distinct_articles)", "weighted_day_score = topic_day_raw_score × recency_weight(day_age)", "base_score = sum(all weighted_day_score)", "continuity = f(recent_hit_days, current_streak)", "final_account_score = base_score × continuity + topic_breadth_bonus + bucket_breadth_bonus"],
        "no_double_count_cases": ["同一篇文章同一天打中多个相似关键词，不再吃满多次 rank 分。", "同一篇文章跨多个类目，只给小额 angle bonus。", "同一天被抓取多次，只认有效表现。"],
        "yes_count_cases": ["跨天继续上榜，按新的 1 天继续记分。", "同一天同 topic 出现不同文章，获得递减内容厚度加分。", "最近覆盖更多产品和类目，获得加法型广度奖励。", "最近连续命中的黑马因 current_streak 抬升。"],
    }


def _render_page(request: Request, page: str, context: dict) -> HTMLResponse:
    filename = _PAGE_NAMES[page]
    environment = Environment(
        loader=FileSystemLoader(_template_root(request)),
        autoescape=select_autoescape(("html", "xml")),
        keep_trailing_newline=True,
    )
    html = environment.get_template(filename).render(**context)
    return HTMLResponse(html)


def wechat_static_page(request: Request, page: str) -> HTMLResponse:
    if page == "account-score-analysis":
        return _render_page(request, page, _analysis_context(request))
    if page == "account-score-formula":
        return _render_page(request, page, _formula_context())
    filename = _PAGE_NAMES[page]
    from fastapi.responses import FileResponse
    return FileResponse(_frontend_legacy_root(request) / filename, media_type="text/html")


def wechat_keyword_turnover(request: Request) -> FileResponse:
    return wechat_static_page(request, "keyword-turnover")


def wechat_article_hit_detail(request: Request) -> FileResponse:
    return wechat_static_page(request, "article-hit-detail")


def wechat_account_score_analysis(request: Request) -> FileResponse:
    return wechat_static_page(request, "account-score-analysis")


def wechat_account_score_formula(request: Request) -> FileResponse:
    return wechat_static_page(request, "account-score-formula")


def wechat_article_detail_demo(request: Request) -> RedirectResponse:
    """保持旧 demo 链接语义，转到真实文章详情页面。"""
    query = {"article_id": "art_749d447ea394", "wbv": "wechat-v1"}
    return RedirectResponse(
        url="/legacy/wechat/article-hit-detail?" + urlencode(query),
        status_code=307,
    )


def wechat_article_detail_demo_root(request: Request) -> RedirectResponse:
    """根路径别名保持旧 Flask 的重定向目标。"""
    return RedirectResponse(
        url="/article-hit-detail?article_id=art_749d447ea394&wbv=wechat-v1",
        status_code=307,
    )
