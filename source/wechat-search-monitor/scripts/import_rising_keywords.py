#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从微信关联词中筛选上涨候选词，并写入关键词注册表。

统计口径默认固定为 2026-07-05—07 与 2026-07-08—10，便于复现本轮
扩词；后续可传入日期参数重跑。下拉词只保留为观察数据，不单独作为 active
关键词候选，因为它可能是系统补全。脚本先生成候选 JSON，只有 --apply 才写库。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.repositories.keyword_registry_repo import KeywordRegistryRepository


GROUP_LONG_TERM = "长期需求增长词"
GROUP_PRODUCT_ACTION = "产品动作增长词"
GROUP_COMPLIANCE = "合规事件观察词"

# 人工给出的方向性假设。它们不是下拉词/关联词的事实记录，默认不应直接进入
# active 监控词库；只有显式传入 --include-predicted-seeds 才会作为观察种子导入。
PREDICTED_SEED_CANDIDATES: list[tuple[str, str]] = [
    # 长期需求底盘
    (GROUP_LONG_TERM, "香港目前最好的储蓄险"),
    (GROUP_LONG_TERM, "香港保险100万"),
    (GROUP_LONG_TERM, "什么人适合买港险"),
    (GROUP_LONG_TERM, "香港保险的十大优势"),
    (GROUP_LONG_TERM, "香港保险2年交对比"),
    (GROUP_LONG_TERM, "香港人民币保单推荐"),
    (GROUP_LONG_TERM, "香港多元货币保单"),
    (GROUP_LONG_TERM, "香港保险财富传承"),
    (GROUP_LONG_TERM, "香港设立家族信托门槛"),
    (GROUP_LONG_TERM, "香港家族信托哪家好"),
    (GROUP_LONG_TERM, "香港保单钱怎么回来"),
    (GROUP_LONG_TERM, "香港保险演示利率"),
    (GROUP_LONG_TERM, "杠杆寿和增额寿对比图"),
    (GROUP_LONG_TERM, "港险5年交产品对比"),
    (GROUP_LONG_TERM, "香港保险十大功能"),
    (GROUP_LONG_TERM, "香港保险100问"),
    (GROUP_LONG_TERM, "香港保险信托功能"),
    (GROUP_LONG_TERM, "香港保险类信托功能"),
    (GROUP_LONG_TERM, "香港保险金信托案例"),
    (GROUP_LONG_TERM, "香港保险传承优势"),
    (GROUP_LONG_TERM, "香港保险定向传承"),
    (GROUP_LONG_TERM, "香港保险受益人"),
    (GROUP_LONG_TERM, "香港保险年金"),
    (GROUP_LONG_TERM, "香港美式分红储蓄险"),
    (GROUP_LONG_TERM, "香港保险的底层逻辑"),
    (GROUP_LONG_TERM, "保证回本最快的港险"),
    (GROUP_LONG_TERM, "2年期缴费的香港保险"),
    (GROUP_LONG_TERM, "香港保险优惠表"),
    (GROUP_LONG_TERM, "香港分红险实现率排名"),
    (GROUP_LONG_TERM, "港险的汇率风险"),
    (GROUP_LONG_TERM, "汇率波动对香港保单"),
    (GROUP_LONG_TERM, "海外港险"),
    # 合规 / 事件观察
    (GROUP_COMPLIANCE, "港险分红实现率"),
    (GROUP_COMPLIANCE, "港险历年分红实现率"),
    (GROUP_COMPLIANCE, "香港保险分红实现率图"),
    (GROUP_COMPLIANCE, "香港保险返佣保单失效"),
    (GROUP_COMPLIANCE, "香港返佣作废保单"),
    (GROUP_COMPLIANCE, "香港保险返佣入刑"),
    (GROUP_COMPLIANCE, "大陆人买港险合规吗"),
    (GROUP_COMPLIANCE, "香港保险会被CRS交换吗"),
    (GROUP_COMPLIANCE, "香港保诚保费融资"),
    (GROUP_COMPLIANCE, "中银薪火传承保单贷款"),
    (GROUP_COMPLIANCE, "港险GN16"),
    (GROUP_COMPLIANCE, "GN16横评"),
    (GROUP_COMPLIANCE, "保监局保费融资"),
    (GROUP_COMPLIANCE, "宏利高杠杆保单贷款"),
    (GROUP_COMPLIANCE, "南向通港险"),
    (GROUP_COMPLIANCE, "自购返佣"),
    (GROUP_COMPLIANCE, "香港保险返佣能返多少"),
    (GROUP_COMPLIANCE, "香港保险返佣处罚案例"),
    (GROUP_COMPLIANCE, "保费融资贷款"),
    (GROUP_COMPLIANCE, "港险重疾新规"),
    # 产品动作
    (GROUP_PRODUCT_ACTION, "友邦财富盈活门槛"),
    (GROUP_PRODUCT_ACTION, "AIA环宇盈活是什么产品"),
    (GROUP_PRODUCT_ACTION, "友邦环宇盈活提取比例"),
    (GROUP_PRODUCT_ACTION, "友邦环宇盈活优惠"),
    (GROUP_PRODUCT_ACTION, "友邦环宇盈活和保诚信守明天对比"),
    (GROUP_PRODUCT_ACTION, "安盛盛利2最新优惠"),
    (GROUP_PRODUCT_ACTION, "安盛盛利2提领密码"),
    (GROUP_PRODUCT_ACTION, "安盛盛利2保费折扣"),
    (GROUP_PRODUCT_ACTION, "保诚信守明天的缺点"),
    (GROUP_PRODUCT_ACTION, "保诚信守明天信托"),
    (GROUP_PRODUCT_ACTION, "保诚信守明天回本期"),
    (GROUP_PRODUCT_ACTION, "周大福匠心传承储蓄"),
    (GROUP_PRODUCT_ACTION, "周大福匠心传承优惠"),
    (GROUP_PRODUCT_ACTION, "万通富饶万家测评"),
    (GROUP_PRODUCT_ACTION, "万通富饶万家投保规则"),
    (GROUP_PRODUCT_ACTION, "香港友邦杠杆寿险"),
    (GROUP_PRODUCT_ACTION, "友邦环宇盈活趸交"),
    (GROUP_PRODUCT_ACTION, "友邦环宇盈活计划书"),
    (GROUP_PRODUCT_ACTION, "友邦环宇盈活红利锁定"),
    (GROUP_PRODUCT_ACTION, "友邦环宇盈活产品介绍"),
    (GROUP_PRODUCT_ACTION, "安盛盛利2起投门槛"),
    (GROUP_PRODUCT_ACTION, "安盛盛利2人民币保单"),
    (GROUP_PRODUCT_ACTION, "安盛盛利2保费回赠"),
    (GROUP_PRODUCT_ACTION, "信守明天人民币保单"),
    (GROUP_PRODUCT_ACTION, "信守明天多元货币"),
    (GROUP_PRODUCT_ACTION, "信守明天产品亮点"),
    (GROUP_PRODUCT_ACTION, "信守明天产品介绍"),
    (GROUP_PRODUCT_ACTION, "周大福匠心飞越优惠"),
    (GROUP_PRODUCT_ACTION, "周大福匠心飞越投保规则"),
    (GROUP_PRODUCT_ACTION, "万通富饶万家年金"),
    (GROUP_PRODUCT_ACTION, "万通富饶万家人民币"),
    (GROUP_PRODUCT_ACTION, "富饶万家储蓄保险计划"),
    (GROUP_PRODUCT_ACTION, "盈耀万用寿险计划"),
    (GROUP_PRODUCT_ACTION, "万通鼎峰万用寿险"),
    (GROUP_PRODUCT_ACTION, "永明星河尊享2提取密码"),
    (GROUP_PRODUCT_ACTION, "星河尊享2提取密码"),
    (GROUP_PRODUCT_ACTION, "富卫盈聚天下2年交"),
    (GROUP_PRODUCT_ACTION, "国寿傲珑盛世优惠"),
    (GROUP_PRODUCT_ACTION, "国寿傲珑盛世人民币"),
    (GROUP_PRODUCT_ACTION, "太保鑫安逸又上线了吗"),
    (GROUP_PRODUCT_ACTION, "中银月悦出息"),
    (GROUP_PRODUCT_ACTION, "永明享悦即享年金"),
    (GROUP_PRODUCT_ACTION, "友邦卓达智悦2"),
    (GROUP_PRODUCT_ACTION, "友邦活然人生保险计划"),
    (GROUP_PRODUCT_ACTION, "友邦爱无忧3"),
    (GROUP_PRODUCT_ACTION, "保诚致胜财富"),
    (GROUP_PRODUCT_ACTION, "保诚世誉财富产品"),
    (GROUP_PRODUCT_ACTION, "保诚保单贷款"),
    (GROUP_PRODUCT_ACTION, "保费融资产品对比"),
]

