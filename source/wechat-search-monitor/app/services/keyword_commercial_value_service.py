"""关键词商业价值控制层评分。

评分只用于刷新与归档控制，不属于搜索事实。两个透明轴各 1～5 分：
1. transaction_proximity：距离投保/成交动作有多近；
2. wealth_intensity：词面上对应的潜在资产或客单规模。

自动分允许人工覆盖；代码不得把该分数表述为真实成交率或用户净值。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.config import Config
from app.repositories.keyword_registry_repo import KeywordRegistryRepository


DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data"
    / "config"
    / "keyword_commercial_value_policy.json"
)


def _normalize(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _load_policy(policy_path: Path | None = None) -> dict[str, Any]:
    path = Path(policy_path or DEFAULT_POLICY_PATH)
    return json.loads(path.read_text(encoding="utf-8"))


def _matched_signals(text: str, signals: list[Any]) -> list[str]:
    return [
        str(signal)
        for signal in signals
        if _normalize(signal) and _normalize(signal) in text
    ]


def _axis_score(
    text: str,
    config: dict[str, Any],
) -> tuple[int, list[str]]:
    default = max(1, min(5, int(config.get("default") or 1)))
    for score in (5, 4, 3, 2, 1):
        matches = _matched_signals(text, list(config.get(str(score)) or []))
        if matches:
            return score, matches
    return default, []


def score_keyword(
    keyword_text: str,
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回 1～10 分及可解释的两轴明细。"""
    policy = policy or _load_policy()
    text = _normalize(keyword_text)
    if not text:
        return {
            "commercial_value_score": 1,
            "transaction_proximity": 1,
            "wealth_intensity": 1,
            "commercial_value_reason": "空关键词：最低分",
            "reason_codes": ["empty_keyword"],
        }

    hard_low = _matched_signals(text, list(policy.get("hard_low_value_signals") or []))
    expired = _matched_signals(text, list(policy.get("expired_time_signals") or []))
    if hard_low or expired:
        reasons = []
        if hard_low:
            reasons.append(f"导航/泛标签：{'、'.join(hard_low[:3])}")
        if expired:
            reasons.append(f"过期时间词：{'、'.join(expired[:3])}")
        return {
            "commercial_value_score": 1,
            "transaction_proximity": 1,
            "wealth_intensity": 1,
            "commercial_value_reason": "；".join(reasons),
            "reason_codes": [
                *(["hard_low_value"] if hard_low else []),
                *(["expired_time_term"] if expired else []),
            ],
        }

    transaction, transaction_matches = _axis_score(
        text,
        dict(policy.get("transaction_axis") or {}),
    )
    wealth, wealth_matches = _axis_score(
        text,
        dict(policy.get("wealth_axis") or {}),
    )

    education_matches = _matched_signals(
        text,
        list(policy.get("early_education_signals") or []),
    )
    specific_matches = _matched_signals(
        text,
        list(policy.get("specific_entity_signals") or []),
    )
    # “内地与香港保险有什么区别”虽包含“对比”，仍属于早期教育；
    # 只有明确到产品/保司实体时，才保留较高决策意图。
    if education_matches and not specific_matches:
        transaction = min(transaction, 1)

    score = max(1, min(10, transaction + wealth))
    if score >= 9:
        tier = "高净值且临近成交"
    elif score >= 7:
        tier = "高商业价值"
    elif score >= 5:
        tier = "中等商业价值"
    elif score >= 3:
        tier = "长转化或低客单"
    else:
        tier = "低商业价值"

    transaction_label = {
        1: "认知启蒙",
        2: "泛兴趣",
        3: "场景规划",
        4: "方案决策",
        5: "成交动作",
    }[transaction]
    wealth_label = {
        1: "客单不明确",
        2: "大众保障",
        3: "中产储蓄",
        4: "较高资产",
        5: "高净值架构",
    }[wealth]
    reason = (
        f"成交距离{transaction}/5（{transaction_label}"
        f"{'：' + '、'.join(transaction_matches[:3]) if transaction_matches else ''}）；"
        f"资产强度{wealth}/5（{wealth_label}"
        f"{'：' + '、'.join(wealth_matches[:3]) if wealth_matches else ''}）；"
        f"总分{score}/10，{tier}"
    )
    return {
        "commercial_value_score": score,
        "transaction_proximity": transaction,
        "wealth_intensity": wealth,
        "commercial_value_reason": reason,
        "reason_codes": [
            *(["early_education"] if education_matches and not specific_matches else []),
            *(["specific_entity"] if specific_matches else []),
        ],
    }


def apply_commercial_value_scores(
    *,
    repository: KeywordRegistryRepository | None = None,
    include_archived: bool = False,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    repository = repository or KeywordRegistryRepository(Config.SQLITE_PATH)
    policy = _load_policy(policy_path)
    rows = repository.list_keywords(include_archived=include_archived)
    updates: dict[str, dict[str, Any]] = {}
    distribution = {score: 0 for score in range(1, 11)}
    for row in rows:
        result = score_keyword(row.get("keyword_text") or "", policy=policy)
        distribution[int(result["commercial_value_score"])] += 1
        if row.get("commercial_value_source") != "manual":
            updates[str(row["keyword_id"])] = result
    updated = repository.apply_auto_commercial_values(updates)
    return {
        "updated_count": updated,
        "evaluated_count": len(rows),
        "distribution": distribution,
    }
