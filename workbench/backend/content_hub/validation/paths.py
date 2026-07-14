from __future__ import annotations

from pathlib import Path

from content_hub.errors import ValidationAppError


def resolve_allowed_path(raw: str | Path, allowed_roots: tuple[Path, ...]) -> Path:
    candidate = Path(raw).expanduser().resolve(strict=False)
    for root in allowed_roots:
        resolved_root = root.resolve(strict=False)
        if candidate == resolved_root or resolved_root in candidate.parents:
            return candidate
    raise ValidationAppError(f"路径不在允许根目录中：{candidate}")
