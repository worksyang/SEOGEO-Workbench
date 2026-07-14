"""统一内容工作台 · 域层 / 指标字典与累计算子。

指标字典是 v3.2 第 7 张核心表的内存镜像：
- 预置跨 7 套系统的核心 metric_key；
- ``accumulation_mode`` 用于报表区分瞬时 / 计数 / 状态；
- 单元测试可注入自定义 metric，但 ingest 阶段必须保证 key 已注册。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# 累计算子：v3.2 §六.7
ACCUMULATION_GAUGE: Final = "gauge"
ACCUMULATION_COUNTER: Final = "counter"
ACCUMULATION_TEXT: Final = "text"


@dataclass(slots=True, frozen=True)
class MetricDefinition:
    metric_key: str
    platform: str
    subject_type: str
    display_name: str
    value_type: str = "number"
    unit: str | None = None
    accumulation_mode: str = ACCUMULATION_GAUGE
    description: str | None = None


# 7 套系统的核心 metric_key。来源参考 dev-plan §5 与系统数据字段全景。
BUILTIN_METRICS: Final[tuple[MetricDefinition, ...]] = (
    # 微信搜一搜
    MetricDefinition("wechat.article.read_count", "wechat-search", "content", "微信阅读", "number", "次", ACCUMULATION_COUNTER, "微信公众号累计阅读数"),
    MetricDefinition("wechat.article.like_count", "wechat-search", "content", "微信点赞", "number", "次", ACCUMULATION_COUNTER, "微信点赞（好看）"),
    MetricDefinition("wechat.article.friends_follow_count", "wechat-search", "content", "朋友在看", "number", "次", ACCUMULATION_COUNTER, "微信朋友在看数"),
    MetricDefinition("wechat.keyword.position", "wechat-search", "content", "微信排名", "number", "位", ACCUMULATION_GAUGE, "微信搜索结果排名"),
    MetricDefinition("wechat.keyword.delta_read", "wechat-search", "keyword", "微信阅读增量", "number", "次", ACCUMULATION_GAUGE, "微信关键词窗口阅读增量"),
    # 公众号
    MetricDefinition("mp.account.followers_count", "wechat-mp", "creator", "粉丝数", "number", "人", ACCUMULATION_GAUGE, "微信公众号粉丝"),
    # 小红书
    MetricDefinition("xhs.note.liked_count", "xhs-search", "content", "小红书点赞", "number", "次", ACCUMULATION_COUNTER, "小红书笔记点赞"),
    MetricDefinition("xhs.note.collected_count", "xhs-search", "content", "小红书收藏", "number", "次", ACCUMULATION_COUNTER, "小红书笔记收藏"),
    MetricDefinition("xhs.note.comment_count", "xhs-search", "content", "小红书评论", "number", "次", ACCUMULATION_COUNTER, "小红书笔记评论"),
    MetricDefinition("xhs.note.shared_count", "xhs-search", "content", "小红书分享", "number", "次", ACCUMULATION_COUNTER, "小红书笔记分享"),
    MetricDefinition("xhs.creator.followers_count", "xhs-search", "creator", "小红书粉丝", "number", "人", ACCUMULATION_GAUGE, "小红书博主粉丝"),
    MetricDefinition("xhs.note.position", "xhs-search", "content", "小红书排名", "number", "位", ACCUMULATION_GAUGE, "小红书搜索结果排名"),
    # GEO
    MetricDefinition("geo.source.read_count", "geo", "content", "GEO 来源阅读", "number", "次", ACCUMULATION_GAUGE, "GEO 引用源阅读快照"),
    MetricDefinition("geo.source.like_count", "geo", "content", "GEO 来源点赞", "number", "次", ACCUMULATION_GAUGE, "GEO 引用源点赞"),
    MetricDefinition("geo.source.comment_count", "geo", "content", "GEO 来源评论", "number", "次", ACCUMULATION_GAUGE, "GEO 引用源评论"),
    MetricDefinition("geo.source.favorite_count", "geo", "content", "GEO 来源收藏", "number", "次", ACCUMULATION_GAUGE, "GEO 引用源收藏"),
    MetricDefinition("geo.source.share_count", "geo", "content", "GEO 来源分享", "number", "次", ACCUMULATION_GAUGE, "GEO 引用源分享"),
    MetricDefinition("geo.answer.position", "geo", "relation", "GEO 引用位次", "number", "位", ACCUMULATION_GAUGE, "GEO 回答中的引用位次"),
    # Wiki / Writing / Publish
    MetricDefinition("wiki.edit.characters", "wiki", "content", "Wiki 字数", "number", "字", ACCUMULATION_GAUGE, "Wiki 母文章或正文字数"),
    MetricDefinition("writing.fake.latency_ms", "writing", "job", "假 Provider 延迟", "number", "ms", ACCUMULATION_GAUGE, "Fake Provider 生成耗时"),
    MetricDefinition("publish.attempt.duration", "publish", "attempt", "发布尝试耗时", "number", "s", ACCUMULATION_GAUGE, "发布尝试总耗时"),
)


def metric_index() -> dict[str, MetricDefinition]:
    return {item.metric_key: item for item in BUILTIN_METRICS}


def definition_by_key(metric_key: str) -> MetricDefinition | None:
    return metric_index().get(metric_key)


def register_metric(definition: MetricDefinition, registry: dict[str, MetricDefinition] | None = None) -> dict[str, MetricDefinition]:
    """测试 / 适配器注入自定义 metric。"""
    if definition.accumulation_mode not in {ACCUMULATION_GAUGE, ACCUMULATION_COUNTER, ACCUMULATION_TEXT}:
        raise ValueError(f"未知 accumulation_mode：{definition.accumulation_mode}")
    base = registry if registry is not None else dict(metric_index())
    base[definition.metric_key] = definition
    return base


# 常用取值与展示格式辅助函数。
def fmt_gauge(metric_key: str, value: float | int | None) -> str:
    definition = definition_by_key(metric_key)
    if value is None:
        return "—"
    if definition and definition.unit == "ms" and isinstance(value, (int, float)):
        return f"{int(value)} ms"
    if definition and definition.unit and isinstance(value, (int, float)):
        return f"{int(value) if float(value).is_integer() else value:.2f} {definition.unit}"
    if isinstance(value, (int, float)):
        return f"{value:,}"
    return str(value)


def is_state_metric(definition: MetricDefinition) -> bool:
    """状态型 metric（文本 / enum），不适合直接求差。"""
    return definition.accumulation_mode == ACCUMULATION_TEXT


def is_counter_metric(definition: MetricDefinition) -> bool:
    return definition.accumulation_mode == ACCUMULATION_COUNTER
