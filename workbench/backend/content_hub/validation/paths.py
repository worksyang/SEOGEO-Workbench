"""路径解析与防穿越。

dev-plan §4.8：
- 文件读取、Wiki 保存、导入与媒体访问必须解析真实路径；
- 限制在配置的允许根目录内；
- 拒绝 .. 与软链接逃逸与绝对路径越权。
"""
from __future__ import annotations

from pathlib import Path

from content_hub.errors import ValidationAppError

__all__ = ["resolve_allowed_path", "resolve_within"]


def resolve_allowed_path(raw: str | Path, allowed_roots: tuple[Path, ...] | list[Path]) -> Path:
    candidate = Path(raw).expanduser().resolve(strict=False)
    for root in allowed_roots:
        resolved_root = Path(root).resolve(strict=False)
        if candidate == resolved_root or resolved_root in candidate.parents:
            return candidate
    raise ValidationAppError(f"路径不在允许根目录中：{candidate}")


def resolve_within(raw: str | Path, allowed_roots: tuple[Path, ...] | list[Path]) -> Path:
    return resolve_allowed_path(raw, tuple(allowed_roots))
