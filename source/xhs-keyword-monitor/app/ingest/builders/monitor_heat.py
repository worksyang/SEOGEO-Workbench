"""XHS monitor heat — 暂不接入 AIDSO/WSO/DSO（按规范第八章规则）。

小红书场景下暂无稳定的外部热度源，因此：
- kw_score.has_heat 永远为 False
- heat 永远为 0
- richness（丰度）由快照数/命中数/账号数合成
未来若需要引入小红书官方热度或第三方热度，只在此模块新增方法，builder 无需改。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class HeatContext:
    keywords: list[dict]
    has_heat: bool = False
    method: str = "xhs_no_external_heat_v1"
    fit_meta: dict[str, Any] = None


def build_heat_context(keywords: list[dict], normalized_dir: Path) -> HeatContext:
    return HeatContext(keywords=list(keywords), has_heat=False, fit_meta={"available": False, "channels": []})


def wso_fit_meta(heat: HeatContext) -> dict[str, Any]:
    return {"available": False, "channels": [], "method": heat.method}
