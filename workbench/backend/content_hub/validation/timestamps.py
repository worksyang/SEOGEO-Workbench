from __future__ import annotations

from datetime import UTC, datetime

from content_hub.errors import ValidationAppError


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
