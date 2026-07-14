"""penalty_signal_service — 惩罚信号（XHS 暂未启用）。"""
from __future__ import annotations

from typing import Any


def load_penalty_signals() -> dict[str, Any]:
    return {"signals": [], "method": "xhs_no_penalty_v1"}
