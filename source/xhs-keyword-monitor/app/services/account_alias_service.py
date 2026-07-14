"""account_alias_service — 账号别名归并（XHS 暂未启用）。"""
from __future__ import annotations

from typing import Any


def load_account_aliases() -> dict[str, Any]:
    return {"aliases": {}, "method": "xhs_no_aliases_v1"}