NAVIGATION_OR_NOISE = re.compile(
    r"(公众号|官网|客服电话|客服|地址|招聘|app下载|登录|小程序|"
    r"app|客户网站|官网|官微|保险公司$|保险公司介绍|保险公司排名|"
    r"十大保险经纪|星河战队|荣誉2期|龙舟赛|知乎|百度|小红书|视频|"
    r"港险详析|港险干货|我的中银|za bank|汇丰银行缴付|"
    r"sunny港险笔记|健哥鉴港险|金斧子港险|港险保险圈|"
    r"保险金钻|保险金饭碗|香港苏黎世拍卖|安盛天平保险|香港忠意人寿|"
    r"香港保诚保险$|香港友邦保险$|富卫保险$|香港万通介绍$|"
    r"安盛保险我的保单|香港永明人寿介绍|香港永明金融$)",
    re.IGNORECASE,
)
EXPIRING_MONTH_TERM = re.compile(r"(20\d{2}年香港保险数据|202[0-5]年|2025|6月|六月)")
DOMAIN_SIGNAL = re.compile(
    r"(香港保险|港险|香港保单|储蓄险|分红险|家族信托|保费融资|杠杆寿|"
    r"年金|终身寿|万用寿险|友邦|安盛|保诚|宏利|富卫|永明|万通|"
    r"周大福|国寿|太保|中银|忠意|苏黎世|匠心|盛利|环宇|财富盈活|"
    r"信守明天|星河|富饶|薪火传承|CRS|GN16)",
    re.IGNORECASE,
)
COMPLIANCE_SIGNAL = re.compile(
    r"(返佣|合规|CRS|GN16|监管|保监|自购|作废|入刑|处罚|保费融资|贷款|重疾新规)",
    re.IGNORECASE,
)
PRODUCT_SIGNAL = re.compile(
    r"(友邦|安盛|保诚|宏利|富卫|永明|万通|周大福|国寿|太保|中银|"
    r"盛利|环宇|财富盈活|信守明天|星河|富饶|薪火|匠心|傲珑|盈耀|"
    r"提领|优惠|折扣|门槛|回本|测评|投保|对比|计划书|产品|红利|"
    r"人民币|货币|保单贷款|年金)",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="筛选并导入上涨关键词")
    parser.add_argument("--base-start", default="2026-07-05")
    parser.add_argument("--base-end", default="2026-07-07")
    parser.add_argument("--recent-start", default="2026-07-08")
    parser.add_argument("--recent-end", default="2026-07-10")
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument(
        "--include-predicted-seeds",
        action="store_true",
        help="将人工预测种子一并导入（默认关闭；默认只导入下拉/关联词中的精确词）",
    )
    parser.add_argument("--apply", action="store_true", help="确认写入 SQLite 关键词注册表")
    parser.add_argument(
        "--output",
        default=str(ROOT / "data/keyword_lists/2026-07-11_上涨候选词150.json"),
    )
    return parser.parse_args()


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def classify(text: str) -> str:
    if COMPLIANCE_SIGNAL.search(text):
        return GROUP_COMPLIANCE
    if PRODUCT_SIGNAL.search(text):
        return GROUP_PRODUCT_ACTION
    return GROUP_LONG_TERM


