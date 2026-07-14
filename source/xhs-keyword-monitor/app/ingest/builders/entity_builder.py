"""实体构建器 — 小红书 provider envelope → normalized 实体。

事实层最小事实句（来自规范第六章）：
  keyword -> snapshot -> ranking_hit -> article -> account
事实字段映射：
  - workId          → article_id
  - workTitle       → article.title
  - workDesc        → article.summary
  - workUrl         → article.url
  - workPublishTime → article.published_at
  - accountUserid   → account.account_id
  - accountNickname → account.name
  - workLiked/Collected/Comments/Shared → article_metric_observation
排名 1..N 来自搜索结果列表顺序。

所有 raw 字段（包括搜索快照原文）都会被保留，存到 normalized/articles[*].platform_payload
和 normalized/accounts[*].platform_payload，方便审计和未来重算。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.ingest.common import TZ, art_id, kw_id, now_iso, parse_captured_at_iso, parse_published_at, to_iso
from app.ingest.redfox.envelope import (
    build_content_item,
    build_creator_item,
    ContentItem,
    CreatorItem,
    SnapshotEnvelope,
)


def _provider_sources(source_version: str | None) -> tuple[str, str]:
    """Return normalized provider and observation source names.

    The builders are shared by TikHub and the legacy RedFox adapter, so source
    provenance must come from the envelope instead of being hard-coded.
    """
    version = str(source_version or "").strip().lower()
    if version.startswith("tikhub"):
        return "tikhub_xhs", "tikhub_xhs_search_notes"
    if version.startswith("redfox"):
        return "redfox_xhs", "redfox_xhs_searchArticle"
    provider = version.split("_", 1)[0] if version else "unknown"
    return f"{provider}_xhs", f"{provider}_xhs_search"


# XHS 噪音词（地名/商场/弱关联词），命中则降权
NEGATIVE_NOISE_TOKENS = {
    "环宇城", "环宇天地", "环宇荟", "中海环宇城", "环宇购物中心",
    "万象城", "万达广场", "万达", "购物中心", "购物广场", "商场",
    "写字楼", "商场美陈", "美陈", "商场打卡", "打卡",
    "广告", "探店", "推荐官", "薅羊毛",
}


def _split_company_product(keyword: str) -> tuple[list[str], list[str], str]:
    """将关键词拆分为 (公司词, 产品词, 强产品词)。

    通用启发式:
    - 包含「保险/保单/年金/寿险/万用险/IUL/分红险/重疾/医疗/教育金/养老/传承」
      或「保险规划/理财/投资/家族/信托/CRS/身份/护照」 → 该 token 视为「产品/服务词」
    - 含「保诚/友邦/安盛/宏利/富卫/永明/万通/国寿/中银/泰禾/光大/工银/建行」 → 公司
    - 含「盈活/盈聚/盈家/盈耀/尊享/创富/尚裕/卓裕/传家/传世/飞越/传承/守护/智悦/卓达/活然/活悦/悦享/享悦/心安/富裕」→ 强产品词
    - 其它 → 普通 token
    """
    company_tokens = [
        "友邦", "保诚", "安盛", "宏利", "富卫", "永明", "万通", "国寿", "中银", "泰禾",
        "太平", "太平洋", "苏黎世", "周大福", "蚂蚁", "广发", "汇丰", "新加坡", "Singlife",
    ]
    product_tokens = [
        "保险", "保单", "年金", "寿险", "万用险", "万用寿险", "IUL", "分红险", "重疾",
        "医疗", "教育金", "养老", "传承", "杠杆寿", "杠杆寿险", "保费融资", "趸交",
        "短缴", "2年缴", "3年缴", "5年缴", "美元保单", "人民币保单", "多币种",
        "货币转换", "红利锁定", "红利解锁", "价值保障", "双货币户", "财富管家",
        "自主入息", "未来心愿", "传承守护", "第二受保人", "第二持有人", "后备持有人",
        "保单分拆", "金信托", "类信托", "身故分期", "高客", "保单功能", "缴费结构",
        "CRS", "护照", "身份", "信托", "家族", "理财", "投资",
    ]
    strong_product_tokens = [
        "盈活", "盈聚", "盈家", "盈耀", "尊享", "创富", "尚裕", "卓裕", "传家", "传世",
        "飞越", "传承", "守护", "智悦", "卓达", "活然", "活悦", "悦享", "享悦", "富裕",
        "环宇", "盛利", "星河", "宏挚", "匠心", "傲珑", "丰饶", "充裕", "鑫安逸",
        "信守", "创逸", "万用", "鼎峰", "薪火", "非凡", "世誉", "骏誉", "启德",
    ]
    k = keyword
    found_company = [t for t in company_tokens if t in k]
    found_strong = [t for t in strong_product_tokens if t in k]
    found_product = [t for t in product_tokens if t in k and t not in found_company and t not in found_strong]
    return found_company, found_product, " ".join(found_strong) if found_strong else ""


def _compute_relevance(keyword_text: str, title: str | None, summary: str | None) -> tuple[bool, float]:
    """XHS 相关性打分（不删除原始数据，仅标记）。

    严格规则（避免「环宇城」被「友邦环宇」误判为 1.0）：

    1. 关键词完整出现于标题或正文 → 1.0
    2. 拆分为 (公司词, 产品词, 强产品词) 至少命中一个
       - 命中强产品词 + (公司词或产品词) → 1.0
       - 仅命中公司词或产品词 → 0.6
       - 仅命中强产品词 → 0.4
    3. 命中强产品词但同时含「环宇城/中海/万象/万达」等地名噪音 → 强制 ≤ 0.3
    4. 其它 → 0.0
    """
    kw = (keyword_text or "").strip()
    title = (title or "").strip()
    summary = (summary or "").strip()
    blob = (title + " " + summary).lower()
    if not kw:
        return True, 1.0

    # 规则 1: 完整命中
    if kw in title or kw in summary:
        return True, 1.0

    companies, products, strong_products = _split_company_product(kw)
    has_company = any(c in blob for c in companies)
    has_product = any(p in blob for p in products)
    has_strong = any(s in blob for s in strong_products.split()) if strong_products else False

    # 噪音检查
    has_noise = any(n in blob for n in NEGATIVE_NOISE_TOKENS)

    # 规则 3: 命中噪音词 + 仅命中强产品词 → 强制低分（避免「友邦环宇盈活」被「环宇城广告」误判）
    if has_noise and not (has_company or has_product):
        if has_strong:
            return False, 0.2
        return False, 0.0
    if has_noise and has_strong and not (has_company or has_product):
        return False, 0.2

    # 规则 2: 加权
    score = 0.0
    if has_strong and (has_company or has_product):
        score = 1.0
    elif has_strong and (has_company or has_product):
        score = 1.0
    elif has_strong:
        score = 0.4
    elif has_company or has_product:
        score = 0.6
    else:
        # fallback: token 命中（仅 4+ 字关键词的前缀子串）
        if len(kw) >= 4:
            prefix = kw[: max(2, len(kw) * 2 // 3)]
            if prefix in blob:
                score = 0.3
        else:
            score = 0.0

    is_relevant = score >= 0.6
    return is_relevant, round(score, 2)

def _normalize_captured_at(captured_at: str | datetime) -> datetime:
    if isinstance(captured_at, datetime):
        return captured_at
    try:
        return parse_captured_at_iso(captured_at)
    except Exception:
        return datetime.now(TZ)


def build_entities(envelopes: list[SnapshotEnvelope]) -> dict[str, Any]:
    """把若干 SnapshotEnvelope 归并成完整 normalized 实体集合。

    返回:
      keywords / snapshots / snapshot_terms / accounts / articles /
      ranking_hits / note_metric_observations
    """
    envelopes = sorted(envelopes, key=lambda e: (e.keyword, _normalize_captured_at(e.captured_at)))

    keywords_map: dict[str, dict] = {}
    accounts_map: dict[str, dict] = {}
    articles_map: dict[str, dict] = {}
    snapshots_out: list[dict] = []
    snapshot_terms_out: list[dict] = []
    ranking_hits_out: list[dict] = []
    metric_obs_out: list[dict] = []

    recorded_at = now_iso()

    # ── 主键字典：按 (content_id, creator_id) 索引，跨 envelope 合并 ──
    article_index: dict[str, dict] = {}  # article_id → article
    article_to_creator: dict[str, str] = {}  # article_id → account_id

    for env in envelopes:
        captured_at = _normalize_captured_at(env.captured_at)
        captured_iso = to_iso(captured_at)
        kid = kw_id(env.keyword)
        source_name, observation_source = _provider_sources(env.source_version)

        # keyword
        kentry = keywords_map.setdefault(kid, {
            "keyword_id": kid,
            "keyword_text": env.keyword,
            "is_active": True,
            "notes": None,
            "first_seen_at": captured_iso,
            "last_seen_at": captured_iso,
            "snapshot_count": 0,
            "platform": env.platform,
            "source_provider": source_name,
        })
        if captured_at < parse_captured_at_iso(kentry["first_seen_at"]):
            kentry["first_seen_at"] = captured_iso
        if captured_at > parse_captured_at_iso(kentry["last_seen_at"]):
            kentry["last_seen_at"] = captured_iso
        kentry["snapshot_count"] += 1

        sid = f"snap_{kid}_{int(captured_at.timestamp() * 1000)}"
        snapshots_out.append({
            "snapshot_id": sid,
            "keyword_id": kid,
            "captured_at": captured_iso,
            "snapshot_date": captured_at.date().isoformat(),
            "snapshot_time": captured_at.strftime("%H:%M"),
            "timezone": str(TZ),
            "trigger_type": "initial" if "initial" in env.source_version else "manual",
            "is_primary": False,  # 重建阶段再决定
            "status": env.status,
            "result_count": env.result_count,
            "result_limit": None,
            "source_name": source_name,
            "source_version": env.source_version,
            "raw_file_path": env.raw_file_path,
            "error_message": env.error_message,
            "recorded_at": recorded_at,
        })

        # snapshot_terms（XHS 通常不返回下拉/相关词；保留字段以便将来扩展）
        for i, t in enumerate(env.suggestions, 1):
            snapshot_terms_out.append({
                "term_id": f"term_{sid}_suggestion_{i}",
                "snapshot_id": sid,
                "term_type": "suggestion",
                "position": i,
                "term_text": t,
            })
        for i, t in enumerate(env.related_terms, 1):
            snapshot_terms_out.append({
                "term_id": f"term_{sid}_related_{i}",
                "snapshot_id": sid,
                "term_type": "related",
                "position": i,
                "term_text": t,
            })

        # articles + accounts + ranking_hits
        for item in env.items:
            aid_account = item.creator_id or item.creator_name or ""
            item_payload = dict(item.platform_payload or {})
            aentry = accounts_map.setdefault(aid_account, {
                "account_id": aid_account,
                "canonical_name": item.creator_name or aid_account,
                "platform": env.platform,
                "first_seen_at": captured_iso,
                "last_seen_at": captured_iso,
                "is_focus": False,
                "notes": None,
                "headimg_url": item_payload.get("creator_avatar"),
                "description": None,
                "fans": None,
                "total_works": None,
                "likes": None,
                "collects": None,
                "follows": None,
                "ip_location": None,
                "verify_info": None,
                "last_create_time": None,
                "source_provider": source_name,
                "platform_payload": {
                    "source_provider": source_name,
                    "red_id": item_payload.get("creator_red_id"),
                    "red_official_verified": item_payload.get("creator_verified"),
                    "red_official_verify_type": item_payload.get("creator_verify_type"),
                },
            })
            if captured_at < parse_captured_at_iso(aentry["first_seen_at"]):
                aentry["first_seen_at"] = captured_iso
            if captured_at > parse_captured_at_iso(aentry["last_seen_at"]):
                aentry["last_seen_at"] = captured_iso
            if not aentry.get("headimg_url") and item_payload.get("creator_avatar"):
                aentry["headimg_url"] = item_payload["creator_avatar"]
            if item.creator_name and not aentry.get("canonical_name"):
                aentry["canonical_name"] = item.creator_name
            account_payload = aentry.setdefault("platform_payload", {})
            for key, value in {
                "red_id": item_payload.get("creator_red_id"),
                "red_official_verified": item_payload.get("creator_verified"),
                "red_official_verify_type": item_payload.get("creator_verify_type"),
            }.items():
                if value is not None and account_payload.get(key) in (None, ""):
                    account_payload[key] = value

            artid = item.content_id
            pub_dt = parse_published_at(item.published_at or "")
            arentry = article_index.get(artid)
            is_relevant, relevance_score = _compute_relevance(env.keyword, item.title, item.summary)
            if not arentry:
                arentry = {
                    "article_id": artid,
                    "platform": env.platform,
                    "normalized_url": item.url,
                    "raw_url": item.url,
                    "title": item.title,
                    "account_id": aid_account,
                    "published_at": to_iso(pub_dt) if pub_dt else None,
                    "summary": item.summary,
                    "work_type": item.work_type,
                    "cover_url": item.cover_url,
                    "first_seen_at": captured_iso,
                    "last_seen_at": captured_iso,
                    "content_status": "available" if item.summary else "missing",
                    "content_file_path": None,
                    "liked_count": item.liked_count,
                    "collected_count": item.collected_count,
                    "comment_count": item.comment_count,
                    "shared_count": item.shared_count,
                    "read_count": item.read_count,
                    "is_relevant": is_relevant,
                    "relevance_score": relevance_score,
                    "source_provider": source_name,
                    "platform_payload": {
                        **dict(item.platform_payload or {}),
                        "source_provider": source_name,
                    },
                }
                article_index[artid] = arentry
                articles_map[artid] = arentry
            else:
                arentry["title"] = item.title or arentry["title"]
                arentry["summary"] = item.summary or arentry["summary"]
                if not arentry["published_at"] and pub_dt:
                    arentry["published_at"] = to_iso(pub_dt)
                if not arentry["cover_url"] and item.cover_url:
                    arentry["cover_url"] = item.cover_url
                # 互动指标用更近的快照覆盖
                if item.liked_count is not None:
                    arentry["liked_count"] = item.liked_count
                if item.collected_count is not None:
                    arentry["collected_count"] = item.collected_count
                if item.comment_count is not None:
                    arentry["comment_count"] = item.comment_count
                if item.shared_count is not None:
                    arentry["shared_count"] = item.shared_count
                if item.read_count is not None:
                    arentry["read_count"] = item.read_count
                if captured_at < parse_captured_at_iso(arentry["first_seen_at"]):
                    arentry["first_seen_at"] = captured_iso
                if captured_at < parse_captured_at_iso(arentry["last_seen_at"]):
                    pass  # intentionally keep max
                if captured_at > parse_captured_at_iso(arentry["last_seen_at"]):
                    arentry["last_seen_at"] = captured_iso

            article_to_creator[artid] = aid_account

            ranking_hits_out.append({
                "hit_id": f"hit_{sid}_{item.rank}",
                "snapshot_id": sid,
                "rank": item.rank,
                "article_id": artid,
                "account_id": aid_account,
                "title_raw": item.title,
                "summary_raw": item.summary,
                "account_name_raw": item.creator_name,
                "published_at_raw": item.published_at,
                "url_raw": item.url,
                "source": observation_source,
                "created_at": recorded_at,
            })

            # 互动观测：每个 (article, snapshot) 至少一条
            metric_obs_out.append({
                "observation_id": f"obs_{sid}_{artid}",
                "article_id": artid,
                "snapshot_id": sid,
                "captured_at": captured_iso,
                "liked_count": item.liked_count,
                "collected_count": item.collected_count,
                "comment_count": item.comment_count,
                "shared_count": item.shared_count,
                "read_count": item.read_count,
                "source": observation_source,
                "recorded_at": recorded_at,
            })

    # ── 标记每个 (keyword, snapshot_date) 的主快照 ──
    primary_set: set[str] = set()
    by_kw_day: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for snap in snapshots_out:
        by_kw_day[(snap["keyword_id"], snap["snapshot_date"])].append(snap)
    for (_, _day), group in by_kw_day.items():
        primary = min(group, key=lambda s: parse_captured_at_iso(s["captured_at"]))
        primary_set.add(primary["snapshot_id"])
    for snap in snapshots_out:
        snap["is_primary"] = snap["snapshot_id"] in primary_set

    return {
        "keywords": list(keywords_map.values()),
        "snapshots": snapshots_out,
        "snapshot_terms": snapshot_terms_out,
        "accounts": list(accounts_map.values()),
        "articles": list(articles_map.values()),
        "ranking_hits": ranking_hits_out,
        "note_metric_observations": metric_obs_out,
    }
