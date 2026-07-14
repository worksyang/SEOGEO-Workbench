"""monitor_builder — 派生层编排入口。"""
from __future__ import annotations

from typing import Any

from app.ingest.builders.monitor_accounts import (
    build_account_summaries,
    build_account_theme_indexes,
)
from app.ingest.builders.monitor_context import build_monitor_context
from app.ingest.builders.monitor_heat import HeatContext, build_heat_context, wso_fit_meta
from app.ingest.builders.monitor_keywords import build_keyword_summaries
from app.ingest.builders.monitor_scoring import (
    BOARD_CONFIGS,
    RECENT_WINDOW_DAYS,
    TIMELINESS_WINDOW_DAYS,
    WINDOW_DAYS,
)
from app.ingest.common import now_iso


def build_monitor_data(ent: dict, keyword_settings: dict[str, dict] | None = None) -> dict:
    """编排：ctx → heat → keyword → account → payload。"""
    ctx = build_monitor_context(ent, keyword_settings)
    from pathlib import Path
    from app.config import Config
    normalized_dir = Config.NORMALIZED_DIR
    heat = build_heat_context(ctx.keywords, normalized_dir)
    kw_result = build_keyword_summaries(ctx, heat)
    theme_indexes = build_account_theme_indexes(ctx)
    acct_summaries = build_account_summaries(ctx, kw_result, theme_indexes)

    # 从 BOARD_CONFIGS 提取 axes 列表
    hexagon_axes = [{"key": m["key"], "label": m["label"]} for m in BOARD_CONFIGS["account"]["axes_meta"]]
    timeliness_axes = [{"key": m["key"], "label": m["label"]} for m in BOARD_CONFIGS["timeliness"]["axes_meta"]]
    today_axes = [{"key": m["key"], "label": m["label"]} for m in BOARD_CONFIGS["today"]["axes_meta"]]

    return {
        "generated_at": now_iso(),
        "window_days": WINDOW_DAYS,
        "window_start": ctx.window_dates[0],
        "window_end": ctx.window_dates[-1],
        "platform": "小红书",
        "account_score_method": "xhs_three_board_p99_v3_evidence_calibrated",
        "hexagon_axes": hexagon_axes,
        "timeliness_axes": timeliness_axes,
        "today_axes": today_axes,
        "wso_fit_meta": wso_fit_meta(heat),
        "keywords": kw_result.keywords,
        "accounts": acct_summaries,
    }


__all__ = [
    "build_monitor_data",
    "WINDOW_DAYS",
    "RECENT_WINDOW_DAYS",
    "TIMELINESS_WINDOW_DAYS",
]