def _load_term_stats(
    *,
    base_start: str,
    base_end: str,
    recent_start: str,
    recent_end: str,
) -> dict[str, dict[str, Any]]:
    snapshots = json.loads((ROOT / "normalized/snapshots.json").read_text(encoding="utf-8"))
    terms = json.loads((ROOT / "normalized/snapshot_terms.json").read_text(encoding="utf-8"))
    snapshot_map = {
        str(item.get("snapshot_id") or ""): item
        for item in snapshots
        if item.get("snapshot_id")
        and item.get("is_primary")
        and str(item.get("status") or "success") == "success"
    }
    base_count: Counter[str] = Counter()
    recent_count: Counter[str] = Counter()
    recent_parents: dict[str, set[str]] = defaultdict(set)
    recent_related_dates: dict[str, set[str]] = defaultdict(set)
    recent_suggestion_count: Counter[str] = Counter()
    for item in terms:
        snapshot = snapshot_map.get(str(item.get("snapshot_id") or ""))
        if not snapshot:
            continue
        text = normalize(item.get("term_text") or "")
        if not text:
            continue
        date = str(snapshot.get("snapshot_date") or "")
        term_type = str(item.get("term_type") or "")
        if base_start <= date <= base_end and term_type == "related":
            base_count[text] += 1
        if recent_start <= date <= recent_end and term_type == "related":
            recent_count[text] += 1
            recent_parents[text].add(str(snapshot.get("keyword_id") or ""))
            recent_related_dates[text].add(date)
        elif recent_start <= date <= recent_end and term_type == "suggestion":
            recent_suggestion_count[text] += 1

    result: dict[str, dict[str, Any]] = {}
    for text, recent in recent_count.items():
        base = base_count[text]
        increase = recent - base
        parent_count = len(recent_parents[text] - {""})
        suggestion_count = int(recent_suggestion_count[text])
        related_date_count = len(recent_related_dates[text])
        score = increase * 3 + recent + parent_count * 2
        result[text] = {
            "base_count": base,
            "recent_count": recent,
            "increase": increase,
            "parent_keyword_count": parent_count,
            "suggestion_count": suggestion_count,
            "related_count": int(recent),
            "related_date_count": related_date_count,
            "score": round(score, 2),
        }
    return result


