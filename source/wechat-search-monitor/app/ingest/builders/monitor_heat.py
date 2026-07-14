"""monitor_heat.py — AIDSO 关键词热度（WSO/DSO）加载与评分。

与 monitor_scoring.py 的区别：本模块负责 I/O：
- 读取 normalized/aidso_{wso,dso}_heat.json
- 计算幂律拟合，用于 WSO 缺失时按 DSO 估算
- 输出每个关键词的 heat_summary 与 kw_score

与 monitor_builder.py 的关系：
- builder 调用本模块的 `build_heat_context(keywords, normalized_dir)` 拿到
  预计算好的查找表与估算缓存；
- builder 内的 `build_keyword_summaries()` 通过 `heat_summary_for(text, ctx)`
  拿单个关键词的热度信息。
- 行为契约与原 monitor_builder.py 中的实现完全一致。
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class HeatContext:
    """AIDSO 热度查找上下文。

    `lookup` key 是 keyword_text，value 是 {dso?: {...}, wso?: {...}}。
    `wso_est_cache` 存估算值：key 是 keyword_text，value 是 (估算值, 是否估算)。
    `max_wso_with_est` 是用于归一化的 WSO 最大值（含估算）。
    `wso_fit` 是幂律拟合参数（None 表示样本不足，未启用估算）。
    """

    lookup: dict[str, dict] = field(default_factory=dict)
    wso_est_cache: dict[str, tuple[int | None, bool]] = field(default_factory=dict)
    max_wso_with_est: int = 0
    wso_fit: dict | None = None


def _load_aidso_heat_lookup(normalized_dir: Path) -> dict[str, dict]:
    """从 normalized/aidso_{dso,wso}_heat.json 读 month_cover_count，按 keyword_text 索引。

    派生层：合并 WSO + DSO 两通道的月搜索量，供前端色块展示。
    任一文件缺失或单条 error 的关键词，相应通道字段为 null。
    """
    lookup: dict[str, dict] = defaultdict(dict)
    sources = {
        "dso": normalized_dir / "aidso_dso_heat.json",
        "wso": normalized_dir / "aidso_wso_heat.json",
    }
    for channel, path in sources.items():
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in payload.get("items", []):
            if item.get("error"):
                continue
            keyword_text = item.get("keyword_text")
            if not keyword_text:
                continue
            count = item.get("month_cover_count")
            if not isinstance(count, (int, float)) or count <= 0:
                continue
            lookup[keyword_text][channel] = {
                "month_cover_count": int(count),
                "fetched_at": item.get("fetched_at"),
            }
    return lookup


def _fit_wso_power_law(heat_lookup: dict[str, dict], min_dso: int = 50) -> dict | None:
    """对同时有 DSO/WSO 且 DSO>=min_dso 的词做 log1p 线性回归，返回幂律参数或 None（样本不足）。

    注意：这是估算模型，用于在 WSO 缺失时根据 DSO 推算 WSO。仅当样本量>=5时才启用。
    返回的 max_wso 用于封顶估算值，不超过历史观测最大值。
    """
    pairs: list[tuple[int, int]] = []
    max_wso_obs = 0
    for hs in heat_lookup.values():
        d = hs.get("dso") or {}
        w = hs.get("wso") or {}
        dv = d.get("month_cover_count", 0)
        wv = w.get("month_cover_count", 0)
        if isinstance(dv, (int, float)) and isinstance(wv, (int, float)) and dv >= min_dso and wv > 0:
            pairs.append((int(dv), int(wv)))
            if int(wv) > max_wso_obs:
                max_wso_obs = int(wv)
    if len(pairs) < 5:
        return None
    xs = [math.log1p(dv) for dv, _ in pairs]
    ys = [math.log1p(wv) for _, wv in pairs]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0:
        return None
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return {
        "slope": slope,
        "intercept": intercept,
        "r2": r2,
        "n": n,
        "min_dso": min_dso,
        "max_wso": max_wso_obs,
    }


def _estimate_wso(dso_val: int, fit: dict | None, max_wso_cap: int) -> tuple[int | None, bool]:
    """根据幂律拟合估算 WSO 缺失值。返回 (估算值, 是否估算)。

    注意：这是估算值（wso_est），不是真实抓取数据。仅当 DSO>=拟合阈值时才估算。
    估算值会封顶于 max_wso_cap（历史实测最大值），避免外推失真。
    """
    if not fit or dso_val <= 0:
        return None, False
    if dso_val < fit.get("min_dso", 0):
        return None, False
    est = int(math.expm1(fit["slope"] * math.log1p(dso_val) + fit["intercept"]))
    est = max(0, min(est, max_wso_cap))
    return est, True


def _compute_kw_score(
    heat_summary: dict,
    tracked_accounts: int,
    article_count: int,
    max_wso: int,
    max_dso: int,
    max_acct: int,
    max_art: int,
) -> dict:
    """关键词热度分（V4：热度优先，WSO/DSO 50:50）。

    设计目标：
    - 微信搜索量和抖音搜索量先各自归一化，再 50:50 合成一个热度值。
    - 任一通道有数据就算有热度，进入热度段；缺失通道按 0 参与合成。
    - 两个通道都没有数据，才进入无热度段，并按丰度（沉淀文章数）排序。
    - 广度（tracked_accounts）只做展示，不进入 total 计算。

    输出字段：
    - heat：0-1，纯热度（WSO/DSO 50/50 基础 + 差值加成）。
    - breadth：0-1，纯广度（仅展示用，不参与排序）。
    - richness：0-1，纯丰度（article_count / max_art）。
    - total：用于排序的合成值。有热度时为 1.0 + heat（∈[1,2]），
             无热度时为 richness（∈[0,1)）。两段天然分离。
    - has_heat：是否存在任一通道的真实热度数据。
    """
    wso_val = heat_summary.get("wso", {}).get("month_cover_count", 0) if heat_summary.get("wso") else 0
    dso_val = heat_summary.get("dso", {}).get("month_cover_count", 0) if heat_summary.get("dso") else 0
    # WSO 缺失但有 DSO 信号时，用幂律估算补位（独立字段，不覆盖实测值）
    # 注意：wso_est_val 是估算值，只有 wso_estimated=True 时才可信
    wso_est_val = heat_summary.get("wso_est_val")
    wso_estimated = bool(heat_summary.get("wso_estimated"))
    if wso_est_val is not None and not wso_estimated:
        wso_est_val = None  # 只有明确标记为估算的才用
    _wso_for_score = wso_val if wso_val > 0 else (wso_est_val if wso_est_val and wso_estimated else 0)
    has_heat = bool(wso_val or dso_val or (_wso_for_score > 0))

    wso_n = math.log1p(_wso_for_score) / math.log1p(max_wso) if _wso_for_score > 0 and max_wso > 0 else 0.0
    dso_n = math.log1p(dso_val) / math.log1p(max_dso) if dso_val > 0 and max_dso > 0 else 0.0
    base_heat_n = wso_n * 0.5 + dso_n * 0.5
    peak_heat_n = max(wso_n, dso_n)
    # 高热度通道加成：基础 50/50 之外，把最高通道高于均值的部分补一半回来。
    # 等价于高通道 75% + 低通道 25%，避免单平台爆量被另一平台缺失硬砍半。
    heat_n = base_heat_n + (peak_heat_n - base_heat_n) * 0.5

    breadth_n = tracked_accounts / max_acct if max_acct > 0 else 0.0
    richness_n = article_count / max_art if max_art > 0 else 0.0

    if has_heat:
        total = 1.0 + heat_n
    else:
        total = richness_n

    return {
        "total": round(total, 4),
        "heat": round(heat_n, 4),
        "breadth": round(breadth_n, 4),
        "richness": round(richness_n, 4),
        "has_heat": has_heat,
        "wso_val": wso_val,  # 真实抓取的 WSO 月搜索量，0 表示缺失
        "dso_val": dso_val,  # 真实抓取的 DSO 月搜索量，0 表示缺失
        "wso_estimated": wso_estimated,  # True 表示 wso_est_val 是估算值，非真实数据
        "wso_est_val": wso_est_val if wso_estimated else None,  # 估算的 WSO 值，仅当 wso_estimated=True 时有效
    }


def build_heat_context(keywords: list[dict], normalized_dir: Path) -> HeatContext:
    """为给定关键词列表预计算 AIDSO 热度上下文。

    流程：
    1. 加载 WSO/DSO 原始热度数据，建立按 keyword_text 的查找表。
    2. 对同时有 WSO+DSO 的样本做幂律拟合，得到 `wso_fit`。
    3. 对 WSO 缺失但 DSO 有数据的关键词，预先估算 WSO 值缓存到 `wso_est_cache`。
    4. 计算 `max_wso_with_est`（用于 kw_score 归一化）。
    """
    lookup = _load_aidso_heat_lookup(normalized_dir)
    wso_fit = _fit_wso_power_law(lookup)

    wso_est_cache: dict[str, tuple[int | None, bool]] = {}
    if wso_fit:
        for kw_text, hs in lookup.items():
            d = hs.get("dso") or {}
            w = hs.get("wso") or {}
            dv = d.get("month_cover_count", 0)
            wv = w.get("month_cover_count", 0)
            if dv > 0 and (not wv or wv <= 0):
                wso_est_cache[kw_text] = _estimate_wso(dv, wso_fit, wso_fit["max_wso"])

    # 估算后的全局 max_wso（包含估算值，封顶于实测最大值）
    all_heat_summaries = [heat_summary_for(kw["keyword_text"], HeatContext(lookup=lookup, wso_est_cache=wso_est_cache)) for kw in keywords]
    max_wso_with_est = max(
        (
            max(
                hs.get("wso", {}).get("month_cover_count", 0),
                hs.get("wso_est_val") or 0,
            )
            for hs in all_heat_summaries
        ),
        default=0,
    )

    return HeatContext(
        lookup=lookup,
        wso_est_cache=wso_est_cache,
        max_wso_with_est=max_wso_with_est,
        wso_fit=wso_fit,
    )


def heat_summary_for(keyword_text: str, ctx: HeatContext) -> dict:
    """取某个关键词的热度信息（包含 WSO 估算），浅拷贝避免污染查找表缓存。"""
    hs = ctx.lookup.get(keyword_text, {})
    if not hs:
        return {}
    out = dict(hs)
    if keyword_text in ctx.wso_est_cache:
        est, is_est = ctx.wso_est_cache[keyword_text]
        if est is not None and is_est:
            # 注意：wso_est 是估算值（非真实抓取），estimated=True 标记其来源
            out["wso_est"] = {"month_cover_count": est, "estimated": True}
            out["wso_est_val"] = est
            out["wso_estimated"] = True
    return out


def compute_kw_score(
    heat_summary: dict,
    tracked_accounts: int,
    article_count: int,
    max_wso: int,
    max_dso: int,
    max_acct: int,
    max_art: int,
) -> dict:
    """对外暴露的 kw_score 计算（语义与 _compute_kw_score 完全相同）。"""
    return _compute_kw_score(
        heat_summary,
        tracked_accounts,
        article_count,
        max_wso,
        max_dso,
        max_acct,
        max_art,
    )


def wso_fit_meta(ctx: HeatContext) -> dict | None:
    """返回幂律拟合元信息（None 表示未启用估算）。"""
    if not ctx.wso_fit:
        return None
    fit = ctx.wso_fit
    return {
        "slope": round(fit["slope"], 4),
        "intercept": round(fit["intercept"], 4),
        "r2": round(fit["r2"], 4),
        "n": fit["n"],
        "min_dso": fit["min_dso"],
        "max_wso": fit["max_wso"],
    }


__all__ = [
    "HeatContext",
    "build_heat_context",
    "heat_summary_for",
    "compute_kw_score",
    "wso_fit_meta",
]
