"""monitor_builder.py — 监控派生层编排入口（build_monitor_data）。

将原 850 行 `build_monitor_data()` 拆为以下职责明确的子模块：
- monitor_scoring.py：纯评分函数（rank_weight、连续性、广度等）
- monitor_heat.py：AIDSO 热度加载、幂律拟合、kw_score 计算
- monitor_context.py：ent 索引与窗口查询上下文
- monitor_keywords.py：关键词维度汇总
- monitor_accounts.py：账号维度聚合与汇总

`build_monitor_data()` 行为契约保持不变：输入 ent + keyword_settings，
输出与改造前完全一致的 payload（除 generated_at 时间戳）。

为了兼容历史调用路径（如 `from app.ingest.builders.monitor_builder import
rank_weight`），本文件从 monitor_scoring / monitor_heat 重新导出所有相关
函数，确保外部 import 不需要改。
"""
from __future__ import annotations

from typing import Optional

from app.ingest.builders.monitor_accounts import (
    build_account_summaries,
    build_account_theme_indexes,
)
from app.ingest.builders.monitor_context import build_monitor_context
from app.ingest.builders.monitor_heat import (
    HeatContext,
    build_heat_context,
    wso_fit_meta,
)
from app.ingest.builders.monitor_keywords import build_keyword_summaries
from app.ingest.builders.monitor_scoring import (  # noqa: F401  re-export
    RECENT_WINDOW_DAYS,
    TIMELINESS_WINDOW_DAYS,
    WINDOW_DAYS,
    angle_bonus,
    bucket_breadth_bonus,
    continuity_multiplier,
    display_day_count,
    extra_article_bonus,
    longest_positive_streak,
    rank_weight,
    recency_weight,
    summarize_account_keyword_moves,
    timeliness_rank_weight,
    topic_breadth_bonus,
    trailing_positive_streak,
)
from app.ingest.common import NORMALIZED_DIR, now_iso

# 重新导出热度函数（保持历史 import 路径）
from app.ingest.builders.monitor_heat import (  # noqa: F401  re-export
    _load_aidso_heat_lookup,
    _fit_wso_power_law,
    _estimate_wso,
    _compute_kw_score,
)


def build_monitor_data(ent: dict, keyword_settings: dict[str, dict] | None = None) -> dict:
    """编排入口：ctx → heat → keyword → account → payload。

    步骤：
    1. 构造共享上下文（ent 索引 + 窗口查询）
    2. 加载 AIDSO 热度上下文（WSO/DSO + 幂律拟合）
    3. 生成关键词 summaries
    4. 生成账号主题索引 + 账号 summaries
    5. 装配顶层 payload
    """
    ctx = build_monitor_context(ent, keyword_settings)
    heat = build_heat_context(ctx.keywords, NORMALIZED_DIR)
    kw_result = build_keyword_summaries(ctx, heat)
    theme_indexes = build_account_theme_indexes(ctx)
    acct_summaries = build_account_summaries(ctx, kw_result, theme_indexes)

    return {
        "generated_at": now_iso(),
        "window_days": WINDOW_DAYS,
        "window_start": ctx.window_dates[0].isoformat(),
        "window_end": ctx.window_dates[-1].isoformat(),
        "account_score_method": "three_board_breakthrough_v5_1",
        "wso_fit_meta": wso_fit_meta(heat),
        "keywords": kw_result.keywords,
        "accounts": acct_summaries,
    }