def _is_rising_candidate(text: str, stat: dict[str, Any]) -> bool:
    if (
        len(text) < 4
        or len(text) > 36
        or NAVIGATION_OR_NOISE.search(text)
        or EXPIRING_MONTH_TERM.search(text)
        or not DOMAIN_SIGNAL.search(text)
    ):
        return False
    base = int(stat["base_count"])
    recent = int(stat["recent_count"])
    increase = int(stat["increase"])
    related_date_count = int(stat.get("related_date_count") or 0)
    if recent < 3 or related_date_count < 2:
        return False
    return (
        (recent >= 8 and increase >= 3)
        or (recent >= 5 and base == 0)
        or (recent >= 12 and increase > 0)
    )


def build_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    repo = KeywordRegistryRepository(ROOT / "data/state/app.db")
    existing = {
        normalize(item["keyword_text"])
        # 人工归档也是控制层决策；自动扩词不能把已归档词重新加回 active。
        for item in repo.list_keywords(include_archived=True)
    }
    stats = _load_term_stats(
        base_start=args.base_start,
        base_end=args.base_end,
        recent_start=args.recent_start,
        recent_end=args.recent_end,
    )
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set(existing)

    def add(text: str, group_label: str, source: str) -> None:
        normalized = normalize(text)
        if not normalized or normalized in seen or len(candidates) >= args.limit:
            return
        seen.add(normalized)
        stat = stats.get(normalized, {})
        candidates.append({
            "keyword_text": normalized,
            "group_label": group_label,
            "source": source,
            "base_count": int(stat.get("base_count") or 0),
            "recent_count": int(stat.get("recent_count") or 0),
            "increase": int(stat.get("increase") or 0),
            "parent_keyword_count": int(stat.get("parent_keyword_count") or 0),
            "suggestion_count": int(stat.get("suggestion_count") or 0),
            "related_count": int(stat.get("related_count") or 0),
            "related_date_count": int(stat.get("related_date_count") or 0),
            "score": float(stat.get("score") or 0),
            "refresh_frequency_days": 1,
            "refresh_frequency_source": "auto",
        })

    if args.include_predicted_seeds:
        for group_label, text in PREDICTED_SEED_CANDIDATES:
            add(text, group_label, "predicted_seed")

    ranked = sorted(
        (
            (text, stat)
            for text, stat in stats.items()
            if _is_rising_candidate(text, stat)
        ),
        key=lambda item: (
            -float(item[1]["score"]),
            -int(item[1]["recent_count"]),
            item[0],
        ),
    )
    # 先按长期 / 产品 / 合规分组轮询，避免单一产品的词把词库挤满。
    for group_label in (GROUP_LONG_TERM, GROUP_PRODUCT_ACTION, GROUP_COMPLIANCE):
        for text, _ in ranked:
            if classify(text) == group_label:
                add(text, group_label, "term_rise")
    for text, _ in ranked:
        add(text, classify(text), "term_rise")

    return candidates


