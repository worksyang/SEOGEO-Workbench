from __future__ import annotations

import re
import unicodedata


TOPIC_SPECS = [
    {"topic": "财富盈活", "tokens": ["财富盈活", "AIA Wealth Flex", "AIA Wealth", "AIA 财富盈活"]},
    {"topic": "环宇盈活", "tokens": ["环宇盈活"]},
    {"topic": "信守明天", "tokens": ["信守明天"]},
    {"topic": "世誉财富", "tokens": ["世誉财富", "Wealth Prestige"]},
    {"topic": "骏誉财富", "tokens": ["骏誉财富"]},
    {"topic": "盛利2", "tokens": ["盛利 2", "盛利2"]},
    {"topic": "傲珑盛世", "tokens": ["傲珑盛世"]},
    {"topic": "丰饶传承3", "tokens": ["丰饶传承 3", "丰饶传承3"]},
    {"topic": "宏挚传承", "tokens": ["宏挚传承"]},
    {"topic": "Signature Legacy Harvest", "tokens": ["Signature Legacy Harvest"]},
    {"topic": "Singlife Legacy IUL", "tokens": ["Singlife Legacy IUL"]},
    {"topic": "鑫安逸", "tokens": ["鑫安逸"]},
    {"topic": "星河尊享2", "tokens": ["星河尊享 2", "星河尊享2"]},
    {"topic": "盈聚天下2", "tokens": ["盈聚天下 2", "盈聚天下2"]},
    {"topic": "匠心传承2", "tokens": ["匠心传承 2", "匠心传承2"]},
    {"topic": "匠心飞越", "tokens": ["匠心飞越"]},
    {"topic": "充裕未来", "tokens": ["充裕未来"]},
    {"topic": "富饶传家", "tokens": ["富饶传家"]},
    {"topic": "富饶盈家", "tokens": ["富饶盈家"]},
]


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[·•|｜:：,，。！？!?\-—_（）()\[\]【】<>《》“”\"'‘’`×x]+", "", value)
    return value


def infer_topic(keyword_text: str) -> str:
    normalized_keyword = _normalize(keyword_text)
    matched_topics: list[str] = []
    for spec in TOPIC_SPECS:
        if any(_normalize(token) in normalized_keyword for token in spec["tokens"]):
            matched_topics.append(spec["topic"])

    uniq = list(dict.fromkeys(matched_topics))
    if len(uniq) == 1:
        return uniq[0]
    return keyword_text


def resolve_topic(keyword_text: str, explicit_topic: str | None) -> str:
    topic = str(explicit_topic or "").strip()
    if topic:
        return topic
    return infer_topic(keyword_text)
