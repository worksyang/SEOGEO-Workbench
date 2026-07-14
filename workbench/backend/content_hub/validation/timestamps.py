"""时间相关校验与规范化。

依据：
- v3.2 §13.2: 数据库保存 ISO 8601 UTC；业务时间由 report_date + timezone 显式给出。
- dev-plan §4.4: 所有标准时间字段统一 ISO 8601 UTC，文件名与展示中切到 Asia/Shanghai。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from content_hub.errors import ValidationAppError

__all__ = ["parse_utc", "utc_now", "utc_now_iso", "is_utc_iso8601", "to_business_date"]

BUSINESS_TZ_OFFSET_HOURS: Final = 8  # Asia/Shanghai


def parse_utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValidationAppError("标准时间必须以 Z 结尾并使用 UTC。")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValidationAppError(f"无效 ISO 8601 时间：{value}") from exc
    if parsed.tzinfo != UTC:
        parsed = parsed.astimezone(UTC)
    return parsed


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now_iso() -> str:
    """与 utc_now 同语义；保留该别名便于代码风格统一。"""
    return utc_now()


def is_utc_iso8601(value: str) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parse_utc(value)
    except ValidationAppError:
        return False
    return True


def to_business_date(value: str) -> str:
    """UTC ISO 转 Asia/Shanghai 业务日期（YYYY-MM-DD）。"""
    utc = parse_utc(value)
    return (utc.astimezone(UTC).replace(tzinfo=None) + __import__("datetime").timedelta(hours=BUSINESS_TZ_OFFSET_HOURS)).date().isoformat()