def ensure_groups(repo: KeywordRegistryRepository) -> dict[str, str]:
    payload = repo.load_payload()
    groups = {
        str(group["label"]): str(group["group_id"])
        for group in payload.get("groups", [])
    }
    for label in (GROUP_LONG_TERM, GROUP_PRODUCT_ACTION, GROUP_COMPLIANCE):
        if label not in groups:
            groups[label] = str(repo.create_group(label)["group_id"])
    return groups


def import_candidates(candidates: list[dict[str, Any]]) -> dict[str, int]:
    repo = KeywordRegistryRepository(ROOT / "data/state/app.db")
    group_ids = ensure_groups(repo)
    created = 0
    reactivated_or_existing = 0
    for item in candidates:
        text = str(item["keyword_text"])
        prior = next(
            (
                row
                for row in repo.list_keywords(include_archived=True)
                if normalize(row["keyword_text"]) == text
            ),
            None,
        )
        note = (
            "2026-07-11 上涨候选；"
            f"07-05~07 {item['base_count']} → 07-08~10 {item['recent_count']}，"
            f"增量 {item['increase']}，关联词日期 {item.get('related_date_count', 0)}，"
            f"父词 {item['parent_keyword_count']}；"
            f"来源：{'人工预测种子' if item['source'] == 'predicted_seed' else '关联词上涨'}"
        )
        repo.create_keyword(
            group_ids[str(item["group_label"])],
            text,
            note=note,
            source="trend_candidate",
        )
        if prior and prior.get("status") == "active":
            reactivated_or_existing += 1
        else:
            created += 1
    return {
        "created_or_reactivated": created,
        "already_active": reactivated_or_existing,
        "total": len(candidates),
    }


def main() -> int:
    args = parse_args()
    if args.limit < 1 or args.limit > 200:
        raise ValueError("--limit must be between 1 and 200")
    candidates = build_candidates(args)
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": "2026-07-11",
        "comparison": {
            "base": f"{args.base_start} ~ {args.base_end}",
            "recent": f"{args.recent_start} ~ {args.recent_end}",
        },
        "selection_rule": (
            "默认只导入关联词中的精确词，要求在近期满足"
            "关联词至少出现3次且跨至少2日，并满足近期>=8且增量>=3，"
            "或近期>=5且基期=0，或近期>=12且增长；"
            "下拉词不单独入选；"
            "人工预测种子仅在 --include-predicted-seeds 时加入。"
        ),
        "candidates": candidates,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成 {len(candidates)} 个候选词：{output_path}")
    for item in candidates:
        print(
            f"[{item['group_label']}] {item['keyword_text']} "
            f"{item['base_count']}→{item['recent_count']} (+{item['increase']}) "
            f"{item['source']}"
        )
    if args.apply:
        result = import_candidates(candidates)
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
