"""统一内容工作台 · 域层 / 受控词典。

v3.2 文档第 6.1 节描述 contents 表的 entities_json / intents_json 使用受控词典：
- 适配器和编辑器写入时必须校验该词典；
- 词条由 taxonomy.yaml 或本模块常量集中维护；
- 升级为独立表前禁止任意字符串写入。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable


@dataclass(slots=True, frozen=True)
class TaxonomyTerm:
    key: str
    display: str
    description: str = ""


# ── 实体：人 / 机构 / 产品 / 保司 ─────────────────────────────────────

ENTITY_TERMS: Final[tuple[TaxonomyTerm, ...]] = (
    TaxonomyTerm("insurance", "保险", "保险产品或险种类别"),
    TaxonomyTerm("hongkong_insurance", "香港保险", "香港注册或销售的保险产品"),
    TaxonomyTerm("mainland_insurance", "内地保险", "内地注册或销售的保险产品"),
    TaxonomyTerm("singapore_insurance", "新加坡保险", "新加坡注册或销售的保险产品"),
    TaxonomyTerm("savings_insurance", "储蓄险", "储蓄型保险，长期持有"),
    TaxonomyTerm("whole_life", "终身寿险", "终身寿险，提供身故保障与现金价值"),
    TaxonomyTerm("critical_illness", "重疾险", "重疾险"),
    TaxonomyTerm("dividend_realization", "分红实现率", "实际分红/演示分红"),
    TaxonomyTerm("aia", "友邦", "友邦保险（AIA）"),
    TaxonomyTerm("prudential", "保诚", "保诚保险（Prudential）"),
    TaxonomyTerm("axa", "安盛", "安盛保险（AXA）"),
    TaxonomyTerm("manulife", "宏利", "宏利保险（Manulife）"),
    TaxonomyTerm("sunlife", "永明", "永明金融（Sun Life）"),
    TaxonomyTerm("fwd", "富卫", "富卫保险（FWD）"),
    TaxonomyTerm("ctflife", "周大福人寿", "周大福人寿（CTF Life）"),
    TaxonomyTerm("yuan_life", "万通", "万通保险（YF Life）"),
    TaxonomyTerm("china_life", "中国人寿", "中国人寿海外"),
    TaxonomyTerm("hang_seng_insurance", "恒生保险", "恒生保险"),
    TaxonomyTerm("global_currency", "多元货币", "多货币计价保单"),
    TaxonomyTerm("premium_finance", "保费融资", "保费融资"),
    TaxonomyTerm("withdrawal", "提领", "提领"),
    TaxonomyTerm("esg", "ESG", "环境、社会与治理评级"),
    TaxonomyTerm("geo_source", "GEO 来源", "GEO 回答引用的来源实体"),
    TaxonomyTerm("ai_search", "AI 搜索", "豆包、文心等 AI 搜索"),
    TaxonomyTerm("compliance", "合规", "合规与监管"),
    TaxonomyTerm("exchange_rate", "汇率", "汇率与跨境成本"),
    TaxonomyTerm("tax", "税务", "税务与申报"),
)

INTENT_TERMS: Final[tuple[TaxonomyTerm, ...]] = (
    TaxonomyTerm("topic_discovery", "选题发现", "通过关键词挖掘新选题"),
    TaxonomyTerm("rank_tracking", "排名盯号", "追踪关键词排名变化"),
    TaxonomyTerm("read_estimation", "阅读估算", "根据命中估算关键词阅读"),
    TaxonomyTerm("competitor_intel", "同行洞察", "了解同行账号表现"),
    TaxonomyTerm("content_gap", "内容缺口", "发现尚未充分覆盖的关键词"),
    TaxonomyTerm("compliance_review", "合规审查", "敏感词与监管口径检查"),
    TaxonomyTerm("seo_optimization", "SEO 优化", "针对搜索引擎优化"),
    TaxonomyTerm("geo_monitoring", "GEO 监控", "观察 AI 引用源变化"),
    TaxonomyTerm("produce_article", "生成成稿", "从母文章生成可发布稿件"),
    TaxonomyTerm("publish_to_account", "发布到公众号", "将成稿发布至目标公众号"),
    TaxonomyTerm("rework_material", "素材返工", "对素材做返工或归类"),
    TaxonomyTerm("candidate_mining", "探针候选", "从下拉词/相关词中挖掘新词"),
    TaxonomyTerm("coverage_replay", "覆盖回放", "回放历史命中与切片"),
    TaxonomyTerm("snapshot_compare", "快照对比", "两个快照之间的命中差异"),
    TaxonomyTerm("sensitive_check", "敏感词检查", "对 Markdown 做敏感词二次检查"),
)


class TaxonomyValidationError(ValueError):
    """受控词典校验失败。"""


def _build_index(terms: Iterable[TaxonomyTerm]) -> dict[str, TaxonomyTerm]:
    return {term.key: term for term in terms}


_ENTITY_INDEX: Final[dict[str, TaxonomyTerm]] = _build_index(ENTITY_TERMS)
_INTENT_INDEX: Final[dict[str, TaxonomyTerm]] = _build_index(INTENT_TERMS)


def known_entity(key: str) -> TaxonomyTerm | None:
    return _ENTITY_INDEX.get(key)


def known_intent(key: str) -> TaxonomyTerm | None:
    return _INTENT_INDEX.get(key)


def validate_entities(values: Iterable[str]) -> list[str]:
    """校验 entities 字段，未知项抛 TaxonomyValidationError。空数组允许。"""
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise TaxonomyValidationError("entity 项必须为非空字符串")
        if value not in _ENTITY_INDEX:
            raise TaxonomyValidationError(f"未知 entity：{value}")
        cleaned.append(value)
    return cleaned


def validate_intents(values: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise TaxonomyValidationError("intent 项必须为非空字符串")
        if value not in _INTENT_INDEX:
            raise TaxonomyValidationError(f"未知 intent：{value}")
        cleaned.append(value)
    return cleaned


def normalize_entity_list(values: Iterable[str] | None) -> list[str]:
    """去重 + 排序 + 过滤未知项，返回规范形态。"""
    if not values:
        return []
    seen: set[str] = set()
    for value in values:
        if value in _ENTITY_INDEX:
            seen.add(value)
    return sorted(seen)


def normalize_intent_list(values: Iterable[str] | None) -> list[str]:
    if not values:
        return []
    seen: set[str] = set()
    for value in values:
        if value in _INTENT_INDEX:
            seen.add(value)
    return sorted(seen)
